"""Device identity is minted locally — no pairing exchange exists anymore.

On daemon start, _run_loop reads meta["device_id"]; if absent it generates a
uuid4 hex and persists it. Every batch body carries it (the backend passes
it through to the engine as the device external id), so the mint must
happen before the first batch is built and must be stable across restarts.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _isolated_plugin_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="probe-research-tap-devid-test-")
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", tmp)
    # _run_loop requires a configured backend host (no hardcoded fallback).
    monkeypatch.setenv("PROBE_BASE_URL", "https://api.invalid")
    yield Path(tmp)


def _make_watch_config(tmp_path: Path, session_id: str):
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


def _drive_one_batch(config, storage) -> str | None:
    """Run _run_loop for one batch build; return the device_id it shipped."""
    from tap import main as tapmain

    captured: dict[str, str | None] = {"device_id": None}

    def fake_build_batch_body(**kwargs):
        if captured["device_id"] is None:
            captured["device_id"] = kwargs.get("device_id")
        config.shutdown_sentinel.touch()
        return None  # treat as drop-all so the loop exits cleanly

    def fake_tick_read(_c, _s):
        return [b"{}"], 0, lambda: None

    try:
        with (
            mock.patch("tap.main._tick_read", side_effect=fake_tick_read),
            mock.patch("tap.main.outbox.build_batch_body", side_effect=fake_build_batch_body),
            mock.patch("tap.main.outbox.drain_once", return_value=False),
            mock.patch("tap.main._transcript_has_active_reader", return_value=None),
            mock.patch("tap.main.time.sleep", return_value=None),
        ):
            tapmain._run_loop(config, storage)
    finally:
        config.shutdown_sentinel.unlink(missing_ok=True)
    return captured["device_id"]


def test_mints_and_persists_device_id_when_absent(
    _isolated_plugin_dir: Path, tmp_path: Path
) -> None:
    from tap import config as cfg
    from tap.storage import Storage

    config = _make_watch_config(tmp_path, "devid-fresh")
    storage = Storage(cfg.state_db_path())
    try:
        assert storage.get_meta("device_id") == ""
        shipped = _drive_one_batch(config, storage)
        persisted = storage.get_meta("device_id")
        assert persisted, "device_id must be persisted to meta on daemon start"
        assert shipped == persisted, "the batch body must carry the persisted device_id"
        # uuid4().hex: 32 lowercase hex chars.
        assert len(persisted) == 32
        int(persisted, 16)
    finally:
        storage.close()


def test_reuses_existing_device_id(_isolated_plugin_dir: Path, tmp_path: Path) -> None:
    from tap import config as cfg
    from tap.storage import Storage

    config = _make_watch_config(tmp_path, "devid-existing")
    storage = Storage(cfg.state_db_path())
    try:
        storage.set_meta("device_id", "pre-existing-device")
        shipped = _drive_one_batch(config, storage)
        assert shipped == "pre-existing-device"
        assert storage.get_meta("device_id") == "pre-existing-device"
    finally:
        storage.close()


def test_concurrent_mint_converges_on_one_id(
    _isolated_plugin_dir: Path, tmp_path: Path
) -> None:
    """Two daemons (two Storage handles on ONE db) minting in the same minute must
    converge on ONE device_id — the atomic INSERT ... ON CONFLICT DO NOTHING makes
    the first writer win and the second re-read the winner, instead of forking
    machine identity via last-writer-wins."""
    from tap import config as cfg
    from tap.storage import Storage

    db_path = cfg.state_db_path()
    daemon_a = Storage(db_path)
    daemon_b = Storage(db_path)
    try:
        # Each daemon generated its own uuid and races to claim the key.
        id_a = daemon_a.insert_meta_if_absent("device_id", "uuid-from-daemon-a")
        id_b = daemon_b.insert_meta_if_absent("device_id", "uuid-from-daemon-b")

        assert id_a == id_b, "both daemons must converge on the same device_id"
        # First writer wins; the second's insert is a no-op that re-reads the winner.
        assert id_a == "uuid-from-daemon-a"
        assert daemon_a.get_meta("device_id") == "uuid-from-daemon-a"
        assert daemon_b.get_meta("device_id") == "uuid-from-daemon-a"
    finally:
        daemon_a.close()
        daemon_b.close()


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
