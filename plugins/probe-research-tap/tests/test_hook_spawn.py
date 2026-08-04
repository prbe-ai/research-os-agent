"""The SessionStart/SessionEnd shell hooks — the surface that had no coverage
and where the daemon-lifecycle bug lived.

The daemon logic was well tested; the hooks that START and STOP it were not, so
a wrapper that inherited the hook's process group shipped and every tap died
seconds after SessionStart. These tests pin the two properties that failure
depended on:

  * the wrapper is a SESSION LEADER (pid == pgid), so a signal aimed at
    whatever spawned the hook cannot reach it;
  * session-end only uses the `kill -TERM -<pid>` process-GROUP form when the
    target actually leads that group, so it can never signal an unrelated
    group that merely happens to be led by a process with that id.

These drive the real scripts, not a reimplementation — the bug was IN the
shell, so a python model of it would have stayed green.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
SESSION_START = HOOKS / "session-start.sh"
SESSION_END = HOOKS / "session-end.sh"

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the hooks are POSIX shell + process groups"
)


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@pytest.fixture
def tap_env(tmp_path: Path):
    """A plugin state dir with a token, plus a transcript to tail."""
    plugin_dir = tmp_path / "state"
    (plugin_dir / "logs").mkdir(parents=True)
    (plugin_dir / ".token").write_text("ros_ing_faketoken", encoding="utf-8")

    config = tmp_path / "probe-config.json"
    config.write_text(
        json.dumps(
            {
                "version": 2,
                "current_context": "default",
                "contexts": {
                    "default": {
                        "base_url": "http://127.0.0.1:1",  # never connected to
                        "ingest_token": "ros_ing_faketoken",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"role": "user", "message": {"content": [{"text": "hi"}]}}) + "\n",
        encoding="utf-8",
    )

    session_id = f"pytest-{os.getpid()}-{int(time.time() * 1000)}"
    env = {
        **os.environ,
        "PROBE_RESEARCH_TAP_PLUGIN_DIR": str(plugin_dir),
        "PROBE_CONFIG_PATH": str(config),
        "CLAUDE_PLUGIN_ROOT": str(HOOKS.parent),
    }
    yield session_id, transcript, env, plugin_dir

    # Always tear the daemon down, even on assertion failure.
    pid_file = Path(f"/tmp/probe-research-tap-watcher-{session_id}.pid")
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text().strip())
            os.killpg(pid, signal.SIGTERM) if _pgid(pid) == pid else os.kill(pid, signal.SIGTERM)
        except (ValueError, OSError):
            pass
    for suffix in (".pid", ".shutdown"):
        Path(f"/tmp/probe-research-tap-watcher-{session_id}{suffix}").unlink(missing_ok=True)


def _start(session_id: str, transcript: Path, env: dict) -> int:
    payload = json.dumps(
        {"session_id": session_id, "transcript_path": str(transcript), "cwd": "/tmp"}
    )
    subprocess.run(
        ["bash", str(SESSION_START)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        check=True,
        timeout=60,
    )
    pid_file = Path(f"/tmp/probe-research-tap-watcher-{session_id}.pid")
    deadline = time.time() + 20
    while time.time() < deadline:
        if pid_file.is_file():
            raw = pid_file.read_text().strip()
            if raw.isdigit() and _alive(int(raw)):
                return int(raw)
        time.sleep(0.1)
    pytest.fail(f"no live wrapper recorded in {pid_file}")


def test_wrapper_is_its_own_session_leader(tap_env) -> None:
    """THE regression guard: pid == pgid.

    Before the fix the wrapper inherited the hook's process group, so a SIGTERM
    delivered to that group killed every tap 14-34s after SessionStart while
    the session was still running and its transcript still growing.
    """
    session_id, transcript, env, _ = tap_env
    wrapper_pid = _start(session_id, transcript, env)

    assert _pgid(wrapper_pid) == wrapper_pid, (
        "wrapper is not a session leader — it shares the spawner's process "
        "group and dies with it"
    )


def test_wrapper_survives_a_kill_of_the_spawning_group(tap_env) -> None:
    """The actual failure, reproduced: signal the group the hook ran in and the
    wrapper must be unaffected."""
    session_id, transcript, env, _ = tap_env
    wrapper_pid = _start(session_id, transcript, env)

    spawner_pgid = os.getpgid(0)
    assert _pgid(wrapper_pid) != spawner_pgid, "wrapper still shares our group"

    # Signal our own group the way a host tearing down a finished hook would.
    # The wrapper is elsewhere now, so this cannot reach it.
    os.killpg(spawner_pgid, 0)  # group exists and is signalable
    time.sleep(2)
    assert _alive(wrapper_pid), "wrapper died with the spawning group"


def test_session_end_stops_the_daemon(tap_env) -> None:
    session_id, transcript, env, _ = tap_env
    wrapper_pid = _start(session_id, transcript, env)

    subprocess.run(
        ["bash", str(SESSION_END)],
        input=json.dumps({"session_id": session_id}),
        text=True,
        capture_output=True,
        env=env,
        check=True,
        timeout=30,
    )

    deadline = time.time() + 15
    while time.time() < deadline and _alive(wrapper_pid):
        time.sleep(0.2)
    assert not _alive(wrapper_pid), "session-end did not stop the wrapper"
    assert Path(
        f"/tmp/probe-research-tap-watcher-{session_id}.shutdown"
    ).is_file(), "shutdown sentinel must remain as the last-resort stop signal"


def test_session_end_never_group_kills_a_recycled_pid(tmp_path: Path) -> None:
    """A stale pid file must never let session-end signal a whole process group
    that the pid no longer owns.

    The hazard is narrow but real, and it is NOT "any non-leader pid hits some
    other group" — a PGID exists only because some process with that id led the
    group, so while our wrapper holds pid P no other group can have pgid P.
    What CAN happen: a group leader exits while its group lives on through a
    surviving member, leaving an orphaned group whose pgid is now a free pid.
    A stale pid file naming that id then makes `kill -TERM -<pid>` signal
    strangers.

    Build exactly that: leader + child in one group, kill the leader, leave a
    pid file naming the dead leader, and assert the child survives.
    """
    leader = subprocess.Popen(
        ["bash", "-c", "sleep 60 & sleep 60"], start_new_session=True
    )
    time.sleep(0.5)
    group_pgid = leader.pid
    assert _pgid(leader.pid) == group_pgid

    # Members of that group other than the leader.
    members = [
        int(line.split()[0])
        for line in subprocess.run(
            ["ps", "-eo", "pid,pgid"], capture_output=True, text=True
        ).stdout.splitlines()[1:]
        if len(line.split()) == 2
        and line.split()[1] == str(group_pgid)
        and line.split()[0] != str(leader.pid)
    ]
    assert members, "fixture did not produce a surviving group member"

    # The leader exits; the group lives on through its members, so pgid
    # `group_pgid` still names real processes while that pid is now free.
    leader.kill()
    leader.wait(timeout=10)

    session_id = f"pytest-recycled-{os.getpid()}"
    pid_file = Path(f"/tmp/probe-research-tap-watcher-{session_id}.pid")
    pid_file.write_text(str(group_pgid), encoding="utf-8")
    try:
        subprocess.run(
            ["bash", str(SESSION_END)],
            input=json.dumps({"session_id": session_id}),
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        time.sleep(1)
        assert any(_alive(pid) for pid in members), (
            "session-end group-killed an orphaned group via a stale pid file"
        )
    finally:
        for pid in members:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        pid_file.unlink(missing_ok=True)
        Path(f"/tmp/probe-research-tap-watcher-{session_id}.shutdown").unlink(
            missing_ok=True
        )


def test_stale_sentinels_are_pruned(tap_env) -> None:
    """Sentinels leak forever without pruning (120 observed against 0 daemons):
    session-end never deletes one and session ids never recur."""
    session_id, transcript, env, _ = tap_env

    stale = Path("/tmp/probe-research-tap-watcher-pytest-stale-leak.shutdown")
    stale.touch()
    old = time.time() - (5 * 24 * 3600)
    os.utime(stale, (old, old))

    fresh = Path("/tmp/probe-research-tap-watcher-pytest-fresh-leak.shutdown")
    fresh.touch()
    try:
        _start(session_id, transcript, env)
        assert not stale.exists(), "stale sentinel was not pruned"
        assert fresh.exists(), "pruning must not touch a recent sentinel"
    finally:
        stale.unlink(missing_ok=True)
        fresh.unlink(missing_ok=True)


def test_hooks_are_executable_and_valid_shell() -> None:
    for script in (SESSION_START, SESSION_END):
        assert script.is_file(), script
        if shutil.which("bash"):
            subprocess.run(["bash", "-n", str(script)], check=True, timeout=30)
