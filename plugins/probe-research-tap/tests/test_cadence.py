"""Tests for adaptive cadence — config resolution + active/idle switching.

Two layers:
  - cfg.intervals() resolution: env > .config > default, with the legacy
    single-knob escape hatch overriding both.
  - _run_loop's mode switching: active while transcript advances, idle
    after IDLE_THRESHOLD_TICKS empty ticks, back to active when activity
    resumes.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _isolated_plugin_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="probe-research-tap-cadence-test-")
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", tmp)
    # _run_loop requires a configured backend host (no hardcoded fallback).
    # Exercise the real credential source — a probe CLI config file pointed
    # at via PROBE_CONFIG_PATH — instead of the PROBE_BASE_URL env override.
    probe_cfg = Path(tmp) / "probe-config.json"
    probe_cfg.write_text(
        json.dumps({"base_url": "https://api.invalid", "ingest_token": "ing-test"})
    )
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(probe_cfg))
    monkeypatch.delenv("PROBE_BASE_URL", raising=False)
    monkeypatch.delenv("PROBE_INGEST_TOKEN", raising=False)
    # Clear any inherited interval env vars so each test starts clean.
    for var in (
        "PROBE_RESEARCH_TAP_INTERVAL_SECONDS",
        "PROBE_RESEARCH_TAP_ACTIVE_INTERVAL_SECONDS",
        "PROBE_RESEARCH_TAP_IDLE_INTERVAL_SECONDS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield Path(tmp)


def _write_config(plugin_dir: Path, data: dict) -> None:
    (plugin_dir / ".config").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# cfg.intervals()
# ---------------------------------------------------------------------------


def test_intervals_default(_isolated_plugin_dir: Path) -> None:
    from tap import config as cfg

    assert cfg.intervals() == (60, 300)


def test_intervals_legacy_env_overrides_both(
    _isolated_plugin_dir: Path, monkeypatch
) -> None:
    """PROBE_RESEARCH_TAP_INTERVAL_SECONDS=120 means flat 120s for active + idle —
    no adaptive switching."""
    from tap import config as cfg

    monkeypatch.setenv("PROBE_RESEARCH_TAP_INTERVAL_SECONDS", "120")
    assert cfg.intervals() == (120, 120)


def test_intervals_legacy_config_overrides_both(
    _isolated_plugin_dir: Path,
) -> None:
    from tap import config as cfg

    _write_config(_isolated_plugin_dir, {"sync_interval_seconds": 90})
    assert cfg.intervals() == (90, 90)


def test_intervals_active_only_via_config(_isolated_plugin_dir: Path) -> None:
    """Setting just active_interval_seconds keeps idle at default."""
    from tap import config as cfg

    _write_config(_isolated_plugin_dir, {"active_interval_seconds": 30})
    assert cfg.intervals() == (30, 300)


def test_intervals_idle_only_via_config(_isolated_plugin_dir: Path) -> None:
    from tap import config as cfg

    _write_config(_isolated_plugin_dir, {"idle_interval_seconds": 600})
    assert cfg.intervals() == (60, 600)


def test_intervals_both_via_config(_isolated_plugin_dir: Path) -> None:
    from tap import config as cfg

    _write_config(
        _isolated_plugin_dir,
        {"active_interval_seconds": 30, "idle_interval_seconds": 900},
    )
    assert cfg.intervals() == (30, 900)


def test_intervals_env_overrides_config(
    _isolated_plugin_dir: Path, monkeypatch
) -> None:
    from tap import config as cfg

    _write_config(_isolated_plugin_dir, {"active_interval_seconds": 30})
    monkeypatch.setenv("PROBE_RESEARCH_TAP_ACTIVE_INTERVAL_SECONDS", "45")
    assert cfg.intervals() == (45, 300)


def test_intervals_idle_clamped_up_to_active(_isolated_plugin_dir: Path) -> None:
    """Setting idle < active is nonsensical (we'd tick faster when slowing
    down). Clamp idle up to active."""
    from tap import config as cfg

    _write_config(
        _isolated_plugin_dir,
        {"active_interval_seconds": 120, "idle_interval_seconds": 30},
    )
    assert cfg.intervals() == (120, 120)


def test_intervals_zero_or_negative_falls_back_to_default(
    _isolated_plugin_dir: Path,
) -> None:
    from tap import config as cfg

    _write_config(
        _isolated_plugin_dir,
        {"active_interval_seconds": 0, "idle_interval_seconds": -5},
    )
    assert cfg.intervals() == (60, 300)


def test_intervals_garbage_config_falls_back_to_default(
    _isolated_plugin_dir: Path,
) -> None:
    from tap import config as cfg

    (_isolated_plugin_dir / ".config").write_text("{ not valid json")
    assert cfg.intervals() == (60, 300)


# ---------------------------------------------------------------------------
# _run_loop adaptive transitions
#
# We drive the loop with a mocked _tick_read whose return value flips between
# "had lines" and "no lines" to exercise the state machine. time.sleep is
# patched out and we capture each call's `slept < sleep_s` comparison via the
# arg that drove the inner loop. Easier: track the variable directly via a
# spy on the loop's sleep arg.
# ---------------------------------------------------------------------------


def _make_watch_config(tmp_path: Path, *, active: int = 60, idle: int = 300):
    from tap import config as cfg

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}\n")
    return cfg.WatchConfig(
        session_id="cadence-test",
        transcript_path=transcript,
        cwd=tmp_path,
        plugin_root=Path("/nonexistent"),
        token="fake-token",
        active_interval_s=active,
        idle_interval_s=idle,
    )


def _no_op_commit() -> None:
    return None


def _drive_run_loop(monkeypatch, tmp_path, tick_results, *, active=60, idle=300):
    """Run _run_loop with a scripted sequence of _tick_read returns.

    Each entry in `tick_results` is either:
      - "lines"   → returns ([b'{}'], 0, no-op commit) — daemon enqueues
      - "empty"   → returns ([], 0, no-op commit) — no new lines
      - "missing" → raises FileNotFoundError — transcript gone
    Sleep duration is captured per tick into the returned list. Loop exits
    after consuming all entries via the shutdown sentinel.
    """
    from tap import config as cfg, main as tapmain
    from tap.storage import Storage

    config = _make_watch_config(tmp_path, active=active, idle=idle)
    storage = Storage(cfg.state_db_path())

    sleeps: list[int] = []
    iteration = {"i": 0}

    def fake_tick_read(_c, _s):
        i = iteration["i"]
        iteration["i"] = i + 1
        if i >= len(tick_results):
            # Stop the loop by setting the sentinel so the next
            # _shutdown_observed() returns True.
            config.shutdown_sentinel.touch()
            return [], 0, _no_op_commit
        result = tick_results[i]
        if result == "lines":
            return [b'{}'], 0, _no_op_commit
        if result == "empty":
            return [], 0, _no_op_commit
        if result == "missing":
            raise FileNotFoundError("transcript")
        raise AssertionError(f"unknown tick result {result!r}")

    # The "sleep" call is in a 1s slice loop. We capture the OUTER
    # sleep_s via patching the loop variable check — easier route:
    # patch time.sleep to be a no-op, but also intercept the slice
    # loop bound by patching the loop's sleep target. We can do this
    # cleanly by inspecting via a wrapper around time.sleep that the
    # outer loop uses.
    #
    # Simpler approach: patch _run_loop's adaptive-decision to also
    # record sleep_s. We can do that by spying on log.info, since the
    # daemon logs the cadence transitions, but that's brittle.
    #
    # Cleanest: patch time.sleep so it tracks how many times it was
    # called per tick. Since each tick sleeps `sleep_s` 1-second slices,
    # the count between ticks IS the sleep_s used. We use a side-effect
    # counter that resets on each fake_tick_read call.
    sleep_counter = {"n": 0}

    def fake_sleep(_secs):
        sleep_counter["n"] += 1

    def fake_tick_with_sleep_capture(c, s):
        # Snapshot the previous tick's sleep count before producing this tick.
        if iteration["i"] > 0:
            sleeps.append(sleep_counter["n"])
            sleep_counter["n"] = 0
        return fake_tick_read(c, s)

    try:
        with (
            mock.patch("tap.main._tick_read", side_effect=fake_tick_with_sleep_capture),
            mock.patch("tap.main.outbox.drain_once", return_value=False),
            mock.patch("tap.main.outbox.enqueue", return_value=None),
            mock.patch("tap.main.outbox.build_batch_body", return_value=b"{}"),
            mock.patch("tap.main._transcript_has_active_reader", return_value=None),
            mock.patch("tap.main.time.sleep", side_effect=fake_sleep),
        ):
            tapmain._run_loop(config, storage)
        # Capture the final tick's sleep count.
        if sleep_counter["n"] > 0:
            sleeps.append(sleep_counter["n"])
        return sleeps
    finally:
        storage.close()
        try:
            config.shutdown_sentinel.unlink()
        except FileNotFoundError:
            pass


def test_run_loop_uses_active_when_lines_present(_isolated_plugin_dir: Path, monkeypatch, tmp_path: Path) -> None:
    """A tick that produces lines sleeps for the active interval."""
    sleeps = _drive_run_loop(
        monkeypatch, tmp_path,
        tick_results=["lines"],
        active=7, idle=99,
    )
    # First tick had lines → next sleep is `active` = 7.
    assert sleeps and sleeps[0] == 7


def test_run_loop_switches_to_idle_after_two_empty_ticks(
    _isolated_plugin_dir: Path, monkeypatch, tmp_path: Path
) -> None:
    """First empty tick stays on active. Second empty tick promotes to idle."""
    sleeps = _drive_run_loop(
        monkeypatch, tmp_path,
        tick_results=["empty", "empty", "empty"],
        active=7, idle=99,
    )
    # Tick 0 (empty, count=1)        → still active sleep
    # Tick 1 (empty, count=2)        → switches to idle, idle sleep
    # Tick 2 (empty)                 → idle sleep
    assert sleeps[0] == 7, f"first empty tick should stay active, got {sleeps}"
    assert sleeps[1] == 99, f"second empty tick should switch to idle, got {sleeps}"
    assert sleeps[2] == 99, f"third empty tick should remain idle, got {sleeps}"


def test_run_loop_switches_back_to_active_when_lines_resume(
    _isolated_plugin_dir: Path, monkeypatch, tmp_path: Path
) -> None:
    """After idle, the moment we see lines again, we drop back to active."""
    sleeps = _drive_run_loop(
        monkeypatch, tmp_path,
        tick_results=["empty", "empty", "empty", "lines", "empty"],
        active=7, idle=99,
    )
    # Tick 0 (empty)  → active sleep (7)
    # Tick 1 (empty)  → idle sleep (99) — promoted
    # Tick 2 (empty)  → idle sleep (99)
    # Tick 3 (lines)  → active sleep (7) — demoted
    # Tick 4 (empty)  → active sleep (7) — first empty after activity
    assert sleeps == [7, 99, 99, 7, 7], sleeps


def test_run_loop_treats_missing_transcript_as_empty_for_cadence(
    _isolated_plugin_dir: Path, monkeypatch, tmp_path: Path
) -> None:
    """FileNotFoundError counts toward the empty-tick threshold — same as
    'no new lines this tick'."""
    sleeps = _drive_run_loop(
        monkeypatch, tmp_path,
        tick_results=["missing", "missing", "missing"],
        active=7, idle=99,
    )
    # First missing-tick stays active, second promotes to idle, third stays idle.
    assert sleeps[0] == 7
    assert sleeps[1] == 99
    assert sleeps[2] == 99


def test_run_loop_flat_cadence_when_active_equals_idle(
    _isolated_plugin_dir: Path, monkeypatch, tmp_path: Path
) -> None:
    """If user set sync_interval_seconds (legacy flat mode), active and idle
    are equal — the mode-switch logs are noise but the sleep duration is
    consistent regardless of mode transitions."""
    sleeps = _drive_run_loop(
        monkeypatch, tmp_path,
        tick_results=["lines", "empty", "empty", "empty", "lines"],
        active=42, idle=42,
    )
    assert sleeps == [42, 42, 42, 42, 42]


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
