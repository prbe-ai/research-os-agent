"""Tests for durable batch_seq high-water mark.

Bug being fixed: source_event_id at the gateway is "<session>:<batch_seq>"
and the gateway uses ON CONFLICT DO NOTHING on (customer, source_system,
source_event_id). The daemon used to resume batch_seq from outbox.max_batch_seq
only — empty after successful drains — so a daemon restart would replay
batch_seq 0,1,2,... and silently de-dupe at the gateway. We now keep a
durable high-water mark in `meta` under "last_batch_seq:<session>" and use
it on init.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _isolated_plugin_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="probe-research-tap-bseq-test-")
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", tmp)
    # _run_loop requires a configured backend host (no hardcoded fallback).
    monkeypatch.setenv("PROBE_BASE_URL", "https://api.invalid")
    yield Path(tmp)


def _make_storage() -> "Storage":
    from tap import config as cfg
    from tap.storage import Storage

    return Storage(cfg.state_db_path())


# ---------------------------------------------------------------------------
# Helpers: _read_int_meta + _batch_seq_meta_key
# ---------------------------------------------------------------------------


def test_meta_key_format_uses_session_id() -> None:
    from tap.main import _batch_seq_meta_key

    assert _batch_seq_meta_key("abc-123") == "last_batch_seq:abc-123"


def test_read_int_meta_returns_default_when_missing(_isolated_plugin_dir: Path) -> None:
    from tap.main import _read_int_meta

    storage = _make_storage()
    try:
        assert _read_int_meta(storage, "missing_key", default=-1) == -1
        assert _read_int_meta(storage, "missing_key", default=42) == 42
    finally:
        storage.close()


def test_read_int_meta_parses_stored_int(_isolated_plugin_dir: Path) -> None:
    from tap.main import _read_int_meta

    storage = _make_storage()
    try:
        storage.set_meta("k", "17")
        assert _read_int_meta(storage, "k", default=-1) == 17
    finally:
        storage.close()


def test_read_int_meta_falls_back_on_garbage(_isolated_plugin_dir: Path) -> None:
    """Defensive: a corrupted meta value shouldn't crash the daemon. Treat as
    missing and continue with the default."""
    from tap.main import _read_int_meta

    storage = _make_storage()
    try:
        storage.set_meta("k", "not-an-int")
        assert _read_int_meta(storage, "k", default=-1) == -1
    finally:
        storage.close()


# ---------------------------------------------------------------------------
# _run_loop init: batch_seq survives daemon restart even when outbox is empty
# ---------------------------------------------------------------------------


def _make_watch_config(tmp_path: Path, session_id: str = "session-X"):
    from tap import config as cfg

    transcript = tmp_path / f"{session_id}.jsonl"
    transcript.write_text("{}\n")
    return cfg.WatchConfig(
        session_id=session_id,
        transcript_path=transcript,
        cwd=tmp_path,
        plugin_root=Path("/nonexistent"),
        token="fake-token",
        active_interval_s=60,
        idle_interval_s=300,
    )


def test_run_loop_resumes_from_meta_when_outbox_empty(
    _isolated_plugin_dir: Path, tmp_path: Path
) -> None:
    """Simulates: prior daemon shipped through batch_seq=13, all rows drained
    (outbox empty). Restart: must resume at 14, not 0."""
    from tap import config as cfg, main as tapmain
    from tap.storage import Storage

    config = _make_watch_config(tmp_path, "session-restart")
    storage = Storage(cfg.state_db_path())
    storage.set_meta("last_batch_seq:session-restart", "13")
    # Outbox is empty (nothing queued).
    assert storage.max_batch_seq("session-restart") == -1

    # Capture the initial batch_seq the loop computes by spying on
    # outbox.build_batch_body's batch_seq kwarg the first time it's called.
    captured = {"batch_seq": None}

    def fake_build_batch_body(**kwargs):
        if captured["batch_seq"] is None:
            captured["batch_seq"] = kwargs.get("batch_seq")
        # Stop the loop after the first build call.
        config.shutdown_sentinel.touch()
        return None  # treat as drop-all so we exit cleanly

    def fake_tick_read(_c, _s):
        # Drive one tick with a single line so build_batch_body gets called.
        return [b'{}'], 0, lambda: None

    try:
        with (
            mock.patch("tap.main._tick_read", side_effect=fake_tick_read),
            mock.patch("tap.main.outbox.build_batch_body", side_effect=fake_build_batch_body),
            mock.patch("tap.main.outbox.drain_once", return_value=False),
            mock.patch("tap.main._transcript_has_active_reader", return_value=None),
            mock.patch("tap.main.time.sleep", return_value=None),
        ):
            tapmain._run_loop(config, storage)
        assert captured["batch_seq"] == 14, (
            f"expected resume at 14 (meta=13 +1), got {captured['batch_seq']}"
        )
    finally:
        storage.close()
        try:
            config.shutdown_sentinel.unlink()
        except FileNotFoundError:
            pass


def test_run_loop_takes_max_of_outbox_and_meta(
    _isolated_plugin_dir: Path, tmp_path: Path
) -> None:
    """Outbox high-water (5) AND meta high-water (13) both present — the
    loop must pick max+1=14, not just one or the other."""
    from tap import config as cfg, main as tapmain
    from tap.storage import Storage

    config = _make_watch_config(tmp_path, "session-mixed")
    storage = Storage(cfg.state_db_path())
    storage.set_meta("last_batch_seq:session-mixed", "13")
    # Pretend a stale row is still in the outbox at batch_seq=5.
    storage.enqueue_batch(
        session_id="session-mixed",
        batch_seq=5,
        cwd="/x",
        body=b"{}",
        created_at=0,
        next_attempt_at=0,
    )
    assert storage.max_batch_seq("session-mixed") == 5

    captured = {"batch_seq": None}

    def fake_build_batch_body(**kwargs):
        if captured["batch_seq"] is None:
            captured["batch_seq"] = kwargs.get("batch_seq")
        config.shutdown_sentinel.touch()
        return None

    def fake_tick_read(_c, _s):
        return [b'{}'], 0, lambda: None

    try:
        with (
            mock.patch("tap.main._tick_read", side_effect=fake_tick_read),
            mock.patch("tap.main.outbox.build_batch_body", side_effect=fake_build_batch_body),
            mock.patch("tap.main.outbox.drain_once", return_value=False),
            mock.patch("tap.main._transcript_has_active_reader", return_value=None),
            mock.patch("tap.main.time.sleep", return_value=None),
        ):
            tapmain._run_loop(config, storage)
        assert captured["batch_seq"] == 14
    finally:
        storage.close()
        try:
            config.shutdown_sentinel.unlink()
        except FileNotFoundError:
            pass


def test_run_loop_starts_at_zero_for_unknown_session(
    _isolated_plugin_dir: Path, tmp_path: Path
) -> None:
    """First-ever run for a brand new session: meta missing, outbox empty.
    batch_seq must start at 0 (max(-1,-1)+1)."""
    from tap import config as cfg, main as tapmain
    from tap.storage import Storage

    config = _make_watch_config(tmp_path, "session-fresh")
    storage = Storage(cfg.state_db_path())

    captured = {"batch_seq": None}

    def fake_build_batch_body(**kwargs):
        if captured["batch_seq"] is None:
            captured["batch_seq"] = kwargs.get("batch_seq")
        config.shutdown_sentinel.touch()
        return None

    def fake_tick_read(_c, _s):
        return [b'{}'], 0, lambda: None

    try:
        with (
            mock.patch("tap.main._tick_read", side_effect=fake_tick_read),
            mock.patch("tap.main.outbox.build_batch_body", side_effect=fake_build_batch_body),
            mock.patch("tap.main.outbox.drain_once", return_value=False),
            mock.patch("tap.main._transcript_has_active_reader", return_value=None),
            mock.patch("tap.main.time.sleep", return_value=None),
        ):
            tapmain._run_loop(config, storage)
        assert captured["batch_seq"] == 0
    finally:
        storage.close()
        try:
            config.shutdown_sentinel.unlink()
        except FileNotFoundError:
            pass


def test_enqueue_path_persists_high_water_mark(
    _isolated_plugin_dir: Path, tmp_path: Path
) -> None:
    """After a successful enqueue at batch_seq=N, meta[last_batch_seq:<sess>]
    must equal N. A daemon crash here means the next start picks up at N+1."""
    from tap import config as cfg, main as tapmain
    from tap.storage import Storage

    config = _make_watch_config(tmp_path, "session-persist")
    storage = Storage(cfg.state_db_path())
    storage.set_meta("last_batch_seq:session-persist", "13")  # restart at 14

    enqueue_calls = {"n": 0}

    def fake_enqueue(**kwargs):
        enqueue_calls["n"] += 1
        # After the first enqueue, set the shutdown sentinel so the loop
        # exits cleanly on its NEXT iteration.
        config.shutdown_sentinel.touch()

    def fake_tick_read(_c, _s):
        return [b'{}'], 0, lambda: None

    try:
        with (
            mock.patch("tap.main._tick_read", side_effect=fake_tick_read),
            mock.patch("tap.main.outbox.build_batch_body", return_value=b"{}"),
            mock.patch("tap.main.outbox.enqueue", side_effect=fake_enqueue),
            mock.patch("tap.main.outbox.drain_once", return_value=False),
            mock.patch("tap.main._transcript_has_active_reader", return_value=None),
            mock.patch("tap.main.time.sleep", return_value=None),
        ):
            tapmain._run_loop(config, storage)
        # We resumed at 14 and shipped one batch.
        assert enqueue_calls["n"] >= 1
        # Meta must reflect the highest batch_seq we ENQUEUED (14), not the
        # next one to be assigned (15).
        assert storage.get_meta("last_batch_seq:session-persist") == "14"
    finally:
        storage.close()
        try:
            config.shutdown_sentinel.unlink()
        except FileNotFoundError:
            pass


def test_meta_isolated_per_session(_isolated_plugin_dir: Path) -> None:
    """Two CC sessions running back-to-back must not share their batch_seq
    counter — the meta key includes the session_id."""
    from tap.main import _batch_seq_meta_key, _read_int_meta

    storage = _make_storage()
    try:
        storage.set_meta(_batch_seq_meta_key("sess-A"), "100")
        storage.set_meta(_batch_seq_meta_key("sess-B"), "5")
        assert _read_int_meta(storage, _batch_seq_meta_key("sess-A"), default=-1) == 100
        assert _read_int_meta(storage, _batch_seq_meta_key("sess-B"), default=-1) == 5
    finally:
        storage.close()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
