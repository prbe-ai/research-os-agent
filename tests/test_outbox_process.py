"""Process-level outbox tests: the gap between mock-verified and machine-verified.

Everything else in the outbox suite runs against an in-process fake transport
and deliberately never forks (a CLI test must not spawn real processes). These
tests do the opposite, on purpose:

  * a REAL detached worker, forked by the real ``maybe_spawn``, delivering to
    a REAL localhost HTTP server;
  * several REAL writer processes sharing one journal (flock for real);
  * SIGKILL mid-append and mid-drain, proving the atomic-write / fsync
    orderings empirically instead of by code review.

No GPUs, no external network: the only ingredients are subprocesses and a
loopback socket. Generous deadlines -- these assert outcomes, not timings.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from probe.sdk.journal import Journal, drain


class _Handler(BaseHTTPRequestHandler):
    def _respond(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _write_verb(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.server.delay:
            time.sleep(self.server.delay)
        if self.server.auth_fail:
            self._respond(401, b'{"detail": "expired"}')
            return
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = None
        with self.server.lock:
            self.server.records.append((self.command, self.path, body))
        self._respond(200, b"{}")

    do_POST = _write_verb
    do_PATCH = _write_verb

    def do_GET(self) -> None:
        self._respond(200, b'{"id": "r-proc", "tags": []}')

    def log_message(self, *args) -> None:  # noqa: D102 -- keep pytest output clean
        pass


@pytest.fixture
def live_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.records = []
    server.lock = threading.Lock()
    server.auth_fail = False
    server.delay = 0.0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def process_env(monkeypatch, tmp_path, live_server):
    """Environment REAL child processes inherit: loopback API, tmp config."""
    _, url = live_server
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(tmp_path / "config" / "probe.json"))
    monkeypatch.setenv("PROBE_BASE_URL", url)
    monkeypatch.setenv("PROBE_TOKEN", "probe_pat_process_test")
    monkeypatch.delenv("PROBE_ASYNC", raising=False)
    monkeypatch.delenv("PROBE_OUTBOX_DIR", raising=False)
    return url


@pytest.fixture
def journal(tmp_path, process_env) -> Journal:
    return Journal(
        tmp_path / "outbox", context={"name": None, "base_url": process_env}
    )


def _wait(predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def _received(server) -> list:
    with server.lock:
        return list(server.records)


# -- a real detached worker ---------------------------------------------------


def test_a_real_forked_worker_delivers_in_order_and_exits(live_server, journal):
    server, _ = live_server
    for step in (1, 2, 3):
        journal.append_http(
            "POST", "/v1/runs/r-proc/metrics", {"points": [{"step_index": step}]}
        )
    from probe.sdk import outbox_worker

    assert outbox_worker.maybe_spawn(str(journal.dir)) is True
    assert _wait(lambda: not journal.pending(), 30), "worker never emptied the journal"
    assert _wait(lambda: outbox_worker._lease_is_free(journal), 15), (
        "worker must release its lease when it exits"
    )
    steps = [
        point["step_index"]
        for _, _, body in _received(server)
        for point in body["points"]
    ]
    assert steps == [1, 2, 3], "FIFO must survive the process boundary"
    assert (journal.dir / "drainer.log").exists()


def test_a_real_worker_exits_3_on_auth_block_with_ops_untouched(live_server, journal):
    server, _ = live_server
    server.auth_fail = True
    journal.append_http("POST", "/v1/runs/r-proc/metrics", {"points": []})
    proc = subprocess.run(
        [sys.executable, "-m", "probe.sdk.outbox_worker", str(journal.dir)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 3, proc.stderr[-500:]
    assert len(journal.pending()) == 1, "auth block must leave ops queued (T2-A)"
    assert Journal.read_status(journal.dir)["auth_blocked_since"]


# -- many real processes, one journal -----------------------------------------

_WRITER = """
import sys
from probe.sdk.journal import Journal
directory, base_url, count, writer_id = sys.argv[1:5]
journal = Journal(directory, context={"name": None, "base_url": base_url})
journal.register_producer("stress:shared")
for i in range(int(count)):
    journal.append_http(
        "POST", "/v1/runs/r-proc/metrics",
        {"points": [{"writer": writer_id, "i": i}]},
    )
"""


def test_four_processes_share_one_journal_without_loss_or_duplication(
    live_server, journal, process_env
):
    server, _ = live_server
    writers = [
        subprocess.Popen(
            [sys.executable, "-c", _WRITER, str(journal.dir), process_env, "10", str(w)]
        )
        for w in range(4)
    ]
    for proc in writers:
        assert proc.wait(timeout=60) == 0
    sequences = [op["producer_sequence"] for _, op in journal.pending()]
    assert sorted(sequences) == list(range(1, 41)), (
        "a shared producer id must mint gapless, duplicate-free sequences "
        "across REAL processes"
    )
    report = drain(journal)
    assert report.clean and report.delivered == 40
    assert len(_received(server)) == 40


# -- SIGKILL, mid-append ------------------------------------------------------

_KILL_WRITER = """
import sys, time
from probe.sdk.journal import Journal
journal = Journal(sys.argv[1], context={"name": None, "base_url": sys.argv[2]})
journal.register_producer("kills:shared")
i = 0
while True:
    journal.append_http(
        "POST", "/v1/runs/r-proc/metrics", {"points": [{"i": i}]}
    )
    i += 1
    time.sleep(0.002)
"""


def test_sigkilled_writers_never_leave_a_torn_journal(live_server, journal, process_env):
    for _ in range(3):
        proc = subprocess.Popen(
            [sys.executable, "-c", _KILL_WRITER, str(journal.dir), process_env]
        )
        time.sleep(1.2)  # let it get through interpreter startup + some appends
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=30)
    journaled = len(journal.pending())
    assert journaled > 0, "the kill window closed before any append -- retune the sleep"
    assert journal.quarantine_corrupt() == 0, (
        "atomic appends: SIGKILL must never leave a half-written op"
    )
    report = drain(journal)
    assert report.clean and report.delivered == journaled
    (producer,) = journal.producer_report()
    # A kill between sequence-persist and op-write burns a sequence; the
    # invariant is a VISIBLE hole (high-water >= ops), never silent loss.
    assert producer["last_sequence"] >= journaled


# -- SIGKILL, mid-drain -------------------------------------------------------

_DRAINER = """
import sys
from probe.sdk.journal import Journal, drain
drain(Journal(sys.argv[1]))
"""


def test_sigkilled_drainer_loses_nothing(live_server, journal, process_env):
    server, _ = live_server
    total = 20
    for i in range(total):
        journal.append_http("POST", "/v1/runs/r-proc/metrics", {"points": [{"i": i}]})
    server.delay = 0.15  # slow the API so the kill lands mid-drain
    proc = subprocess.Popen([sys.executable, "-c", _DRAINER, str(journal.dir)])
    time.sleep(1.5)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=30)
    remaining = len(journal.pending())
    assert 0 < remaining <= total, "expected a mid-drain kill; retune the sleep"
    server.delay = 0.0
    # The kernel released the dead drainer's flock; a fresh drain must finish.
    report = drain(journal)
    assert report.clean and journal.pending() == []
    delivered_is = {
        point["i"] for _, _, body in _received(server) for point in body["points"]
    }
    assert delivered_is == set(range(total)), (
        "at-least-once: duplicates are allowed after a mid-flight kill, "
        "losing a point is not"
    )
