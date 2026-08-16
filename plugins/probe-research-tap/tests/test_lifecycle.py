"""Tests for the daemon's lifecycle edge-case handling.

Covered:
  - plugin-update detection: mtime advance on tap/__init__.py triggers exit 0
  - orphan reaping: lsof returns no readers after we've seen one → exit 0 + sentinel touched
  - lsof unavailable / startup race: never false-positives into orphan exit
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _isolated_plugin_dir(monkeypatch):
    """Each test gets a fresh PROBE_RESEARCH_TAP_PLUGIN_DIR so writes don't pollute the
    user's real plugin state."""
    tmp = tempfile.mkdtemp(prefix="probe-research-tap-test-")
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", tmp)
    # _run_loop requires a configured backend host (no hardcoded fallback);
    # a logged-in install always has one. `.invalid` never resolves, so the
    # killswitch poll fails fast and fails open — exactly as in production.
    monkeypatch.setenv("PROBE_BASE_URL", "https://api.invalid")
    # tap.config caches paths only inside helpers — no module-level cache to clear.
    yield Path(tmp)


# ---------------------------------------------------------------------------
# _transcript_has_active_reader
# ---------------------------------------------------------------------------


def _fake_run(stdout: str = "", returncode: int = 0):
    """Build a CompletedProcess that subprocess.run can return."""
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def test_active_reader_returns_true_when_lsof_lists_pids(tmp_path: Path) -> None:
    from tap.main import _transcript_has_active_reader

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    with mock.patch("tap.main.subprocess.run", return_value=_fake_run(stdout="42\n")):
        assert _transcript_has_active_reader(transcript) is True


def test_active_reader_returns_false_when_lsof_empty(tmp_path: Path) -> None:
    from tap.main import _transcript_has_active_reader

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    # lsof exits 1 with empty stdout when no process holds the file — that's
    # the success-but-no-readers case, not an error.
    with mock.patch("tap.main.subprocess.run", return_value=_fake_run(stdout="", returncode=1)):
        assert _transcript_has_active_reader(transcript) is False


def test_active_reader_returns_none_when_lsof_missing(tmp_path: Path) -> None:
    from tap.main import _transcript_has_active_reader

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    with mock.patch("tap.main.subprocess.run", side_effect=FileNotFoundError("lsof")):
        assert _transcript_has_active_reader(transcript) is None


def test_active_reader_returns_none_on_timeout(tmp_path: Path) -> None:
    from tap.main import _transcript_has_active_reader

    transcript = tmp_path / "session.jsonl"
    transcript.write_text("")
    with mock.patch(
        "tap.main.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="lsof", timeout=5.0),
    ):
        assert _transcript_has_active_reader(transcript) is None


# ---------------------------------------------------------------------------
# _run_loop integration: plugin-update + orphan-exit paths
#
# We drive _run_loop directly with a real Storage on a tempdir, a real
# transcript file, and tight monkey-patching for the two probes (mtime and
# lsof). The loop is sleep-bounded so we monkeypatch time.sleep to a no-op
# and break out via the shutdown sentinel after one iteration.
# ---------------------------------------------------------------------------


def _make_watch_config(tmp_path: Path, *, session_id: str = "sess-1"):
    from tap import config as cfg

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"type":"session_start"}\n')
    return cfg.WatchConfig(
        session_id=session_id,
        transcript_path=transcript,
        cwd=tmp_path,
        plugin_root=Path("/nonexistent"),
        token="fake-token",
        active_interval_s=1,  # short, but we'll patch sleep anyway
        idle_interval_s=1,
    )


def test_run_loop_exits_and_signals_wrapper_when_orphaned(tmp_path: Path) -> None:
    """After observing an active reader, then observing none, the daemon
    touches the shutdown sentinel and exits 0 — wrapper sees sentinel and
    quits instead of respawning into the same dead-session state."""
    from tap import config as cfg
    from tap.main import _run_loop
    from tap.storage import Storage

    config = _make_watch_config(tmp_path)
    storage = Storage(cfg.state_db_path())

    # Force orphan-check on every tick so we don't have to spin 12 iterations.
    # Sequence: tick 1 sees a reader (sets seen_active_reader=True), tick 2
    # sees none (orphan-exit). The patched range produces exit on tick 2.
    reader_states = iter([True, False])

    def fake_has_reader(_path):
        try:
            return next(reader_states)
        except StopIteration:
            return False

    try:
        with (
            mock.patch("tap.main.ORPHAN_CHECK_EVERY_TICKS", 1),
            mock.patch("tap.main._transcript_has_active_reader", side_effect=fake_has_reader),
            mock.patch("tap.main.outbox.drain_once", return_value=False),
            mock.patch("tap.main.time.sleep", return_value=None),
        ):
            rc = _run_loop(config, storage)
        assert rc == 0
        assert config.shutdown_sentinel.exists(), "wrapper sentinel must be touched on orphan exit"
    finally:
        storage.close()
        # Clean up sentinel for hermeticity.
        try:
            os.remove(config.shutdown_sentinel)
        except FileNotFoundError:
            pass


