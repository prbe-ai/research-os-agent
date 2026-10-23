"""The daemon worker's supervisor inside `tap watch`: how it spawns, when it gives
up, how it backs off a crashing worker, the SDK socket it drains, that it never
starts a second worker while one holds the session lock, and that hosted by a
service that supervises workers itself it hands its worker over and spawns none.

`test_companion.py` covers spawning only in `daemon` and exit 3; this covers the
rest.
"""

from __future__ import annotations

import fcntl
import json
import os
import socket
import threading
import time

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


def _sup(tmp_path, monkeypatch, exits, service=None):
    spawned: list[dict] = []
    clock = Clock()
    monkeypatch.setattr(supervisor_mod.subprocess, "Popen", _popen(exits, spawned))
    monkeypatch.setattr(supervisor_mod.time, "monotonic", clock)
    sup = supervisor_mod.Supervisor(session_id=SID, transcript=tmp_path / "t.jsonl", cwd=tmp_path, service=service)
    return sup, spawned, clock


class Link:
    """The service's `probe.service.workers.Link`, as the tap sees it."""

    def __init__(self, answer=True) -> None:
        self.answer = answer
        self.specs: list[dict] = []
        self.released = 0

    def supervise(self, spec):
        self.specs.append(spec)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def release(self):
        self.released += 1


def test_a_cli_without_the_worker_command_is_not_respawned(tmp_path, monkeypatch):
    sup, spawned, _ = _sup(tmp_path, monkeypatch, [2, 2])
    _set_state("daemon")
    for _ in range(4):
        sup.poll()
    assert len(spawned) == 1 and sup.gave_up_in == "daemon"


def _lease() -> dict | None:
    try:
        return json.loads(lease.lease_path(SID).read_text())
    except OSError:
        return None


@pytest.mark.parametrize("code", [3, 2])
def test_a_give_up_releases_the_lease_with_reason_error_and_respawning_is_unchanged(tmp_path, monkeypatch, code):
    """CRITICAL regression (crash-report plan B5): the give-up marks the lease,
    so the status line and the Probe service see a stopped daemon instead of a
    lapse; the supervisor's own rules (no respawn until the switch moves) hold."""
    sup, spawned, clock = _sup(tmp_path, monkeypatch, [code, None])
    _set_state("daemon")
    lease.renew(SID)  # the worker held it
    sup.poll()  # spawn
    sup.poll()  # it gave up
    data = _lease()
    assert data["reason"] == lease.REASON_ERROR and data["expires_at"] <= time.time()
    for _ in range(3):
        clock.now += 3600
        sup.poll()
    assert len(spawned) == 1 and sup.gave_up_in == "daemon"
    _set_state("off")
    sup.poll()
    _set_state("daemon")
    sup.poll()
    assert len(spawned) == 2, "the switch moved: it starts again, as before"


def test_a_failure_the_worker_released_its_lease_with_is_kept(tmp_path, monkeypatch):
    sup, _, _ = _sup(tmp_path, monkeypatch, [3])
    _set_state("daemon")
    lease.release(SID, lease.REASON_UNAUTHORIZED)  # the worker's own word on its way out: no key
    sup.poll()
    sup.poll()
    assert _lease()["reason"] == lease.REASON_UNAUTHORIZED


def test_no_cli_is_a_give_up_that_marks_the_lease(tmp_path, monkeypatch):
    sup, spawned, _ = _sup(tmp_path, monkeypatch, [])
    monkeypatch.setattr(supervisor_mod, "probe_cli", lambda: None)
    _set_state("daemon")
    sup.poll()
    assert spawned == [] and sup.gave_up_in == "daemon" and _lease()["reason"] == lease.REASON_ERROR


def test_a_crash_or_a_clean_exit_never_marks_the_lease(tmp_path, monkeypatch):
    sup, _, clock = _sup(tmp_path, monkeypatch, [supervisor_mod.EXIT_CRASHED, 0, supervisor_mod.EXIT_RESPAWN_LATER])
    _set_state("daemon")
    for _ in range(6):
        clock.now += supervisor_mod.CRASH_BACKOFF_CAP_SECONDS
        sup.poll()
    assert _lease() is None


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


