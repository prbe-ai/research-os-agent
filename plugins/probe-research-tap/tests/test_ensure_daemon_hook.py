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
