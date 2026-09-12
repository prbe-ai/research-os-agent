"""`tap start` — gates, pid-file states, and the spawn contract."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tap import config as cfg
from tap import start as tap_start

#: One fixed id for every test here, so the fixture can scrub the /tmp files
#: that carry it. `cfg.pid_file` and `cfg.shutdown_sentinel` live in the shared
#: /tmp by design (session-end.sh and a detached wrapper both have to find
#: them), so tmp_path isolation does not reach them.
SID = "01a06383-6f4e-751a-b94c-bcefef19938c"


@pytest.fixture
def plugin_dir(tmp_path, monkeypatch):
    """A private plugin state dir, so no test touches a real install."""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("PROBE_TAP_SOURCE", "pi")
    monkeypatch.setenv("PROBE_PI_TAP_PLUGIN_DIR", str(d))
    # `load_token()`'s last resort is the probe CLI's own config.json. Without
    # this the "unpaired" tests read the developer's real credentials and pass
    # or fail on whether they happen to be logged in.
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(d / "absent-probe-config.json"))
    monkeypatch.delenv("PROBE_PI_TAP_TOKEN", raising=False)
    for p in (cfg.pid_file(SID), cfg.shutdown_sentinel(SID)):
        p.unlink(missing_ok=True)
    yield d
    for p in (cfg.pid_file(SID), cfg.shutdown_sentinel(SID)):
        p.unlink(missing_ok=True)


@pytest.fixture
def paired(plugin_dir):
    (plugin_dir / ".token").write_text("probe_ing_test", encoding="utf-8")
    return plugin_dir


def _args(tmp_path, **over):
    transcript = over.pop("transcript", None)
    if transcript is None:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('{"type":"session"}\n', encoding="utf-8")
    return [
        "--session-id", over.pop("session_id", SID),
        "--cwd", str(over.pop("cwd", tmp_path)),
        "--transcript", str(transcript),
    ]


def test_unpaired_refuses_with_its_own_exit_code(plugin_dir, tmp_path):
    spawned = []
    assert tap_start.main(_args(tmp_path), spawn=spawned.append) == tap_start.EXIT_NOT_PAIRED
    assert spawned == []


def test_killswitch_refuses(paired, tmp_path):
    (paired / ".disabled").write_text("", encoding="utf-8")
    spawned = []
    assert tap_start.main(_args(tmp_path), spawn=spawned.append) == tap_start.EXIT_KILLSWITCH
    assert spawned == []


def test_disabled_path_refuses(paired, tmp_path):
    (paired / ".disabled_paths").write_text(str(tmp_path) + "\n", encoding="utf-8")
    spawned = []
    assert tap_start.main(_args(tmp_path), spawn=spawned.append) == tap_start.EXIT_DISABLED_PATH
    assert spawned == []


def test_missing_transcript_refuses(paired, tmp_path):
    spawned = []
    code = tap_start.main(
        _args(tmp_path, transcript=tmp_path / "gone.jsonl"), spawn=spawned.append
    )
    assert code == tap_start.EXIT_NO_TRANSCRIPT
    assert spawned == []


def test_old_interpreter_refuses_instead_of_crash_looping(paired, tmp_path):
    spawned = []
    code = tap_start.main(
        _args(tmp_path), spawn=spawned.append, version_info=(3, 10, 14)
    )
    assert code == tap_start.EXIT_INTERPRETER_TOO_OLD
    assert spawned == []


def test_happy_path_spawns_once_and_passes_the_transcript(paired, tmp_path):
    spawned = []
    assert tap_start.main(_args(tmp_path), spawn=spawned.append) == tap_start.EXIT_OK
    assert len(spawned) == 1
    argv = spawned[0]
    assert argv[0] == "/bin/sh"
    assert argv[1] == "-c"
    assert "--transcript" in argv
    assert argv[-1].endswith("session.jsonl")


def test_live_daemon_is_already_running_not_a_second_spawn(paired, tmp_path, monkeypatch):
    cfg.pid_file(SID).write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(tap_start, "_looks_like_the_uploader", lambda pid: True)
    spawned = []
    assert tap_start.main(_args(tmp_path), spawn=spawned.append) == tap_start.EXIT_OK
    assert spawned == []


def test_dead_pid_is_stale_so_the_spawn_proceeds(paired, tmp_path):
    pf = cfg.pid_file(SID)
    pf.write_text("999999999", encoding="utf-8")
    spawned = []
    assert tap_start.main(_args(tmp_path), spawn=spawned.append) == tap_start.EXIT_OK
    assert len(spawned) == 1


def test_live_pid_that_is_not_the_tap_is_stale(paired, tmp_path, monkeypatch):
    cfg.pid_file(SID).write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(tap_start, "_looks_like_the_uploader", lambda pid: False)
    spawned = []
    assert tap_start.main(_args(tmp_path), spawn=spawned.append) == tap_start.EXIT_OK
    assert len(spawned) == 1


def test_a_stale_shutdown_sentinel_is_cleared_before_spawning(paired, tmp_path):
    sentinel = cfg.shutdown_sentinel(SID)
    sentinel.write_text("", encoding="utf-8")
    tap_start.main(_args(tmp_path), spawn=lambda argv: None)
    assert not sentinel.exists()


def test_heal_marker_is_under_the_plugin_state_dir(plugin_dir):
    """The bash hook stats this exact path — see hooks/ensure-daemon.sh."""
    assert cfg.heal_marker(SID) == Path(plugin_dir) / "heal" / SID
