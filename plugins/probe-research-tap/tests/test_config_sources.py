# tests/test_config_sources.py
from tap import config as cfg


def test_capture_source_reads_pi(monkeypatch):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "pi")
    assert cfg.capture_source() == "pi"


def test_capture_source_is_case_and_space_insensitive(monkeypatch):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "  PI  ")
    assert cfg.capture_source() == "pi"


def test_unknown_source_env_falls_back_to_claude_code(monkeypatch):
    # Fallback is right HERE (an unset env is the Claude Code install) and
    # wrong in sources.get(). Keep the asymmetry.
    monkeypatch.setenv("PROBE_TAP_SOURCE", "cursor")
    assert cfg.capture_source() == "claude_code"


def test_webhook_path_follows_the_source(monkeypatch):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "pi")
    assert cfg.webhook_path() == "/ingest/v1/sessions/pi"
    monkeypatch.setenv("PROBE_TAP_SOURCE", "codex")
    assert cfg.webhook_path() == "/ingest/v1/sessions/codex"


# ---------------------------------------------------------------------------
# pi env overrides — mirrors the codex coverage in
# tests/test_reconcile.py (PRBE_CODEX_TAP_PLUGIN_DIR / PRBE_CODEX_SESSIONS_DIR).
# Nothing previously exercised PROBE_PI_TAP_PLUGIN_DIR / PROBE_PI_TAP_TOKEN,
# so a typo in either name would go undetected until a later phase built on
# top of it.
# ---------------------------------------------------------------------------


def test_plugin_dir_follows_the_pi_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "pi")
    monkeypatch.setenv("PROBE_PI_TAP_PLUGIN_DIR", str(tmp_path / "pi-state"))
    assert cfg.plugin_dir() == tmp_path / "pi-state"


def test_load_token_follows_the_pi_token_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "pi")
    # Isolate plugin_dir too, so load_token's .token-file check can't ever
    # touch a real ~/.pi/agent/state/probe-research-tap/.token.
    monkeypatch.setenv("PROBE_PI_TAP_PLUGIN_DIR", str(tmp_path / "pi-state"))
    monkeypatch.setenv("PROBE_PI_TAP_TOKEN", "pi-secret-token")
    assert cfg.load_token() == "pi-secret-token"


def test_an_unrecognized_source_falls_back_but_says_so(monkeypatch, caplog):
    """The live-capture side of the seam: warn, never raise.

    `probe.cli.capabilities.agent_source()` RAISES on this same case, because
    a wizard can afford to stop and ask. This daemon cannot: it runs inside a
    researcher's session, and breaking capture outright is worse than
    capturing under a wrong label. So it falls back -- but not silently, or it
    is just the original bug with extra steps.
    """
    import logging

    monkeypatch.setenv("PROBE_TAP_SOURCE", "pi2")
    with caplog.at_level(logging.WARNING):
        assert cfg.capture_source() == "claude_code"
    assert any("pi2" in r.getMessage() for r in caplog.records), caplog.records


def test_an_unset_source_falls_back_quietly(monkeypatch, caplog):
    """Unset is "nothing was asked" and must not warn -- that would fire on
    every Claude Code session ever."""
    import logging

    monkeypatch.delenv("PROBE_TAP_SOURCE", raising=False)
    with caplog.at_level(logging.WARNING):
        assert cfg.capture_source() == "claude_code"
    assert not [r for r in caplog.records if "PROBE_TAP_SOURCE" in r.getMessage()]
