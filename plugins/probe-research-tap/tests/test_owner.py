"""tap/owner.py: which agent process owns a session, and is it still running.

The daemon used to end a session on "no reader and ten quiet minutes"; Claude
Code holds no reader, so every pause ended a live session. These pin the
replacement: a session ends when its recorded owner PROCESS is gone, and never
on anything weaker.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

from tap import owner

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process ancestry is POSIX")


@pytest.fixture
def record_path(tmp_path, monkeypatch):
    path = tmp_path / "session.owner"
    monkeypatch.setattr(owner.cfg, "owner_file", lambda _sid: path)
    return path


def _write(path: Path, pid: int, start: str, name: str = "claude") -> None:
    path.write_text(json.dumps({"pid": pid, "start": start, "name": name}))


def _start_of(pid: int) -> str:
    info = owner._proc(pid)
    assert info is not None
    return info.start


def test_unknown_without_a_record(record_path):
    assert owner.state("sid") is owner.OwnerState.UNKNOWN


@pytest.mark.parametrize(
    "body", ["", "{}", '{"pid": "x", "start": "1"}', '{"pid": 1, "start": "1", "name": "c"}']
)
def test_unknown_for_an_unreadable_record(record_path, body):
    record_path.write_text(body)
    assert owner.state("sid") is owner.OwnerState.UNKNOWN


def test_alive_for_the_exact_process(record_path):
    _write(record_path, os.getpid(), _start_of(os.getpid()))
    assert owner.state("sid") is owner.OwnerState.ALIVE


def test_gone_once_the_process_exits(record_path):
    proc = subprocess.Popen(["sleep", "30"])
    _write(record_path, proc.pid, _start_of(proc.pid))
    assert owner.state("sid") is owner.OwnerState.ALIVE
    proc.kill()
    proc.wait(timeout=10)
    assert owner.state("sid") is owner.OwnerState.GONE


def test_a_reused_pid_reads_as_gone_never_alive(record_path):
    """Same pid, different start time: the owner exited and something else
    took its number. Reading that as alive would keep a dead session open."""
    _write(record_path, os.getpid(), "not-this-process")
    assert owner.state("sid") is owner.OwnerState.GONE


def test_an_unreaped_owner_is_gone(record_path):
    proc = subprocess.Popen(["true"])  # exits at once; never waited on yet
    deadline = time.time() + 10
    info = owner._proc(proc.pid)
    while time.time() < deadline and not (info and info.zombie):
        time.sleep(0.05)
        info = owner._proc(proc.pid)
    assert info is not None and info.zombie, "fixture did not leave a zombie"
    _write(record_path, proc.pid, info.start)
    try:
        assert owner.state("sid") is owner.OwnerState.GONE
    finally:
        proc.wait(timeout=10)


def test_a_record_another_user_wrote_is_ignored(record_path, monkeypatch):
    """/tmp is shared. Only this user's record may end this user's session."""
    _write(record_path, 2**22 + 12345, "gone")  # a pid that cannot be running
    assert owner.state("sid") is owner.OwnerState.GONE
    other_user = os.getuid() + 1
    monkeypatch.setattr(owner.os, "getuid", lambda: other_user)
    assert owner.state("sid") is owner.OwnerState.UNKNOWN


def _agent_tree(tmp_path: Path, name: str) -> tuple[subprocess.Popen, int]:
    """`name` -> sh -> sleep, the shape of an agent running a hook."""
    agent = tmp_path / name
    agent.write_text("#!/bin/bash\nsh -c 'echo $$; exec sleep 30' & wait\n")
    agent.chmod(0o755)
    proc = subprocess.Popen([str(agent)], stdout=subprocess.PIPE, text=True)
    leaf = int(proc.stdout.readline())
    return proc, leaf


def _kill_tree(proc: subprocess.Popen, leaf: int) -> None:
    with contextlib.suppress(OSError):
        os.kill(leaf, signal.SIGKILL)
    proc.kill()
    proc.wait(timeout=10)


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists(),
    reason="a #! script's process name is its file name only on Linux",
)
def test_find_owner_walks_up_to_the_agent(tmp_path, record_path, monkeypatch):
    monkeypatch.setattr(owner, "AGENT_NAMES", frozenset({"tapowner-agent"}))
    proc, leaf = _agent_tree(tmp_path, "tapowner-agent")
    try:
        found = owner.record("sid", leaf)
        assert found is not None and found.pid == proc.pid and found.name == "tapowner-agent"
        assert json.loads(record_path.read_text())["pid"] == proc.pid
        assert owner.state("sid") is owner.OwnerState.ALIVE
    finally:
        _kill_tree(proc, leaf)
    assert owner.state("sid") is owner.OwnerState.GONE


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="Linux process names")
def test_no_agent_ancestor_clears_the_record(tmp_path, record_path, monkeypatch):
    """A stale record would end the session when some unrelated old process
    exits. No agent found means unknown, and unknown never ends a session."""
    monkeypatch.setattr(owner, "AGENT_NAMES", frozenset({"no-such-agent"}))
    _write(record_path, os.getpid(), "stale")
    proc, leaf = _agent_tree(tmp_path, "tapowner-other")
    try:
        assert owner.record("sid", leaf) is None
        assert not record_path.exists()
        assert owner.state("sid") is owner.OwnerState.UNKNOWN
    finally:
        _kill_tree(proc, leaf)


def test_the_hook_process_itself_is_never_its_own_owner(monkeypatch):
    """`--from-pid $$` is the hook; only its ANCESTORS can own the session."""
    me = owner._proc(os.getpid())
    assert me is not None
    monkeypatch.setattr(owner, "AGENT_NAMES", me.names)
    found = owner.find_owner(os.getpid())
    assert found is None or found.pid != os.getpid()


def test_owner_subcommand_never_fails(record_path):
    assert owner.main(["--session-id", "sid", "--from-pid", "999999999"]) == 0
    assert not record_path.exists()