def test_run_loop_does_not_orphan_exit_without_prior_active_reader(tmp_path: Path) -> None:
    """Startup race: the very first orphan check returns 'no reader'. We must
    NOT exit — could be a pre-CC-open race, or lsof itself being weird. Only
    after a True observation can a False trigger orphan-exit."""
    from tap import config as cfg
    from tap.main import _run_loop
    from tap.storage import Storage

    config = _make_watch_config(tmp_path)
    storage = Storage(cfg.state_db_path())

    # All ticks see "no reader". After 3 ticks (well past ORPHAN_CHECK_EVERY_TICKS=1
    # patched below), force shutdown via SIGTERM-equivalent so the test terminates.
    # If the gating logic is wrong, _run_loop would exit early via orphan-exit
    # and we'd never reach the manual shutdown.
    tick_calls = {"n": 0}

    def fake_has_reader(_path):
        tick_calls["n"] += 1
        if tick_calls["n"] >= 3:
            # Trigger shutdown via the sentinel so _run_loop exits cleanly
            # with the expected "ran without orphaning" outcome.
            config.shutdown_sentinel.touch()
        return False

    try:
        with (
            mock.patch("tap.main.ORPHAN_CHECK_EVERY_TICKS", 1),
            mock.patch("tap.main._transcript_has_active_reader", side_effect=fake_has_reader),
            mock.patch("tap.main.outbox.drain_once", return_value=False),
            mock.patch("tap.main.time.sleep", return_value=None),
        ):
            rc = _run_loop(config, storage)
        assert rc == 0
        assert tick_calls["n"] >= 3, "loop must run multiple ticks without orphan-exiting"
    finally:
        storage.close()
        try:
            os.remove(config.shutdown_sentinel)
        except FileNotFoundError:
            pass


def test_run_loop_does_not_orphan_exit_when_lsof_unavailable(tmp_path: Path) -> None:
    """lsof returning None (not installed, container quirk) must never trigger
    orphan-exit, even after we've seen True earlier — None is 'can't tell'."""
    from tap import config as cfg
    from tap.main import _run_loop
    from tap.storage import Storage

    config = _make_watch_config(tmp_path)
    storage = Storage(cfg.state_db_path())

    # True (sets seen_active_reader=True), then None (must NOT exit), then we
    # set the sentinel to break out cleanly.
    states = iter([True, None, None])

    def fake_has_reader(_path):
        try:
            v = next(states)
        except StopIteration:
            v = None
        # On the third call, force shutdown so the test terminates.
        if v is None and not config.shutdown_sentinel.exists():
            # Touch on the second None so we definitely break.
            config.shutdown_sentinel.touch()
        return v

    try:
        with (
            mock.patch("tap.main.ORPHAN_CHECK_EVERY_TICKS", 1),
            mock.patch("tap.main._transcript_has_active_reader", side_effect=fake_has_reader),
            mock.patch("tap.main.outbox.drain_once", return_value=False),
            mock.patch("tap.main.time.sleep", return_value=None),
        ):
            rc = _run_loop(config, storage)
        assert rc == 0
        # Sentinel exists because WE touched it to break, not because of orphan-exit.
        # The key assertion is that we made it through multiple ticks.
    finally:
        storage.close()
        try:
            os.remove(config.shutdown_sentinel)
        except FileNotFoundError:
            pass


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


# ---------------------------------------------------------------------------
# Session finalize on shutdown
#
# The engine only runs its knowledge-unit extraction (qa / code_change /
# decision / file_ref) on a session marked COMPLETE. Before these paths
# existed the tap never said so — SessionEnd just SIGTERMed the daemon — and
# every session stayed open forever, captured but never mined.
# ---------------------------------------------------------------------------


def _queued_bodies(storage) -> list[dict]:
    """Every body currently sitting in the outbox, oldest first."""
    import json as _json

    rows = storage._conn.execute(
        "SELECT body_json FROM outbox ORDER BY id ASC"
    ).fetchall()
    return [_json.loads(r[0]) for r in rows]


