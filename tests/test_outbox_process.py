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
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from probe.sdk import journal as journal_mod
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
        # The artifact dance -- presign, PUT the bytes, confirm -- lives here
        # rather than in a second fixture so an UPLOAD op can be drained over a
        # real socket, which is the whole point of this file. The PUT target is
        # this same server, standing in for the object store.
        if self.path.endswith("/uploads"):
            self._respond(
                200,
                json.dumps(
                    {
                        "artifact_id": uuid.uuid4().hex[:8],
                        "upload_url": (
                            f"http://127.0.0.1:{self.server.server_port}/blob/"
                            + uuid.uuid4().hex[:8]
                        ),
                        "have": False,
                    }
                ).encode(),
            )
            return
        if self.path.endswith("/confirm"):
            self._respond(200, b'{"id": "a-1", "status": "complete"}')
            return
        self._respond(200, b"{}")

    do_POST = _write_verb
    do_PATCH = _write_verb

    def do_PUT(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        with self.server.lock:
            self.server.blobs[self.path] = raw
        self._respond(200, b"{}")

    def do_GET(self) -> None:
        self._respond(200, b'{"id": "r-proc", "tags": []}')

    def log_message(self, *args) -> None:  # noqa: D102 -- keep pytest output clean
        pass


@pytest.fixture
def live_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    server.records = []
    server.blobs = {}
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


# -- staging headroom ---------------------------------------------------------
#
# snapshot_file clones copy-on-write only WITHIN one filesystem; a source on a
# network mount falls through to a real byte copy onto the local disk. Enqueue
# is fire-and-forget and drain is one serial worker, so a producer that outruns
# delivery fills the disk as its steady state. These pin the degrade path.


class _Usage:
    """What ``shutil.disk_usage`` returns, with a ``free`` we choose."""

    def __init__(self, free: int) -> None:
        self.total = 1 << 40
        self.free = free
        self.used = self.total - free


def _disk(monkeypatch, *, free: int, floor: int) -> None:
    monkeypatch.setattr(journal_mod, "MIN_FREE_BYTES", floor)
    monkeypatch.setattr(
        journal_mod.shutil, "disk_usage", lambda _path: _Usage(free)
    )


def _queue_upload(journal, src, **kw) -> dict:
    return journal.append_upload(
        anchor="run",
        anchor_id="r-proc",
        name=src.name,
        src_path=str(src),
        run_ref="r-proc",
        **kw,
    )


def test_staging_degrades_to_unstaged_when_the_floor_would_be_breached(
    monkeypatch, tmp_path, journal
):
    src = tmp_path / "shard.bin"
    src.write_bytes(b"x" * 4096)
    _disk(monkeypatch, free=8192, floor=1 << 30)
    queued = _queue_upload(journal, src, inline_hash=True)

    assert queued["staged"] is False
    ((_, op),) = journal.pending()
    assert op["upload"]["staged"] is False
    assert [p for p in journal.blobs_dir.iterdir()] == [], (
        "a breached floor must copy NOTHING -- staging the bytes to discover "
        "there was no room is the failure this guard exists to prevent"
    )
    # Fail-open: the op still went in, and it still names the bytes to send.
    assert op["upload"]["src_path"] == str(src)
    assert op["upload"]["blob"] is not None, "inline_hash still fingerprints"


def test_a_degraded_op_records_why_and_shows_up_in_status(
    monkeypatch, tmp_path, journal
):
    src = tmp_path / "shard.bin"
    src.write_bytes(b"x" * 4096)
    _disk(monkeypatch, free=8192, floor=1 << 30)
    _queue_upload(journal, src, inline_hash=True)
    _queue_upload(journal, src, inline_hash=True)

    ((_, op), _) = journal.pending()
    reason = op["upload"]["unstaged_reason"]
    assert "low disk" in reason
    assert "PROBE_OUTBOX_MIN_FREE_BYTES" in reason, "the reason must name its knob"
    assert str(4096) in reason and str(1 << 30) in reason, (
        "the reason must carry the numbers that produced it, or it cannot be "
        "acted on"
    )
    status = Journal.read_status(journal.dir)
    assert status["unstaged_low_disk"] == 2, "the count is cumulative, not per-op"


def test_a_zero_floor_disables_the_guard_entirely(monkeypatch, tmp_path, journal):
    src = tmp_path / "shard.bin"
    src.write_bytes(b"x" * 4096)
    # Zero free space: only the disable can produce a staged op here, so this
    # proves the switch rather than catching a machine that happened to be empty.
    _disk(monkeypatch, free=0, floor=0)
    queued = _queue_upload(journal, src, inline_hash=True)

    assert queued["staged"] is True
    ((_, op),) = journal.pending()
    assert op["upload"]["unstaged_reason"] is None
    assert (journal.blobs_dir / queued["blob"]).exists()
    assert Journal.read_status(journal.dir)["unstaged_low_disk"] == 0


def test_a_malformed_floor_override_warns_and_falls_back(monkeypatch):
    monkeypatch.setenv("PROBE_OUTBOX_MIN_FREE_BYTES", "2GB")
    with pytest.warns(UserWarning, match="PROBE_OUTBOX_MIN_FREE_BYTES"):
        fallback = journal_mod._min_free_bytes()
    assert fallback == 2 * 1024 * 1024 * 1024, (
        "a typo in an env var must not silently disable a disk guard"
    )
    monkeypatch.setenv("PROBE_OUTBOX_MIN_FREE_BYTES", "4096")
    assert journal_mod._min_free_bytes() == 4096
    monkeypatch.setenv("PROBE_OUTBOX_MIN_FREE_BYTES", "0")
    assert journal_mod._min_free_bytes() == 0, "zero must survive as zero"


def test_an_unstaged_upload_delivers_from_its_original_source(
    monkeypatch, tmp_path, journal, live_server
):
    server, _ = live_server
    payload = b"weights " * 512
    src = tmp_path / "weights.bin"
    src.write_bytes(payload)
    _disk(monkeypatch, free=8192, floor=1 << 30)
    # inline_hash=False also puts the drain-side hash on the unstaged path.
    _queue_upload(journal, src, inline_hash=False)
    ((_, op),) = journal.pending()
    assert op["upload"]["staged"] is False

    report = drain(journal)

    assert report.clean and report.delivered == 1
    with server.lock:
        stored = list(server.blobs.values())
    assert stored == [payload], (
        "an unstaged op must deliver the ORIGINAL bytes; the drain reads "
        "src_path when there is no blob"
    )
    assert src.exists(), "delivery must not consume the producer's own file"


def test_an_unstaged_upload_whose_source_vanished_dead_letters_clearly(
    monkeypatch, tmp_path, journal
):
    src = tmp_path / "vanished.bin"
    src.write_bytes(b"x" * 4096)
    _disk(monkeypatch, free=8192, floor=1 << 30)
    _queue_upload(journal, src, inline_hash=True)
    src.unlink()

    report = drain(journal)

    assert report.dead_lettered == 1 and not report.stopped_transient, (
        "a source that can never come back must dead-letter, not park the FIFO"
    )
    ((_, op),) = journal.failed()
    error = op["last_error"]
    assert "never staged" in error and "source file" in error
    assert "staged bytes for op" not in error, (
        "an op that was never staged must not report missing STAGED bytes -- "
        "that sends people hunting the blob store for a file never in it"
    )
    assert "low disk" in error, "the dead letter must carry why it was unstaged"


def test_a_missing_staged_blob_still_reports_staged_bytes(tmp_path, journal):
    """The other half of the same branch: the two absences must not converge."""
    src = tmp_path / "staged.bin"
    src.write_bytes(b"x" * 4096)
    queued = _queue_upload(journal, src, inline_hash=True)
    (journal.blobs_dir / queued["blob"]).unlink()
    src.unlink()

    report = drain(journal)

    assert report.dead_lettered == 1
    ((_, op),) = journal.failed()
    assert "staged bytes for op" in op["last_error"]


# -- per-producer accounting --------------------------------------------------

_CLI_WRITER = """
import sys
from probe.sdk.client import Client
from probe.sdk.journal import Journal
directory, base_url, count = sys.argv[1:4]
journal = Journal(directory, context={"name": None, "base_url": base_url})
client = Client(journal=journal, async_writes=True, auto_drain=False, surface="cli")
for i in range(int(count)):
    client.write("POST", "/v1/runs/r-proc/metrics", {"points": [{"i": i}]})
client.close()
"""


def _cli_writer(journal, base_url, count, producer_id=None):
    env = dict(os.environ)
    env.pop("PROBE_OUTBOX_PRODUCER_ID", None)
    if producer_id is not None:
        env["PROBE_OUTBOX_PRODUCER_ID"] = producer_id
    return subprocess.Popen(
        [sys.executable, "-c", _CLI_WRITER, str(journal.dir), base_url, str(count)],
        env=env,
    )


def test_concurrent_cli_writers_stay_distinct_when_each_names_itself(
    journal, process_env
):
    """The per-host `cli:<host>` id is right for a training loop and wrong for
    concurrent importers: they collapse into one line nobody can decompose."""
    writers = [
        _cli_writer(journal, process_env, 3, producer_id="import:shard-a"),
        _cli_writer(journal, process_env, 2, producer_id="import:shard-b"),
    ]
    for proc in writers:
        assert proc.wait(timeout=90) == 0

    report = {p["producer_id"]: p for p in journal.producer_report()}
    assert set(report) == {"import:shard-a", "import:shard-b"}
    assert report["import:shard-a"]["last_sequence"] == 3
    assert report["import:shard-b"]["last_sequence"] == 2
    assert all(p["role"] == "cli" for p in report.values())
    assert all(p["state"] == "open" for p in report.values()), (
        "naming a producer must not change its lifecycle: a shared id whose "
        "first process exits must not read as closed under its live siblings"
    )
    stamped = {}
    for _, op in journal.pending():
        stamped.setdefault(op["producer_id"], []).append(op["producer_sequence"])
    assert sorted(stamped["import:shard-a"]) == [1, 2, 3]
    assert sorted(stamped["import:shard-b"]) == [1, 2]


def test_an_unset_override_keeps_the_per_host_cli_identity(journal, process_env):
    import socket

    assert _cli_writer(journal, process_env, 2).wait(timeout=90) == 0
    ((record,),) = (journal.producer_report(),)
    assert record["producer_id"] == f"cli:{socket.gethostname()}"


def test_delivered_tallies_are_kept_per_producer(live_server, journal):
    a = Journal(journal.dir, context=journal.context)
    b = Journal(journal.dir, context=journal.context)
    a.register_producer("import:shard-a", role="cli")
    b.register_producer("import:shard-b", role="cli")
    for _ in range(3):
        a.append_http("POST", "/v1/runs/r-proc/metrics", {"points": []})
    for _ in range(2):
        b.append_http("POST", "/v1/runs/r-proc/metrics", {"points": []})

    report = drain(journal)

    assert report.clean and report.delivered == 5
    tallies = {p["producer_id"]: p["delivered"] for p in journal.producer_report()}
    assert tallies == {"import:shard-a": 3, "import:shard-b": 2}, (
        "DrainReport.delivered is per-pass and machine-wide; 'how much of what "
        "I enqueued has landed' has to be answerable per producer"
    )
    sequences = {p["producer_id"]: p["last_sequence"] for p in journal.producer_report()}
    assert sequences == {"import:shard-a": 3, "import:shard-b": 2}


def test_a_delivered_tally_survives_a_sigkilled_drain(
    live_server, journal, process_env
):
    server, _ = live_server
    writer = Journal(journal.dir, context=journal.context)
    writer.register_producer("kills:tally", role="cli")
    total = 20
    for i in range(total):
        writer.append_http("POST", "/v1/runs/r-proc/metrics", {"points": [{"i": i}]})

    server.delay = 0.15  # slow the API so the kill lands mid-drain
    proc = subprocess.Popen([sys.executable, "-c", _DRAINER, str(journal.dir)])
    time.sleep(1.5)
    os.kill(proc.pid, signal.SIGKILL)
    proc.wait(timeout=30)

    remaining = len(journal.pending())
    assert 0 < remaining < total, "expected a mid-drain kill; retune the sleep"
    (record,) = journal.producer_report()
    partial = record["delivered"]
    assert partial > 0, (
        "a tally batched to the end of the pass would read 0 here -- a pass "
        "that dies mid-flight is exactly when the number matters"
    )
    assert partial <= total - remaining, (
        "the tally is a FLOOR: it must never claim more than actually landed"
    )

    server.delay = 0.0
    drain(journal)
    (record,) = journal.producer_report()
    assert total - 1 <= record["delivered"] <= total, (
        "the crash window between unlinking an op and crediting it costs at "
        "most one op per crash, and never over-counts"
    )
