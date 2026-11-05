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
    monkeypatch.setenv("PROBE_TAP_SOURCE", "nonsense")
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


def test_pi_never_reads_the_cli_configs_capture_token(monkeypatch, tmp_path):
    """The CLI config's `ingest_token` is Claude Code's capture token; the
    server 403s it on pi's route, so an unpaired pi is unpaired (D3)."""
    import json

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"base_url": "https://api.research.prbe.ai", "ingest_token": "ros_ing_claude"}))
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(config))
    monkeypatch.delenv("PROBE_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("PROBE_BASE_URL", raising=False)
    monkeypatch.delenv("PROBE_PI_TAP_TOKEN", raising=False)
    monkeypatch.setenv("PROBE_PI_TAP_PLUGIN_DIR", str(tmp_path / "pi-state"))
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "claude-state"))

    monkeypatch.setenv("PROBE_TAP_SOURCE", "pi")
    assert cfg.load_token() is None

    monkeypatch.setenv("PROBE_TAP_SOURCE", "claude_code")
    assert cfg.load_token() == "ros_ing_claude"


def _claude(monkeypatch, tmp_path, config: dict, *, token: str | None = None, pinned: str | None = None):
    """Claude Code's capture, its plugin folder and the CLI config in `tmp_path`."""
    import json

    monkeypatch.setenv("PROBE_TAP_SOURCE", "claude_code")
    monkeypatch.delenv("PROBE_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("PROBE_BASE_URL", raising=False)
    plugin = tmp_path / "plugin"
    plugin.mkdir(exist_ok=True)
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(plugin))
    if token is not None:
        (plugin / ".token").write_text(token)
    if pinned is not None:
        (plugin / ".config").write_text(json.dumps({"api_base_url": pinned}))
    (tmp_path / "config.json").write_text(json.dumps(config))
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(tmp_path / "config.json"))


def test_the_cli_configs_token_goes_only_to_the_cli_configs_own_endpoint(monkeypatch, tmp_path):
    """D26 (service-only ROOT 2): an environment or a plugin's .config naming another endpoint never
    receives the CLI config's ingest token."""
    _claude(monkeypatch, tmp_path, {"base_url": "https://api.research.prbe.ai", "ingest_token": "ros_ing_cli"})
    assert cfg.load_token() == "ros_ing_cli" and cfg.api_base_url() == "https://api.research.prbe.ai"
    monkeypatch.setenv("PROBE_BASE_URL", "https://evil.example")
    assert cfg.load_token() is None, "the env's endpoint is not the config's own"
    monkeypatch.delenv("PROBE_BASE_URL")
    _claude(monkeypatch, tmp_path, {"base_url": "https://api.research.prbe.ai", "ingest_token": "ros_ing_cli"},
            pinned="https://evil.example")
    assert cfg.api_base_url() == "https://api.research.prbe.ai", "a .config pin without its own .token is no one's"
    assert cfg.load_token() == "ros_ing_cli"


def test_a_plugins_pinned_endpoint_goes_with_its_own_token_only(monkeypatch, tmp_path):
    _claude(monkeypatch, tmp_path, {"base_url": "https://api.research.prbe.ai", "ingest_token": "ros_ing_cli"},
            token="ros_dev_paired", pinned="https://paired.prbe.ai")
    assert cfg.load_token() == "ros_dev_paired" and cfg.api_base_url() == "https://paired.prbe.ai"


def test_a_session_recorded_for_a_fenced_caller_never_wants_a_daemon_worker():
    """Service-only round 3, H-b: `PROBE_CALLER_FENCED=1` (the service records it for a caller in a
    sandbox) records only; neither the hosted supervisor nor the service's starts its worker."""
    from tap import companion_supervisor

    assert companion_supervisor._is_fenced_session({"PROBE_CALLER_FENCED": "1"}) is True
    assert companion_supervisor._is_fenced_session({"PROBE_CALLER_FENCED": "0"}) is False
    assert companion_supervisor._is_fenced_session({}) is False
