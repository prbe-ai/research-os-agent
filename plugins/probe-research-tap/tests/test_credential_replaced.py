"""A sign-in that replaces the capture key ends the recorder holding the old one (agent-prompt
install D39): its uploads must not go on under a key released a moment later, perhaps another
account's. The Probe service's guard records the session again, reading the new key."""

from __future__ import annotations

from pathlib import Path

from tap import config as cfg
from tap import main as tapmain


def _config(tmp_path: Path, token: str) -> cfg.WatchConfig:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n")
    return cfg.WatchConfig(session_id="synthetic-sid-0001", transcript_path=transcript, cwd=tmp_path,
                           plugin_root=None, token=token, active_interval_s=1, idle_interval_s=1)


def test_a_new_key_on_disk_ends_the_loop_to_record_under_it(tmp_path, monkeypatch):
    from tap.storage import Storage

    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("PROBE_BASE_URL", "http://synthetic.invalid")
    monkeypatch.setenv("PROBE_INGEST_TOKEN", "the-new-key")
    storage = Storage(cfg.state_db_path())
    try:
        assert tapmain._run_durable_loop(_config(tmp_path, "the-old-key"), storage) == tapmain.EXIT_CREDENTIAL_CHANGED
    finally:
        storage.close()


def test_the_same_key_or_none_readable_is_no_change(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("PROBE_INGEST_TOKEN", "k")
    assert not tapmain._is_credential_replaced(_config(tmp_path, "k"))
    monkeypatch.delenv("PROBE_INGEST_TOKEN")
    monkeypatch.setattr(cfg, "load_token", lambda: None)
    assert not tapmain._is_credential_replaced(_config(tmp_path, "k")), "mid-write: not a change"
