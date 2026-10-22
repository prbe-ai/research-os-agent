"""tap/service_link.py: `tap watch` keeps the Probe service told this session is alive.

Against a fake service on the real socket path (a throwaway HOME): register at
start with the session's mode and owner, re-register on a heartbeat and at once
after the service goes away, end on the way out; an off session never starts
or contacts the service; an owner change raises the generation; a newer
generation's refusal makes this one step back.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path

import pytest

from tap import companion_lease as lease
from tap import service_link
from tap.service import client, paths
from tap.service.protocol import Mode

#: The real `start`: conftest stubs it for every test so no `tap watch` reaches
#: the developer's own service.
REAL_START = service_link.start


class FakeService:
    """Answers hello / session.register / session.end, and records them."""

    def __init__(self) -> None:
        paths.ensure_dirs()
        self.path = paths.socket_path()
        self.calls: list[tuple[str, dict]] = []
        self.refuse_register: dict | None = None
        #: Errors for the next registers, one each, before refuse_register.
        self.refuse_next: list[dict] = []
        #: session.register's `alive` (False: the service found the owner not running).
        self.alive_answer = True
        self.conns: list[socket.socket] = []
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(str(self.path))
        self.sock.listen(8)
        self.sock.settimeout(0.1)
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:  # socket.timeout is one
                continue
            self.conns.append(conn)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        buffer = b""
        while True:
            try:
                chunk = conn.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                msg = json.loads(line)
                self.calls.append((msg["method"], msg.get("params") or {}))
                if msg["method"] == "session.register" and self.refuse_next:
                    reply = {"jsonrpc": "2.0", "id": msg["id"], "error": self.refuse_next.pop(0)}
                elif msg["method"] == "session.register" and self.refuse_register:
                    reply = {"jsonrpc": "2.0", "id": msg["id"], "error": self.refuse_register}
                elif msg["method"] == "session.register":
                    reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {"alive": self.alive_answer}}
                else:
                    reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {}}
                try:
                    conn.sendall((json.dumps(reply) + "\n").encode())
                except OSError:
                    return

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]

    def registers(self) -> list[dict]:
        return [p for m, p in self.calls if m == "session.register"]

    def drop_connections(self) -> None:
        for conn in self.conns:
            with contextlib.suppress(OSError):
                conn.shutdown(socket.SHUT_RDWR)
            conn.close()
        self.conns.clear()

    def close(self) -> None:
        self._stop.set()
        self.drop_connections()
        self.sock.close()
        self.thread.join(2)
        self.path.unlink(missing_ok=True)


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


@pytest.fixture
def home(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="pt-", dir="/tmp"))
    monkeypatch.setenv("HOME", str(root))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def service(home):
    fake = FakeService()
    yield fake
    fake.close()


def _state(sid: str, value: str) -> None:
    path = lease.sessions_dir() / f"{sid}.state"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)


class _Ensures:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self) -> str:
        self.count += 1
        return client.Ensured.BACKOFF


def _link(sid, *, owner=None, ensure=None, **kw):
    owners = owner if callable(owner) else (lambda: owner)
    return service_link.ServiceLink(
        sid,
        harness="test_harness",
        owner_fn=owners,
        ensure_fn=ensure or _Ensures(),
        heartbeat_s=kw.pop("heartbeat_s", 0.3),
        retry_s=kw.pop("retry_s", 0.05),
        undecided_wait_s=kw.pop("undecided_wait_s", 0.2),
        **kw,
    )


def test_registers_heartbeats_and_ends(service):
    _state("sid-1", "daemon")
    owner = {"pid": os.getpid(), "start_time": "123"}
    link = _link("sid-1", owner=owner).start()
    try:
        assert _wait(lambda: len(service.registers()) >= 3, 3.0), service.calls
    finally:
        link.stop()
    first = service.registers()[0]
    assert first == {
        "sid": "sid-1", "harness": "test_harness", "mode": "daemon", "owner": owner,
        "generation": link.generation,
        # Where this session's locks live, for the service's quiet check (plan D13).
        "state_dir": str(lease.sessions_dir().parent.resolve()),
    }
    assert service.methods()[0] == "service.hello"
    # Heartbeats are the same generation on the same connection: one hello.
    assert service.methods().count("service.hello") == 1
    assert {r["generation"] for r in service.registers()} == {link.generation}
    assert service.calls[-1] == ("session.end", {"sid": "sid-1", "generation": link.generation})


def test_reregisters_at_once_when_the_service_goes_away(service):
    _state("sid-2", "full")
    link = _link("sid-2", heartbeat_s=30.0).start()
    try:
        assert _wait(lambda: len(service.registers()) == 1)
        assert service.registers()[0]["mode"] == "inline"
        service.drop_connections()  # a restarted service
        assert _wait(lambda: len(service.registers()) == 2, 2.0), service.calls
        # One reconnect for the restart. Counted before stop(): its session.end may
        # open a connection of its own if the thread is between connections then.
        assert service.methods().count("service.hello") == 2
    finally:
        link.stop()


def test_an_off_session_never_starts_or_contacts_the_service(service):
    _state("sid-off", "off")
    ensures = _Ensures()
    link = _link("sid-off", ensure=ensures).start()
    time.sleep(0.5)
    link.stop()
    assert service.calls == []
    assert ensures.count == 0


def test_no_service_means_ensure_then_backoff(home):
    _state("sid-3", "full")
    ensures = _Ensures()
    link = _link("sid-3", ensure=ensures, heartbeat_s=0.4).start()
    time.sleep(0.6)
    link.stop()
    assert 1 <= ensures.count <= 4  # 0.05, 0.1, 0.2, 0.4 s between tries: not a hot loop


def test_switching_off_registers_off_and_stops_ensuring(service):
    _state("sid-4", "full")
    ensures = _Ensures()
    link = _link("sid-4", ensure=ensures).start()
    try:
        assert _wait(lambda: len(service.registers()) >= 1)
        _state("sid-4", "off")
        assert _wait(lambda: service.registers()[-1]["mode"] == "off", 3.0)
        service.drop_connections()
        service.close()  # the service is gone; an off session must not start one
        time.sleep(0.5)
    finally:
        link.stop()
    assert ensures.count == 0


def test_an_owner_change_raises_the_generation(service):
    _state("sid-5", "full")
    owners = [{"pid": 11, "start_time": "a"}]
    link = _link("sid-5", owner=lambda: owners[0]).start()
    try:
        assert _wait(lambda: len(service.registers()) >= 1)
        first = service.registers()[0]["generation"]
        owners[0] = {"pid": 12, "start_time": "b"}  # resumed in a new agent process
        assert _wait(lambda: service.registers()[-1]["owner"] == owners[0], 3.0)
    finally:
        link.stop()
    assert service.registers()[-1]["generation"] > first


def _stale(generation: int) -> dict:
    return {"code": -32012, "message": "refused", "data": {"reason": "stale_generation", "generation": generation}}


def test_a_newer_generation_makes_this_link_step_back(service):
    _state("sid-6", "full")
    link = _link("sid-6")
    service.refuse_register = _stale(link.generation + 1)
    link.start()
    assert _wait(lambda: link.superseded, 3.0)
    link.stop()
    assert len(service.registers()) == 1
    assert "session.end" not in service.methods()  # the newer registrant's to end


def test_its_own_ended_generation_registers_again_under_a_new_one(service):
    """The service ended THIS generation (a heartbeat lapse, an owner exit) and
    refuses it: no newer registrant exists, so the link must not step back."""
    _state("sid-6b", "full")
    link = _link("sid-6b")
    first = link.generation
    service.refuse_next.append(_stale(first))
    link.start()
    try:
        assert _wait(lambda: len(service.registers()) >= 2, 3.0), service.calls
    finally:
        link.stop()
    assert not link.superseded
    assert service.registers()[0]["generation"] == first
    assert service.registers()[1]["generation"] > first
    assert service.calls[-1] == ("session.end", {"sid": "sid-6b", "generation": link.generation})


def test_a_dead_owner_neither_holds_nor_restarts_the_service(service):
    """The service answered `alive: false`: the owner it was given is not running
    (the agent died without SessionEnd and this tap has not noticed yet). Such a
    session holds nothing, so the link must stop re-registering it and must not
    start a new service for it once the old one idles out -- until a new owner
    is recorded (a resume)."""
    _state("sid-dead", "full")
    owners = [{"pid": 11, "start_time": "a"}]
    ensures = _Ensures()
    service.alive_answer = False
    link = _link("sid-dead", owner=lambda: owners[0], ensure=ensures).start()
    revived = None
    try:
        assert _wait(lambda: len(service.registers()) >= 1)
        time.sleep(0.8)  # more than two heartbeats (0.3 s)
        assert len(service.registers()) == 1, service.calls
        service.close()  # nothing holds the service now: it idles out
        time.sleep(0.5)
        assert ensures.count == 0
        revived = FakeService()
        # Resumed in a new agent process (a real, running one: the link checks).
        owners[0] = client.owner_of(os.getpid())
        assert _wait(lambda: any(r["owner"] == owners[0] for r in revived.registers()), 3.0), revived.calls
    finally:
        link.stop()
        if revived is not None:
            revived.close()


def test_an_undecided_session_waits_then_counts_as_on(service):
    link = _link("sid-7", undecided_wait_s=0.3).start()
    try:
        time.sleep(0.15)
        assert service.registers() == []
        assert _wait(lambda: len(service.registers()) >= 1, 2.0)
    finally:
        link.stop()
    assert service.registers()[0]["mode"] == "inline"


@pytest.mark.parametrize(
    ("state", "profile", "mode"),
    [
        ("daemon", None, Mode.DAEMON),
        ("read-only", "daemon", Mode.DAEMON),
        ("read-only", None, Mode.INLINE),
        ("full", None, Mode.INLINE),
        ("off", None, Mode.OFF),
        (None, None, None),
    ],
)
def test_session_mode(home, state, profile, mode):
    if state:
        _state("sid-m", state)
    if profile:
        (lease.sessions_dir() / "sid-m.profile").write_text(profile)
    assert service_link.session_mode("sid-m") == mode


@pytest.mark.parametrize("state", ["full", "read-only"])
def test_quota_handoff_registers_inline_with_the_main_service(home, state):
    _state("sid-q", state)
    (lease.sessions_dir() / "sid-q.profile").write_text("daemon")
    (lease.sessions_dir() / "sid-q.inline").write_text(json.dumps({
        "from": "read-only" if state == "read-only" else "daemon", "since": time.time(),
        "mode": state, "reason": "budget_exhausted",
    }))
    assert service_link.session_mode("sid-q") == Mode.INLINE


def test_pi_names_its_own_process_as_the_owner(home, monkeypatch):
    monkeypatch.setenv(service_link.OWNER_PID_ENV, str(os.getpid()))
    link = service_link.ServiceLink("sid-9", harness="x")
    owner = link.owner_fn()
    assert owner["pid"] == os.getpid() and owner["start_time"]


def test_off_posix_there_is_no_link_and_no_ensure(home, monkeypatch):
    """Windows has no service yet (Unix socket, fcntl): `tap watch` must not
    try to start one from every session (D16)."""
    started = []
    monkeypatch.setattr(service_link, "ServiceLink", lambda *a, **k: started.append(a) or pytest.fail("linked"))
    monkeypatch.setattr(client, "ensure", lambda *a, **k: started.append("ensure") or client.Ensured.SPAWNED)
    with monkeypatch.context() as windows:
        # Only around the call: pathlib reads os.name on every Path().
        windows.setattr(service_link.os, "name", "nt")
        link = REAL_START("sid-win")
    assert link is None and started == []


def test_on_posix_start_links_the_session(home, monkeypatch):
    made = []

    class _Fake:
        def __init__(self, sid, *, harness, env=None):
            made.append((sid, harness, env))

        def start(self):
            return self

    monkeypatch.setattr(service_link, "ServiceLink", _Fake)
    assert isinstance(REAL_START("sid-posix"), _Fake)
    assert [sid for sid, _, _ in made] == ["sid-posix"]
    # Hosted by the service (plan A2): the session's environment, for the link's thread.
    assert isinstance(REAL_START("sid-hosted", env={"HOME": "/h"}), _Fake)
    assert made[-1][2] == {"HOME": "/h"}


def test_a_hosted_link_reads_under_its_sessions_environment(monkeypatch):
    """The link's own thread runs under the environment it was given: a hosted
    session's harness and folders, never the service process's."""
    import os

    import threading

    monkeypatch.setattr(os, "environ", os.environ)
    seen = []
    asked = threading.Event()

    def mode(sid):
        seen.append(os.environ.get("HOME"))
        asked.set()

    link = service_link.ServiceLink(
        "sid-env", harness="codex", env={"PROBE_TAP_SOURCE": "codex", "HOME": "/h/x"},
        mode_fn=mode, undecided_wait_s=0,
    )
    link.start()
    assert asked.wait(5)
    link.stop()
    assert seen and set(seen) == {"/h/x"}