def test_a_worker_that_left_over_the_switch_respawns_even_when_it_already_reads_daemon_again(tmp_path, monkeypatch):
    # T11, the race: the switch file was unreadable for a minute, the worker left,
    # and by the time this poll looks the switch reads `daemon` again. A give-up
    # recorded now would be recorded IN `daemon` and never respawn; the worker's
    # own exit code says "respawn later" instead.
    sup, spawned, clock = _sup(tmp_path, monkeypatch, [supervisor_mod.EXIT_RESPAWN_LATER, None])
    _set_state("daemon")
    sup.poll()  # spawn 1
    sup.poll()  # it left over the switch, which is readable again already
    assert sup.gave_up_in is None and sup.crashes == 0 and len(spawned) == 1
    clock.now += supervisor_mod.RESPAWN_LATER_SECONDS + 1
    sup.poll()
    assert len(spawned) == 2, "the session stays with the agent for good"


def test_a_worker_without_its_libraries_is_tried_again_later_not_given_up(tmp_path, monkeypatch):
    # 2026-10-12: the daemon's AI libraries were stripped by a reinstall and put
    # back an hour later; a supervisor that gave up (exit 3) left the session's
    # daemon down until the switch moved. Exit 6 waits, then tries again.
    sup, spawned, clock = _sup(tmp_path, monkeypatch, [supervisor_mod.EXIT_RETRY_LIBS, None])
    _set_state("daemon")
    sup.poll()  # spawn 1: no libraries
    sup.poll()
    assert sup.gave_up_in is None and sup.crashes == 0 and len(spawned) == 1
    clock.now += supervisor_mod.LIBS_RETRY_SECONDS - 1
    sup.poll()
    assert len(spawned) == 1, "not in a loop while they are still missing"
    clock.now += 2
    sup.poll()
    assert len(spawned) == 2


def test_every_worker_is_told_the_supervisor_retries_missing_libraries(tmp_path, monkeypatch):
    sup, spawned, _ = _sup(tmp_path, monkeypatch, [None])
    _set_state("daemon")
    sup.poll()
    assert spawned[0]["env"][supervisor_mod.RETRIES_LIBS_ENV] == "1"
    assert spawned[0]["env"][supervisor_mod.FOLLOWS_INLINE_ENV] == "1"
    assert spawned[0]["env"][supervisor_mod.READ_ONLY_INLINE_ENV] == "1"


def test_read_only_quota_inline_does_not_respawn_but_explicit_read_does(tmp_path, monkeypatch):
    sup, spawned, _ = _sup(tmp_path, monkeypatch, [0, None])
    _set_state("read-only")
    (lease.sessions_dir() / f"{SID}.profile").write_text("daemon")
    sup.poll()
    marker = lease.sessions_dir() / f"{SID}.inline"
    marker.write_text(json.dumps({"from": "read-only", "since": 100.0,
                                  "mode": "read-only", "reason": "budget_exhausted"}))
    for _ in range(4):
        sup.poll()
    assert len(spawned) == 1 and sup.gave_up_in is None
    # /probe read closes the inline marker; the read-only byte stays unchanged.
    marker.unlink()
    sup.poll()
    assert len(spawned) == 2


def test_a_worker_that_left_over_the_switch_waits_while_it_is_not_daemon(tmp_path, monkeypatch):
    sup, spawned, clock = _sup(tmp_path, monkeypatch, [supervisor_mod.EXIT_RESPAWN_LATER, None])
    _set_state("daemon")
    sup.poll()
    _set_state("full")
    for _ in range(3):
        clock.now += supervisor_mod.RESPAWN_LATER_SECONDS + 1
        sup.poll()
    assert len(spawned) == 1
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


def _hold_lock():
    path = supervisor_mod.daemon_dir() / f"{SID}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a")  # noqa: SIM115 -- the test closes it
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return handle


def test_no_second_worker_while_another_holds_the_session_lock(tmp_path, monkeypatch):
    """A worker whose exit this never saw (the Windows harness: ~100 waiting
    workers piled up), or a resumed session's predecessor: none is started
    while it holds the lock, and it is asked to hand over meanwhile."""
    sup, spawned, clock = _sup(tmp_path, monkeypatch, [None])
    _set_state("daemon")
    held = _hold_lock()
    try:
        for _ in range(10):
            sup.poll()
            clock.now += supervisor_mod.HANDOVER_TOUCH_SECONDS
        assert spawned == [] and sup.spawns == [], "a wait is no spawn: the per-minute cap is untouched"
        marker = supervisor_mod.daemon_dir() / f"{SID}.handover"
        assert time.time() - marker.stat().st_mtime < 10
    finally:
        held.close()
    sup.poll()
    assert len(spawned) == 1


