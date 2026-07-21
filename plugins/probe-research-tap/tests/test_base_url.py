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

import base64
import json
import tempfile
from pathlib import Path

import pytest

from tap import config as cfg


def _make_jwt(payload: dict) -> str:
    """A structurally-valid JWT (unsigned). Only the payload's claims are read
    locally; the signature is never verified by the plugin."""
    def _seg(d: dict) -> str:
        raw = json.dumps(d).encode("utf-8")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{_seg({'alg': 'HS256', 'typ': 'JWT'})}.{_seg(payload)}.sig"


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


# --- base URL derived from a pairing token's `iss` --------------------------


def test_base_url_from_pairing_token_allows_research_host():
    token = _make_jwt({"iss": "api.research.prbe.ai", "aud": "agent-tap"})
    assert cfg.base_url_from_pairing_token(token) == "https://api.research.prbe.ai"


def test_base_url_from_pairing_token_accepts_apex_and_strips_whitespace():
    for iss, expected in [
        ("prbe.ai", "https://prbe.ai"),
        ("api.research.prbe.ai", "https://api.research.prbe.ai"),
        ("https://api.research.prbe.ai/", "https://api.research.prbe.ai"),
        ("  api.research.prbe.ai  ", "https://api.research.prbe.ai"),
    ]:
        assert cfg.base_url_from_pairing_token(_make_jwt({"iss": iss})) == expected


def test_base_url_from_pairing_token_rejects_non_prbe_and_unsafe_hosts():
    """The token is unsigned from our side, so `iss` must be constrained to an
    https Probe host — otherwise a pasted/forged token could pin an attacker
    host as the transcript/bearer upload target."""
    hostile = [
        "evil.com",                              # not a Probe host
        "http://api.research.prbe.ai",           # TLS downgrade
        "https://api.research.prbe.ai@evil.com", # userinfo confusion → real host evil.com
        "https://evil.com",                      # foreign host, valid https
        "https://notprbe.ai",                    # suffix without the dot boundary
        "https://prbe.ai.evil.com",              # lookalike subdomain
        "//evil.com",                            # scheme-relative → empty host
        "ftp://api.research.prbe.ai",            # wrong scheme
    ]
    for iss in hostile:
        with pytest.raises(ValueError):
            cfg.base_url_from_pairing_token(_make_jwt({"iss": iss}))


def test_base_url_from_pairing_token_missing_or_malformed_raises():
    with pytest.raises(ValueError):
        cfg.base_url_from_pairing_token(_make_jwt({"aud": "agent-tap"}))  # no iss
    with pytest.raises(ValueError):
        cfg.base_url_from_pairing_token("not-a-jwt")


def test_pair_base_url_prefers_env_over_token(monkeypatch):
    monkeypatch.setenv("PROBE_BASE_URL", "https://override.example")
    token = _make_jwt({"iss": "api.research.prbe.ai"})
    assert cfg.pair_base_url(token) == "https://override.example"


# --- host pinned at pair time (plugin-local .config) ------------------------


def test_persisted_pair_host_used_by_api_base_url():
    cfg.persist_api_base_url("https://api.research.prbe.ai")
    assert cfg.api_base_url() == "https://api.research.prbe.ai"


def test_env_base_url_beats_pinned_pair_host(monkeypatch):
    cfg.persist_api_base_url("https://pinned.example")
    monkeypatch.setenv("PROBE_BASE_URL", "https://env.example")
    assert cfg.api_base_url() == "https://env.example"


def test_pinned_pair_host_beats_probe_config_base_url():
    _write_probe_config({"base_url": "https://probe-cli.example", "ingest_token": "t"})
    cfg.persist_api_base_url("https://pinned.example")
    assert cfg.api_base_url() == "https://pinned.example"


def test_persist_pair_host_merges_not_clobbers():
    """Pinning the host must not wipe cadence knobs already in .config."""
    cfg.config_file().parent.mkdir(parents=True, exist_ok=True)
    cfg.config_file().write_text(json.dumps({"active_interval_seconds": 42}))
    cfg.persist_api_base_url("https://api.research.prbe.ai")
    data = json.loads(cfg.config_file().read_text())
    assert data["active_interval_seconds"] == 42
    assert data["api_base_url"] == "https://api.research.prbe.ai"


# --- paired .token precedence over the manual/self-host sources -------------


def test_token_file_takes_precedence_over_config_ingest_token():
    """A paired device keeps shipping under its own token even when the probe
    CLI is separately logged in with a different ingest_token."""
    _write_probe_config({"ingest_token": "config-file-token"})
    cfg.write_token("paired-device-token")
    assert cfg.load_token() == "paired-device-token"


def test_token_file_takes_precedence_over_env_ingest_token(monkeypatch):
    monkeypatch.setenv("PROBE_INGEST_TOKEN", "env-token")
    cfg.write_token("paired-device-token")
    assert cfg.load_token() == "paired-device-token"


def test_blank_token_file_falls_through_to_config_ingest_token():
    """An empty/whitespace .token is treated as UNSET, not as "" masking the
    next source — so a corrupt/empty file can't silently disable a self-host
    fallback that is otherwise configured."""
    _write_probe_config({"ingest_token": "config-file-token"})
    cfg.token_file().parent.mkdir(parents=True, exist_ok=True)
    cfg.token_file().write_text("   ")
    assert cfg.load_token() == "config-file-token"
