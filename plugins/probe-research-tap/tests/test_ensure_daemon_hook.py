"""The UserPromptSubmit respawn hook: cheap when healthy, bounded when not."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from tap import config as cfg

HOOK = Path(__file__).resolve().parents[1] / "hooks" / "ensure-daemon.sh"

#: Distinct from test_start.py's id on purpose: both files reach the same
#: shared `/tmp` filenames, and a leftover pid file from one is a silent
#: "daemon is alive" in the other.
SID = "01a06383-6f4e-751a-b94c-ee5ea70d0001"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hook is POSIX shell")


def _run(payload: dict, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/bin/bash", str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=10,
    )


@pytest.fixture
def hook_env(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    spawn_log = tmp_path / "spawned.txt"
    fake_root = tmp_path / "plugin"
    (fake_root / "hooks").mkdir(parents=True)
    (fake_root / "hooks" / "session-start.sh").write_text(
        f"#!/bin/sh\necho ran >> {spawn_log}\n", encoding="utf-8"
    )
    (fake_root / "hooks" / "session-start.sh").chmod(0o755)
    pid_path = Path("/tmp") / f"probe-research-tap-watcher-{SID}.pid"
    pid_path.unlink(missing_ok=True)
    yield {
        "env": {
            "PLUGIN_ROOT": str(fake_root),
            # The claude_code state dir env — see tap/sources.py. NOT
            # PROBE_CLAUDE_TAP_PLUGIN_DIR, which is not a name anything reads.
            "PROBE_RESEARCH_TAP_PLUGIN_DIR": str(state),
            "PROBE_TAP_SOURCE": "claude_code",
        },
        "state": state,
        "spawn_log": spawn_log,
        "pid_path": pid_path,
    }
    pid_path.unlink(missing_ok=True)


def test_exits_without_spawning_when_a_daemon_is_alive(hook_env):
    hook_env["pid_path"].write_text(str(os.getpid()), encoding="utf-8")
    result = _run({"session_id": SID}, hook_env["env"])
    assert result.returncode == 0
    assert not hook_env["spawn_log"].exists()


def test_spawns_when_no_daemon_is_alive(hook_env):
    result = _run({"session_id": SID}, hook_env["env"])
    assert result.returncode == 0
    assert hook_env["spawn_log"].read_text(encoding="utf-8").strip() == "ran"


def test_a_fresh_marker_blocks_a_second_spawn(hook_env):
    first = _run({"session_id": SID}, hook_env["env"])
    second = _run({"session_id": SID}, hook_env["env"])
    # Both return codes, or this is vacuous: a hook that CRASHES on the marker
    # read also fails to spawn, and the count alone cannot tell the two apart.
    # `stat -f %m` did exactly that on GNU coreutils for every prompt after the
    # first, so the self-heal was dead on Linux under a green test.
    assert (first.returncode, second.returncode) == (0, 0), second.stderr
    assert hook_env["spawn_log"].read_text(encoding="utf-8").count("ran") == 1


def test_an_expired_marker_allows_another_spawn(hook_env):
    """The other direction: the bound is a real clock, not a crash.

    Paired with the test above this pins that the marker's mtime is genuinely
    parsed as a number. A hook that reads garbage and falls back to 0 re-spawns
    every time and fails the fresh-marker test; one that cannot read the mtime
    at all never re-spawns and fails this one.
    """
    _run({"session_id": SID}, hook_env["env"])
    marker = hook_env["state"] / "heal" / SID
    old_mtime = time.time() - 700  # past the ten-minute bound
    os.utime(marker, (old_mtime, old_mtime))
    result = _run({"session_id": SID}, hook_env["env"])
    assert result.returncode == 0, result.stderr
    assert hook_env["spawn_log"].read_text(encoding="utf-8").count("ran") == 2


def test_no_session_id_exits_quietly(hook_env):
    result = _run({}, hook_env["env"])
    assert result.returncode == 0
    assert not hook_env["spawn_log"].exists()


def test_a_dead_pid_does_not_count_as_a_live_daemon(hook_env):
    hook_env["pid_path"].write_text("999999999", encoding="utf-8")
    result = _run({"session_id": SID}, hook_env["env"])
    assert result.returncode == 0
    assert hook_env["spawn_log"].read_text(encoding="utf-8").strip() == "ran"


def test_the_marker_path_is_the_one_config_py_computes(hook_env, monkeypatch):
    """The ten-minute bound is ONE rule, so it must be one path.

    The hook resolves the marker in bash; `probe`'s `ensure_capture` resolves
    it through `tap.config.heal_marker`. Two spellings of the same path means
    two independent ten-minute windows, each blind to the other — and neither
    side would ever report an error.
    """
    _run({"session_id": SID}, hook_env["env"])
    for key, value in hook_env["env"].items():
        monkeypatch.setenv(key, value)
    assert cfg.heal_marker(SID).is_file()


def _hosted_marker(pid: int) -> Path:
    path = Path("/tmp") / f"probe-research-tap-watcher-{SID}.hosted"
    path.write_text(str(pid), encoding="utf-8")
    return path


def test_a_live_service_recording_the_session_spawns_nothing(hook_env):
    """Plan A2: the `.hosted` marker names the Probe service that records it."""
    marker = _hosted_marker(os.getpid())
    try:
        result = _run({"session_id": SID}, hook_env["env"])
        assert result.returncode == 0
        assert not hook_env["spawn_log"].exists()
    finally:
        marker.unlink(missing_ok=True)


def test_a_gone_service_is_started_again_without_the_ten_minute_wait(hook_env):
    """Its recorder's service crashed: the next prompt starts it again (it
    resumes every session it recorded), a fresh heal marker or not."""
    marker = _hosted_marker(999999999)
    try:
        assert _run({"session_id": SID}, hook_env["env"]).returncode == 0
        assert _run({"session_id": SID}, hook_env["env"]).returncode == 0
        assert hook_env["spawn_log"].read_text(encoding="utf-8").count("ran") == 2
    finally:
        marker.unlink(missing_ok=True)


def test_the_turn_end_hook_starts_a_gone_service_in_the_background(hook_env, tmp_path):
    """A long autonomous turn has no prompt: its Stop hook starts the service
    again (`tap service ensure`: it resumes every session it recorded), and
    never SessionStart in the background (a detached owner lookup finds no
    agent and drops the record, Codex R2)."""
    turn_end = tmp_path / "plugin" / "hooks" / "turn-end.sh"
    turn_end.write_text((HOOK.parent / "turn-end.sh").read_text(encoding="utf-8"), encoding="utf-8")
    turn_end.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "python3-calls.txt"
    (bin_dir / "python3").write_text(f'#!/bin/sh\necho "$*" >> {calls}\ncat >/dev/null\nexit 0\n')
    (bin_dir / "python3").chmod(0o755)
    env = {**hook_env["env"], "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    payload = json.dumps({"session_id": SID, "transcript_path": str(tmp_path / "t.jsonl"), "cwd": str(tmp_path)})

    def run():
        return subprocess.run(["/bin/bash", str(turn_end)], input=payload, capture_output=True, text=True,
                              env={**os.environ, **env}, timeout=10)

    marker = _hosted_marker(os.getpid())
    try:
        assert run().returncode == 0
        time.sleep(0.5)
        assert "service ensure" not in (calls.read_text() if calls.exists() else ""), "a live service: nothing to do"
        _hosted_marker(999999999)
        started = time.monotonic()
        assert run().returncode == 0
        assert time.monotonic() - started < 2, "never waits for the start"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and "service ensure" not in (calls.read_text() if calls.exists() else ""):
            time.sleep(0.05)
        assert "-m tap service ensure" in calls.read_text()
        assert not hook_env["spawn_log"].exists(), "never SessionStart in the background"
    finally:
        marker.unlink(missing_ok=True)


# -- the hot paths start no python (plan Q1: the tap's cheapest warm path) ------------
#
# A bash hook reaches a warm host only through a second interpreter (bash
# cannot hand a socket the harness's stdout and stderr), and that start alone
# costs more than these hooks' whole common path once their one python start
# is gone: measured on the devbox, ensure-daemon.sh 24.6 -> 5.1 ms per prompt
# and turn-end.sh 32.3 -> 3.5 ms per Stop, against 28.6 ms for one bash route
# behind the probe-research plugin's warm host.

#: Ids of their own, one per test: the shared `/tmp` marker names must not
#: meet another test's (xdist runs them side by side).
HOT_SID = "01a06383-6f4e-751a-b94c-ee5ea70d0002"
NESTED_SID = "01a06383-6f4e-751a-b94c-ee5ea70d0004"
STOP_SID = "01a06383-6f4e-751a-b94c-ee5ea70d0005"


def _python_spy(tmp_path):
    """A `python3` first on PATH that records each start, then runs the real one."""
    real = subprocess.run(["/bin/sh", "-c", "command -v python3"], capture_output=True, text=True).stdout.strip()
    bin_dir = tmp_path / "spy-bin"
    bin_dir.mkdir()
    calls = tmp_path / "python3-starts.txt"
    (bin_dir / "python3").write_text(f'#!/bin/sh\necho "$*" >> {calls}\nexec {real} "$@"\n')
    (bin_dir / "python3").chmod(0o755)
    return f"{bin_dir}:{os.environ['PATH']}", calls


def test_a_prompt_with_a_live_daemon_starts_no_python(hook_env, tmp_path):
    path, calls = _python_spy(tmp_path)
    pid_path = Path("/tmp") / f"probe-research-tap-watcher-{HOT_SID}.pid"
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        result = _run({"session_id": HOT_SID, "prompt": 'say "session_id": "x"'}, {**hook_env["env"], "PATH": path})
        assert result.returncode == 0
        assert not calls.exists(), "the common path started python"
        assert not hook_env["spawn_log"].exists()
    finally:
        pid_path.unlink(missing_ok=True)


def test_a_payload_naming_two_session_ids_is_parsed_as_before(hook_env, tmp_path):
    path, calls = _python_spy(tmp_path)
    nested = {"session_id": NESTED_SID, "extra": {"session_id": "01a06383-6f4e-751a-b94c-ee5ea70d0003"}}
    result = _run(nested, {**hook_env["env"], "PATH": path})
    assert result.returncode == 0
    assert calls.exists(), "an ambiguous payload must be parsed, never guessed"
    # The top-level id, as the parse reads it: its heal marker, not the nested one's.
    assert (hook_env["state"] / "heal" / NESTED_SID).is_file()
    assert not (hook_env["state"] / "heal" / "01a06383-6f4e-751a-b94c-ee5ea70d0003").exists()


def test_a_stop_outside_the_daemon_starts_no_python_and_marks_nothing(tmp_path):
    path, calls = _python_spy(tmp_path)
    xdg = tmp_path / "xdg"
    sessions = xdg / "probe" / "sessions"
    sessions.mkdir(parents=True)
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 7)
    hook = HOOK.parent / "turn-end.sh"
    env = {**os.environ, "PATH": path, "XDG_STATE_HOME": str(xdg), "PROBE_TAP_SOURCE": "claude_code"}
    payload = json.dumps({"session_id": STOP_SID, "transcript_path": str(transcript)})

    def stop():
        return subprocess.run(["/bin/bash", str(hook)], input=payload, capture_output=True, text=True, env=env,
                              timeout=10)

    for state in (None, "full", "off"):
        if state is not None:
            (sessions / f"{STOP_SID}.state").write_text(state, encoding="utf-8")
        assert stop().returncode == 0
        assert not calls.exists(), f"a Stop in state {state!r} started python"
        assert not (sessions / f"{STOP_SID}.turn").exists()
    (sessions / f"{STOP_SID}.state").write_text(" Daemon\n", encoding="utf-8")  # read as the python reads it
    assert stop().returncode == 0
    assert json.loads((sessions / f"{STOP_SID}.turn").read_text())["offset"] == 7


BIG_SID = "01a06383-6f4e-751a-b94c-ee5ea70d0006"


@pytest.mark.parametrize("size", [200_000, 300_000], ids=["read-in-bash", "parsed"])
def test_a_huge_prompt_stays_cheap_wherever_its_id_is(hook_env, tmp_path, size):
    """A pasted log before the id: the bash read is linear (a `${x#*pat}`
    here took 12 s at 120 KB), and past 256 KB the payload is parsed instead."""
    path, calls = _python_spy(tmp_path)
    pid_path = Path("/tmp") / f"probe-research-tap-watcher-{BIG_SID}.pid"
    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    try:
        payload = {"prompt": 'a "quoted" line\n' * (size // 16), "session_id": BIG_SID}
        started = time.monotonic()
        result = _run(payload, {**hook_env["env"], "PATH": path})
        assert result.returncode == 0
        assert time.monotonic() - started < 3
        assert calls.exists() is (size > 262144)
        assert not hook_env["spawn_log"].exists(), "the live daemon was found either way"
    finally:
        pid_path.unlink(missing_ok=True)
