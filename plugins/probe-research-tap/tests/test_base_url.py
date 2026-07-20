"""Credential + endpoint resolution — the "no hardcoded fallback" contract.

The ingest token and backend base URL come from the environment
(PROBE_INGEST_TOKEN / PROBE_BASE_URL) or from the probe CLI's config file
($XDG_CONFIG_HOME/probe/config.json, default ~/.config/probe/config.json,
written by `probe login`; PROBE_CONFIG_PATH overrides the file path). Env
wins over the file. There is deliberately no baked-in default host — an
unconfigured plugin must fail loudly (APIBaseURLUnset) rather than guess —
and a missing ingest token means "not configured": the daemon and hooks
no-op instead of erroring.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from tap import config as cfg


@pytest.fixture(autouse=True)
def _isolated_plugin_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="probe-research-tap-baseurl-test-")
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", tmp)
    # Point the probe CLI config at a file that doesn't exist yet so each
    # test starts unconfigured and opts in by writing it.
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(Path(tmp) / "probe-config.json"))
    monkeypatch.delenv("PROBE_BASE_URL", raising=False)
    monkeypatch.delenv("PROBE_INGEST_TOKEN", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    yield Path(tmp)


def _write_probe_config(data: dict) -> Path:
    p = cfg.probe_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data))
    return p


# --- the contract: no hardcoded fallback -----------------------------------


def test_no_hardcoded_default_constant() -> None:
    """A reintroduced default host is the regression we're guarding — the SDK
    has one (api.research.prbe.ai) and the plugin must NOT inherit it."""
    assert not hasattr(cfg, "DEFAULT_API_BASE_URL")
    assert not hasattr(cfg, "DEFAULT_BASE_URL")


def test_api_base_url_raises_when_unconfigured() -> None:
    with pytest.raises(cfg.APIBaseURLUnset):
        cfg.api_base_url()


def test_webhook_path_targets_research_os_ingest() -> None:
    assert cfg.WEBHOOK_PATH == "/ingest/v1/sessions/claude-code"


# --- base_url resolution precedence -----------------------------------------


def test_env_base_url_wins_over_config_file(monkeypatch) -> None:
    _write_probe_config({"base_url": "https://file.example"})
    monkeypatch.setenv("PROBE_BASE_URL", "https://env.example/")
    assert cfg.api_base_url() == "https://env.example"  # trailing slash trimmed


def test_base_url_from_config_file() -> None:
    _write_probe_config({"base_url": "https://file.example/", "ingest_token": "t"})
    assert cfg.api_base_url() == "https://file.example"  # trailing slash trimmed


def test_config_file_without_base_url_still_raises() -> None:
    _write_probe_config({"ingest_token": "t"})
    with pytest.raises(cfg.APIBaseURLUnset):
        cfg.api_base_url()


def test_garbage_config_file_treated_as_unconfigured() -> None:
    p = cfg.probe_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not valid json")
    with pytest.raises(cfg.APIBaseURLUnset):
        cfg.api_base_url()
    assert cfg.load_token() is None


# --- ingest token resolution precedence --------------------------------------


def test_env_ingest_token_wins_over_config_file(monkeypatch) -> None:
    _write_probe_config({"ingest_token": "file-token"})
    monkeypatch.setenv("PROBE_INGEST_TOKEN", "env-token")
    assert cfg.load_token() == "env-token"


def test_ingest_token_from_config_file() -> None:
    _write_probe_config({"ingest_token": "file-token"})
    assert cfg.load_token() == "file-token"


def test_missing_ingest_token_is_not_configured() -> None:
    """No env, no file → None. Callers treat None as a no-op state, never an
    error (the hook logs "no ingest token configured; skipping")."""
    assert cfg.load_token() is None


def test_blank_ingest_token_is_not_configured(monkeypatch) -> None:
    monkeypatch.setenv("PROBE_INGEST_TOKEN", "   ")
    assert cfg.load_token() is None
    monkeypatch.delenv("PROBE_INGEST_TOKEN")
    _write_probe_config({"ingest_token": ""})
    assert cfg.load_token() is None


def test_blank_env_ingest_token_falls_through_to_config_file(monkeypatch) -> None:
    """An exported-but-empty PROBE_INGEST_TOKEN must NOT mask a valid config-file
    token. session-start.sh treats empty env as unset and falls through to the
    config file; the daemon must do the same or hook and daemon disagree."""
    _write_probe_config({"ingest_token": "file-token"})
    monkeypatch.setenv("PROBE_INGEST_TOKEN", "")
    assert cfg.load_token() == "file-token"
    monkeypatch.setenv("PROBE_INGEST_TOKEN", "   ")
    assert cfg.load_token() == "file-token"


# --- probe config file path resolution ---------------------------------------


def test_probe_config_path_env_override(monkeypatch, tmp_path: Path) -> None:
    alt = tmp_path / "elsewhere" / "cfg.json"
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(alt))
    assert cfg.probe_config_path() == alt
    alt.parent.mkdir(parents=True)
    alt.write_text(json.dumps({"base_url": "https://alt.example", "ingest_token": "alt-t"}))
    assert cfg.api_base_url() == "https://alt.example"
    assert cfg.load_token() == "alt-t"


def test_probe_config_path_follows_xdg_config_home(monkeypatch, tmp_path: Path) -> None:
    """Without PROBE_CONFIG_PATH, mirror the probe CLI: $XDG_CONFIG_HOME/probe/
    config.json, falling back to ~/.config/probe/config.json."""
    monkeypatch.delenv("PROBE_CONFIG_PATH", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert cfg.probe_config_path() == tmp_path / "probe" / "config.json"
    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert cfg.probe_config_path() == Path.home() / ".config" / "probe" / "config.json"


# --- daemon behavior when unconfigured ---------------------------------------


def _watch_args(tmp_path: Path, session_id: str) -> list[str]:
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}\n")
    return [
        "--session-id", session_id,
        "--transcript", str(transcript),
        "--cwd", str(tmp_path),
        "--plugin-root", str(tmp_path),
    ]


def test_daemon_noops_without_ingest_token(tmp_path: Path) -> None:
    """No token → exit 0 without touching the shutdown sentinel: the wrapper
    keeps its normal lifecycle and nothing errors."""
    from tap.main import main as watch_main

    sid = "baseurl-no-token"
    sentinel = cfg.shutdown_sentinel(sid)
    try:
        assert watch_main(_watch_args(tmp_path, sid)) == 0
        assert not sentinel.exists()
    finally:
        sentinel.unlink(missing_ok=True)


def test_daemon_does_not_require_plugin_root(tmp_path: Path) -> None:
    """--plugin-root is optional: a future hook that stops passing it must NOT
    argparse-exit (SystemExit) the daemon into a silent capture outage. With no
    token configured the daemon cleanly no-ops (returns 0) rather than erroring."""
    from tap.main import main as watch_main

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}\n")
    args = [
        "--session-id", "no-plugin-root",
        "--transcript", str(transcript),
        "--cwd", str(tmp_path),
        # deliberately no --plugin-root
    ]
    assert watch_main(args) == 0


def test_daemon_noops_and_stops_wrapper_when_base_url_unset(
    monkeypatch, tmp_path: Path
) -> None:
    """Token present but no base_url → the daemon logs, touches the shutdown
    sentinel (so the wrapper stops respawning it), and exits 0."""
    from tap.main import main as watch_main

    monkeypatch.setenv("PROBE_INGEST_TOKEN", "ing-test")
    sid = "baseurl-unset"
    sentinel = cfg.shutdown_sentinel(sid)
    try:
        assert watch_main(_watch_args(tmp_path, sid)) == 0
        assert sentinel.exists(), "wrapper sentinel must be touched so it stops respawning"
    finally:
        sentinel.unlink(missing_ok=True)


# --- status must not report an unconfigured install as healthy ----------------


def test_status_reports_not_configured_without_token(capsys) -> None:
    from tap.status import run

    rc = run()
    assert rc == 1
    assert "not configured" in capsys.readouterr().out


def test_status_reports_missing_base_url(monkeypatch, capsys) -> None:
    monkeypatch.setenv("PROBE_INGEST_TOKEN", "ing-test")
    from tap.status import run

    rc = run()
    assert rc == 1
    assert "no backend base URL" in capsys.readouterr().out


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))


# --- v2 named-context config (the CLI's shape as of the workspace-context pass) ---


def test_reads_a_v2_context_config():
    """The tap shares one file with the probe CLI, and the CLI now writes v2.

    Reading only the v1 flat shape meant transcript ingestion stopped silently the
    first time the user ran any command that saved config: no error, just an unset
    base_url and a tap that quietly did nothing.
    """
    _write_probe_config({
        "version": 2,
        "current_context": "default",
        "contexts": {
            "default": {"base_url": "https://file.example", "ingest_token": "ros_ing_X"}
        },
    })
    assert cfg.api_base_url() == "https://file.example"
    assert cfg.load_token() == "ros_ing_X"


def test_v2_reads_the_ACTIVE_context_not_the_first_one():
    _write_probe_config({
        "version": 2,
        "current_context": "staging",
        "contexts": {
            "default": {"base_url": "https://prod.example", "ingest_token": "ros_ing_PROD"},
            "staging": {"base_url": "https://staging.example", "ingest_token": "ros_ing_STG"},
        },
    })
    assert cfg.api_base_url() == "https://staging.example"
    assert cfg.load_token() == "ros_ing_STG"


def test_v1_flat_config_still_works():
    """The migration is lazy — a user who has not run a saving command still has v1."""
    _write_probe_config({"base_url": "https://legacy.example", "ingest_token": "ros_ing_OLD"})
    assert cfg.api_base_url() == "https://legacy.example"
    assert cfg.load_token() == "ros_ing_OLD"