def test_leaving_while_it_asked_the_holder_to_hand_over_withdraws_the_ask(tmp_path, monkeypatch):
    """Its session ends while another worker holds the lock: that worker must
    finish its queue, not hand it to a successor that never comes."""
    sup, spawned, _ = _sup(tmp_path, monkeypatch, [None])
    _set_state("daemon")
    marker = supervisor_mod.daemon_dir() / f"{SID}.handover"
    held = _hold_lock()
    try:
        sup.poll()
        assert marker.exists()
        sup.stop()
        assert not marker.exists() and spawned == []
        marker.touch()
        ahead = time.time() + 180
        os.utime(marker, (ahead, ahead))
        sup2, _, _ = _sup(tmp_path, monkeypatch, [None])
        sup2.poll()
        sup2.stop()
        assert marker.exists(), "an ask dated ahead (a stopping service's) is not this one's"
    finally:
        held.close()


def test_hosted_it_does_nothing_until_its_recorder_owns_the_session(tmp_path, monkeypatch):
    """A hosted recorder polls before it takes the journal's live claim; one
    that then steps back for another recorder of the session must start (or
    hand over) nothing -- every 15 s retry would otherwise churn a worker."""
    link = Link()
    sup, spawned, _ = _sup(tmp_path, monkeypatch, [None], service=link)
    _set_state("daemon")
    held = _hold_lock()
    try:
        for _ in range(3):
            sup.poll()
        assert spawned == [] and link.specs == [] and not sup.delegated
        assert not (supervisor_mod.daemon_dir() / f"{SID}.handover").exists()
    finally:
        held.close()
    sup.stop()
    assert link.released == 0


def test_hosted_by_a_supervising_service_it_hands_the_worker_over_and_spawns_none(tmp_path, monkeypatch):
    link = Link()
    sup, spawned, _ = _sup(tmp_path, monkeypatch, [None], service=link)
    _set_state("daemon")
    sup.poll()
    sup.claimed()
    for _ in range(3):
        sup.poll()
    assert spawned == [] and sup.delegated and len(link.specs) == 1
    (spec,) = link.specs
    assert spec["sid"] == SID and spec["transcript"] == str(tmp_path / "t.jsonl") and spec["cwd"] == str(tmp_path)
    assert spec["daemon_dir"] == str(supervisor_mod.daemon_dir()) and spec["source"] == "claude_code"
    assert spec["env"]["XDG_STATE_HOME"] == os.environ["XDG_STATE_HOME"]
    assert spec["switch"]() == ("daemon", True)
    _set_state("off")
    assert spec["switch"]() == ("off", False)
    assert sup.sock is not None, "the SDK socket stays the tap's"
    sup.stop()
    assert link.released == 1


def test_the_switch_it_hands_over_reads_its_own_session_s_state_from_any_thread(tmp_path, monkeypatch):
    """The service calls it on its own thread, whose environment is the
    service's: it must read under this session's (its XDG_STATE_HOME)."""
    link = Link()
    sup, _, _ = _sup(tmp_path, monkeypatch, [None], service=link)
    _set_state("daemon")
    sup.claimed()
    switch = link.specs[0]["switch"]
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "elsewhere"))
    got = {}
    thread = threading.Thread(target=lambda: got.setdefault("v", switch()))
    thread.start()
    thread.join(5)
    assert got["v"] == ("daemon", True)


@pytest.mark.parametrize("answer", [False, RuntimeError("an older service")])
def test_a_service_that_says_no_leaves_the_worker_here(tmp_path, monkeypatch, answer):
    link = Link(answer)
    sup, spawned, _ = _sup(tmp_path, monkeypatch, [None], service=link)
    _set_state("daemon")
    sup.claimed()
    sup.poll()
    sup.poll()
    assert len(spawned) == 1 and not sup.delegated
    assert len(link.specs) == 1, "asked once"
    sup.stop()
    assert link.released == 0
