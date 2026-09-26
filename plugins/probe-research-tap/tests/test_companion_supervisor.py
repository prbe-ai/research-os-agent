"""The daemon worker's supervisor inside `tap watch`: how it spawns, when it gives
up, how it backs off a crashing worker, and the SDK socket it drains.

`test_companion.py` covers spawning only in `daemon` and exit 3; this covers the
rest.
"""

from __future__ import annotations

import json
import socket

import pytest

from tap import companion_lease as lease
from tap import companion_supervisor as supervisor_mod

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(supervisor_mod, "probe_cli", lambda: "/usr/bin/probe")
    yield


def _set_state(state: str) -> None:
    path = lease.sessions_dir() / f"{SID}.state"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state)


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _popen(exits: list[int | None], spawned: list[dict]):
    """A Popen that exits with the next code of `exits` (None: keeps running)."""

    class FakePopen:
        def __init__(self, argv, **kw):
            spawned.append(kw)
            self.pid = 4000 + len(spawned)
            self.returncode = exits[len(spawned) - 1] if len(spawned) <= len(exits) else None

        def poll(self):
            return self.returncode

    return FakePopen


def _sup(tmp_path, monkeypatch, exits):
    spawned: list[dict] = []
    clock = Clock()
    monkeypatch.setattr(supervisor_mod.subprocess, "Popen", _popen(exits, spawned))
    monkeypatch.setattr(supervisor_mod.time, "monotonic", clock)
    sup = supervisor_mod.Supervisor(session_id=SID, transcript=tmp_path / "t.jsonl", cwd=tmp_path)
    return sup, spawned, clock


def test_a_cli_without_the_worker_command_is_not_respawned(tmp_path, monkeypatch):
    sup, spawned, _ = _sup(tmp_path, monkeypatch, [2, 2])
    _set_state("daemon")
    for _ in range(4):
        sup.poll()
    assert len(spawned) == 1 and sup.gave_up_in == "daemon"


def test_a_crashing_worker_is_restarted_after_a_growing_wait(tmp_path, monkeypatch):
    sup, spawned, clock = _sup(tmp_path, monkeypatch, [supervisor_mod.EXIT_CRASHED, 1, 0, None])
    _set_state("daemon")
    sup.poll()  # spawn 1
    sup.poll()  # it crashed: wait
    sup.poll()
    assert len(spawned) == 1 and sup.crashes == 1
    clock.now += supervisor_mod.CRASH_BACKOFF_SECONDS + 1
    sup.poll()  # spawn 2
    sup.poll()  # crashed again (a plain traceback exit): the wait doubles
    clock.now += supervisor_mod.CRASH_BACKOFF_SECONDS + 1
    sup.poll()
    assert len(spawned) == 2 and sup.crashes == 2
    clock.now += supervisor_mod.CRASH_BACKOFF_SECONDS
    sup.poll()  # spawn 3, which exits cleanly: the count resets
    sup.poll()
    assert len(spawned) == 4 and sup.crashes == 0
    assert sup.gave_up_in is None, "a crash is not a reason to stop for good"


def test_a_worker_that_left_over_an_unreadable_switch_starts_again_once_it_reads_daemon(tmp_path, monkeypatch):
    # The worker exits EXIT_DO_NOT_RESPAWN after its switch file was unreadable for a
    # minute (`probe.daemon.worker.SWITCH_UNREADABLE_EXIT_S`): no respawn while it
    # stays unreadable, one as soon as it reads `daemon` again.
    sup, spawned, _ = _sup(tmp_path, monkeypatch, [supervisor_mod.worker.EXIT_DO_NOT_RESPAWN, None])
    _set_state("daemon")
    sup.poll()
    (lease.sessions_dir() / f"{SID}.state").unlink()
    for _ in range(3):
        sup.poll()
    assert len(spawned) == 1 and sup.child is None
    _set_state("daemon")
    sup.poll()
    assert len(spawned) == 2


def test_the_supervisor_keeps_no_copy_of_the_childs_log_file(tmp_path, monkeypatch):
    sup, spawned, _ = _sup(tmp_path, monkeypatch, [None])
    _set_state("daemon")
    sup.poll()
    log_file = spawned[0]["stdout"]
    assert log_file.name.endswith(f"{SID}.log")
    assert log_file.closed, "one open file leaked into capture per spawn"


def test_sdk_messages_on_the_socket_land_in_the_inbox(tmp_path, monkeypatch):
    sup, _, _ = _sup(tmp_path, monkeypatch, [None])
    _set_state("daemon")
    sup.poll()
    path = supervisor_mod.socket_path(SID)
    assert path.exists()
    sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    try:
        sender.sendto(json.dumps({"event": "run_start", "run_id": "r1"}).encode(), str(path))
        sender.sendto(b"not json", str(path))
        sender.sendto(json.dumps(["not", "an", "object"]).encode(), str(path))
        sender.sendto(json.dumps({"event": "run_end", "run_id": "r1"}).encode(), str(path))
    finally:
        sender.close()
    sup.poll()
    inbox = supervisor_mod.daemon_dir() / f"{SID}.inbox.jsonl"
    lines = [json.loads(line) for line in inbox.read_text().splitlines()]
    assert [m["event"] for m in lines] == ["run_start", "run_end"]
    assert all(isinstance(m["received_at"], float) for m in lines)
    sup.stop()
    assert not path.exists() and sup.sock is None