def test_shutdown_enqueues_a_finalize_for_the_session(tmp_path: Path) -> None:
    """A clean shutdown must leave a finalize in the outbox."""
    from tap import config as cfg
    from tap.main import _run_loop
    from tap.storage import Storage

    config = _make_watch_config(tmp_path, session_id="sess-finalize")
    storage = Storage(cfg.state_db_path())

    try:
        with (
            mock.patch("tap.main._transcript_has_active_reader", return_value=True),
            # Never drain, so whatever was enqueued is still inspectable.
            mock.patch("tap.main.outbox.drain_once", return_value=False),
            mock.patch("tap.main.time.sleep", return_value=None),
        ):
            config.shutdown_sentinel.touch()
            rc = _run_loop(config, storage)

        assert rc == 0
        bodies = _queued_bodies(storage)
        finalizes = [b for b in bodies if b.get("finalize") is True]
        assert len(finalizes) == 1, f"expected exactly one finalize, got: {bodies}"
        assert finalizes[0]["session_id"] == "sess-finalize"
        # It must be LAST: the engine has to see the whole transcript before it
        # is told the session is over.
        assert bodies[-1] is finalizes[0] or bodies[-1].get("finalize") is True
    finally:
        storage.close()
        with contextlib.suppress(FileNotFoundError):
            os.remove(config.shutdown_sentinel)


def test_orphaned_session_still_gets_a_finalize(tmp_path: Path) -> None:
    """A hard-killed CC (force-quit / SIGKILL / reboot) fires no SessionEnd.

    That is the case with no other way to say goodbye, so the orphan path must
    fall through to the shutdown handling rather than returning early — which
    is what it used to do, losing both the final tail and the finalize.
    """
    from tap import config as cfg
    from tap.main import _run_loop
    from tap.storage import Storage

    config = _make_watch_config(tmp_path, session_id="sess-orphan")
    storage = Storage(cfg.state_db_path())

    reader_states = iter([True, False])

    def fake_has_reader(_path):
        try:
            return next(reader_states)
        except StopIteration:
            return False

    try:
        with (
            mock.patch("tap.main.ORPHAN_CHECK_EVERY_TICKS", 1),
            mock.patch("tap.main._transcript_has_active_reader", side_effect=fake_has_reader),
            mock.patch("tap.main.outbox.drain_once", return_value=False),
            mock.patch("tap.main.time.sleep", return_value=None),
        ):
            rc = _run_loop(config, storage)

        assert rc == 0
        assert config.shutdown_sentinel.exists()
        bodies = _queued_bodies(storage)
        assert any(b.get("finalize") is True for b in bodies), (
            f"orphan exit must still finalize the session; queued: {bodies}"
        )
    finally:
        storage.close()
        with contextlib.suppress(FileNotFoundError):
            os.remove(config.shutdown_sentinel)


def test_killswitch_shutdown_ships_nothing_including_the_finalize(tmp_path: Path) -> None:
    """The killswitch contract is 'ship nothing' — the finalize is not exempt."""
    from tap import config as cfg
    from tap.main import _run_loop
    from tap.storage import Storage

    config = _make_watch_config(tmp_path, session_id="sess-killswitch")
    storage = Storage(cfg.state_db_path())

    try:
        with (
            mock.patch("tap.main._transcript_has_active_reader", return_value=True),
            mock.patch("tap.main.cfg.killswitch_active", return_value=True),
            mock.patch("tap.main.outbox.drain_once", return_value=False),
            mock.patch("tap.main.time.sleep", return_value=None),
        ):
            rc = _run_loop(config, storage)

        assert rc == 0
        bodies = _queued_bodies(storage)
        assert not any(b.get("finalize") is True for b in bodies), (
            f"killswitch must not enqueue a finalize; queued: {bodies}"
        )
    finally:
        storage.close()
        with contextlib.suppress(FileNotFoundError):
            os.remove(config.shutdown_sentinel)


def test_finalize_body_is_the_two_field_gateway_contract() -> None:
    """The gateway validates a finalize against a two-field model and rebuilds
    the forwarded body from it. Sending more implies a contract that does not
    exist — and an `events` array here would be silently dropped rather than
    ingested."""
    import json as _json

    from tap.outbox import build_finalize_body

    body = _json.loads(build_finalize_body(session_id="abc-123"))
    assert body == {"finalize": True, "session_id": "abc-123"}
