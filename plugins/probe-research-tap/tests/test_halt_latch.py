"""A legacy outbox's 401 latch cannot fence or erase the v2 namespace."""

from unittest import mock

import pytest


@pytest.mark.parametrize("fingerprint", ["current", "old", ""])
def test_legacy_halt_is_preserved_but_does_not_block_protocol_negotiation(
    tmp_path, monkeypatch, fingerprint
):
    from tap import config as cfg
    from tap import main as tapmain
    from tap.storage import Storage

    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("PROBE_BASE_URL", "http://synthetic.invalid")
    monkeypatch.setenv("PROBE_INGEST_TOKEN", "synthetic")
    storage = Storage(cfg.state_db_path())
    storage.set_meta("last_401_at", "1234")
    storage.set_meta("last_401_token_sha256", fingerprint)
    storage.close()
    transcript = tmp_path / "source.jsonl"
    transcript.write_text("{}\n")
    with mock.patch.object(tapmain, "_run_durable_loop", return_value=0) as run:
        assert (
            tapmain.main(
                [
                    "--session-id",
                    "synthetic",
                    "--transcript",
                    str(transcript),
                    "--cwd",
                    str(tmp_path),
                ]
            )
            == 0
        )
    run.assert_called_once()
    storage = Storage(cfg.state_db_path())
    assert storage.get_meta("last_401_at") == "1234"
    assert storage.get_meta("last_401_token_sha256") == fingerprint
    storage.close()
