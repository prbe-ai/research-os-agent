"""Plugin configuration: paths, env, sync intervals, killswitch.

State paths derive from PROBE_RESEARCH_TAP_PLUGIN_DIR (env override) or
~/.claude/plugins/probe-research-tap/ so hooks and the daemon agree
without coordination.

Credentials resolve two ways, checked in this order (per field):

  ingest token:  plugin-local .token (written by `tap pair`)
                 >  PROBE_INGEST_TOKEN env
                 >  config.json `ingest_token`
  base URL:      PROBE_BASE_URL env
                 >  plugin-local .config `api_base_url` (pinned by `tap pair`)
                 >  config.json `base_url`

The primary path is device pairing: `python -m tap pair <token>` exchanges a
dashboard-minted pairing token for a device token (written to .token) and pins
the backend host — read from the token's unverified `iss` claim, host-allowed
to *.prbe.ai — into .config. The manual/self-host path is the probe CLI's file,
config.json = $XDG_CONFIG_HOME/probe/config.json (default
~/.config/probe/config.json), written by `probe login`; a PROBE_CONFIG_PATH env
override points at an alternate file (tests, dev). Either path works; a paired
.token is preferred so a paired device keeps shipping even when the probe CLI is
separately logged in with a different ingest token.

There is deliberately NO baked-in default host. A missing base URL raises
APIBaseURLUnset so the daemon stops cleanly instead of shipping to a guessed
host; a missing ingest token means "not configured" and the daemon/hook no-op.

Cadence model: the daemon runs adaptively. While the transcript is
advancing it ticks at the active interval (default 60s); after two
consecutive empty ticks it slows to the idle interval (default 300s)
to reduce backend load on idle CC sessions. A single legacy knob
(sync_interval_seconds) overrides both — set it if you want a flat
cadence with no adaptive switching.
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PLUGIN_NAME = "probe-research-tap"

DEFAULT_ACTIVE_INTERVAL_SECONDS = 60
DEFAULT_IDLE_INTERVAL_SECONDS = 300

WEBHOOK_PATH = "/ingest/v1/sessions/claude-code"
PAIR_PATH = "/agent-tap/pair"
REVOKE_PATH = "/agent-tap/revoke"

# Env overrides for the credential pair. There is deliberately NO hardcoded
# base-URL fallback: a baked-in default is exactly what silently broke the
# hosted plugin's ingestion when its backend moved hosts — every tick kept
# hitting a dead host and failing with a cryptic DNS error instead of saying
# "not configured". See api_base_url().
ENV_BASE_URL = "PROBE_BASE_URL"
ENV_INGEST_TOKEN = "PROBE_INGEST_TOKEN"
ENV_CONFIG_PATH = "PROBE_CONFIG_PATH"

# Key under which `tap pair` pins the backend host into the plugin-local .config
# (distinct file from the probe CLI's config.json). Read back by api_base_url()
# so the daemon and `tap revoke` reach the same backend the pairing used.
CONFIG_API_BASE_URL_KEY = "api_base_url"

# The pairing token's `iss` is UNSIGNED from the plugin's side (we hold no
# key), so a pasted token could otherwise name any host as the upload target.
# Constrain the token-derived host to https + a Probe-owned domain. The
# dashboard mints pairing tokens with an `iss` like `api.research.prbe.ai`.
# Self-hosted/dev backends use the PROBE_BASE_URL env override instead, which is
# an explicit local choice and not gated here.
ALLOWED_HOST_EXACT = "prbe.ai"
ALLOWED_HOST_SUFFIX = ".prbe.ai"


def plugin_dir() -> Path:
    env = os.environ.get("PROBE_RESEARCH_TAP_PLUGIN_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "plugins" / PLUGIN_NAME


def config_file() -> Path:
    """Plugin-local .config — cadence knobs + the host pinned at pair time.

    Never holds the token itself (that lives in .token / the probe CLI config).
    """
    return plugin_dir() / ".config"


def token_file() -> Path:
    """Plugin-local device token written by `python -m tap pair`."""
    return plugin_dir() / ".token"


def disabled_file() -> Path:
    return plugin_dir() / ".disabled"


def disabled_paths_file() -> Path:
    return plugin_dir() / ".disabled_paths"


def state_db_path() -> Path:
    return plugin_dir() / "state.db"


def log_dir() -> Path:
    return plugin_dir() / "logs"


def shutdown_sentinel(session_id: str) -> Path:
    return Path("/tmp") / f"probe-research-tap-watcher-{session_id}.shutdown"


class APIBaseURLUnset(RuntimeError):
    """No backend host is configured.

    Raised instead of falling back to a hardcoded host. There is no baked-in
    default by design — the host comes from the probe CLI's config file
    (written by `probe login`) or an explicit env override, and if neither is
    present we fail loudly rather than silently ship to a guessed URL.
    """


def probe_config_path() -> Path:
    """Path of the probe CLI's config file.

    PROBE_CONFIG_PATH env override (tests/dev) > $XDG_CONFIG_HOME/probe/config.json
    > ~/.config/probe/config.json. Mirrors the probe CLI's own resolution so
    `probe login` and this plugin always agree on the file.
    """
    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        return Path(env)
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "probe" / "config.json"


def _read_probe_config() -> dict[str, Any]:
    """The probe CLI's credentials, flattened to one dict whatever the file shape.

    The CLI writes v2 (named contexts) as of the workspace-context pass; a v1 file
    is a flat credential blob. This plugin must read BOTH: it shares one file with
    the CLI, and reading only v1 would mean transcript ingestion silently stopped
    the first time the user ran any command that saved config — no error, just an
    unset base_url and a tap that quietly does nothing.
    """
    p = probe_config_path()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    contexts = data.get("contexts")
    if isinstance(contexts, dict):
        active = contexts.get(data.get("current_context") or "default")
        return active if isinstance(active, dict) else {}
    return data


def _env_base_url() -> str | None:
    v = os.environ.get(ENV_BASE_URL, "").strip()
    return v.rstrip("/") if v else None


def api_base_url() -> str:
    """Resolve the backend base URL: env > host pinned at pair > probe CLI config.

    No hardcoded fallback — raises APIBaseURLUnset when unconfigured.
    """
    env = _env_base_url()
    if env:
        return env
    # Host pinned by `tap pair` into the plugin-local .config, so the daemon and
    # `tap revoke` reach the same backend the pairing used without a token.
    pinned = _read_config_dict().get(CONFIG_API_BASE_URL_KEY)
    if isinstance(pinned, str) and pinned.strip():
        return pinned.strip().rstrip("/")
    # Manual/self-host path: the probe CLI's config file (written by `probe login`).
    persisted = _read_probe_config().get("base_url")
    if isinstance(persisted, str) and persisted.strip():
        return persisted.strip().rstrip("/")
    raise APIBaseURLUnset(
        "no backend base URL configured — pair this device with "
        "`python -m tap pair <token>` (the host is read from the token), run "
        f"`probe login`, or set {ENV_BASE_URL}"
    )


def _jwt_claim(token: str, claim: str) -> str | None:
    """Best-effort read of a string claim from an unverified JWT payload.

    We don't hold the signing key, so we don't verify — the server verifies the
    signature when we POST /agent-tap/pair, and a forged host just makes pairing
    fail. Returns None for anything that isn't a well-formed JWT carrying a
    non-empty string `claim`.
    """
    parts = token.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    val = payload.get(claim) if isinstance(payload, dict) else None
    return val if isinstance(val, str) and val.strip() else None


def base_url_from_pairing_token(pairing_token: str) -> str:
    """Derive and validate the backend base URL from a pairing JWT's `iss`.

    The dashboard mints the pairing token with `iss` set to the backend host
    (e.g. `api.research.prbe.ai`). Reading it here lets the plugin follow the
    backend across domain moves with no hardcoded host and no plugin update.

    The token is UNSIGNED from our side, so `iss` is attacker-controllable when
    a user pastes a forged token. We require https + a Probe-owned host before
    it can become the upload target — otherwise a pasted token could pin an
    arbitrary host and harvest the device bearer and transcripts (the re-pair
    path would even POST the user's existing bearer to it). Self-hosted/dev
    backends use the PROBE_BASE_URL env override, which is not gated here.
    """
    iss = _jwt_claim(pairing_token, "iss")
    if not iss:
        raise ValueError(
            "pairing token carries no `iss` host claim; cannot determine the "
            "backend host (request a fresh token from the dashboard)"
        )
    url = iss.strip()
    if "://" not in url:
        url = "https://" + url
    url = url.rstrip("/")
    parts = urlsplit(url)
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or "@" in parts.netloc
        or not (host == ALLOWED_HOST_EXACT or host.endswith(ALLOWED_HOST_SUFFIX))
    ):
        raise ValueError(
            f"pairing token `iss` ({iss!r}) is not an allowed Probe backend; "
            f"expected an https://*.{ALLOWED_HOST_EXACT} host. For a self-hosted "
            f"or dev backend, set {ENV_BASE_URL} instead"
        )
    return url


def pair_base_url(pairing_token: str) -> str:
    """Base URL for the pair request: env override > token `iss`."""
    return _env_base_url() or base_url_from_pairing_token(pairing_token)


def persist_api_base_url(url: str) -> None:
    """Pin the resolved base URL into the plugin-local .config so the daemon and
    `tap revoke` reach the same backend the pairing used (merging, not
    clobbering cadence knobs already there)."""
    data = _read_config_dict()
    data[CONFIG_API_BASE_URL_KEY] = url.rstrip("/")
    p = config_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, p)


def load_token() -> str | None:
    """Resolve the bearer token: paired .token > env override > probe CLI config.

    The paired device token (plugin-local .token, written by `tap pair`) is the
    primary path and takes precedence so a paired device keeps shipping even
    when the probe CLI is separately logged in with a different ingest token.
    The manual/self-host fallback is PROBE_INGEST_TOKEN env > config.json.

    Returns None when unconfigured — callers treat that as a no-op state
    ("not configured"), never an error.

    An empty/whitespace .token or an exported-but-empty/whitespace
    PROBE_INGEST_TOKEN is treated as UNSET (fall through), NOT as "" masking the
    next source. session-start.sh's token gate mirrors this precedence so the
    hook and daemon always agree on whether a token is configured.
    """
    p = token_file()
    if p.is_file():
        try:
            t = p.read_text(encoding="utf-8").strip()
            if t:
                return t
        except OSError:
            pass
    env = os.environ.get(ENV_INGEST_TOKEN, "").strip()
    if env:
        return env
    tok = _read_probe_config().get("ingest_token")
    if isinstance(tok, str) and tok.strip():
        return tok.strip()
    return None


def write_token(token: str) -> None:
    """Atomic write of the device bearer token at mode 0600."""
    p = token_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(token, encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, p)


def _parse_positive_int(value: Any) -> int | None:
    """Best-effort positive int. Returns None for missing / unparseable / <= 0."""
    if value is None:
        return None
    try:
        n = int(str(value))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _read_config_dict() -> dict[str, Any]:
    p = config_file()
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def intervals() -> tuple[int, int]:
    """Return (active_seconds, idle_seconds).

    Resolution order, per knob: env > .config > default.

    Legacy single-knob escape hatch: PROBE_RESEARCH_TAP_INTERVAL_SECONDS (env)
    or `sync_interval_seconds` (config) — if set, applies to BOTH active and
    idle. For users who want flat cadence with no adaptive switching.

    Idle is clamped to >= active so we never accidentally tick faster when
    the user thinks they've slowed us down.
    """
    config_data = _read_config_dict()

    # Legacy override path — flat cadence.
    legacy_env = _parse_positive_int(os.environ.get("PROBE_RESEARCH_TAP_INTERVAL_SECONDS"))
    if legacy_env is not None:
        return legacy_env, legacy_env
    legacy_cfg = _parse_positive_int(config_data.get("sync_interval_seconds"))
    if legacy_cfg is not None:
        return legacy_cfg, legacy_cfg

    # Adaptive path.
    active = (
        _parse_positive_int(os.environ.get("PROBE_RESEARCH_TAP_ACTIVE_INTERVAL_SECONDS"))
        or _parse_positive_int(config_data.get("active_interval_seconds"))
        or DEFAULT_ACTIVE_INTERVAL_SECONDS
    )
    idle = (
        _parse_positive_int(os.environ.get("PROBE_RESEARCH_TAP_IDLE_INTERVAL_SECONDS"))
        or _parse_positive_int(config_data.get("idle_interval_seconds"))
        or DEFAULT_IDLE_INTERVAL_SECONDS
    )
    if idle < active:
        idle = active
    return active, idle


def killswitch_active() -> bool:
    return disabled_file().exists()


def cwd_disabled(cwd: Path) -> bool:
    p = disabled_paths_file()
    if not p.is_file():
        return False
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    cwd_str = str(cwd)
    for line in lines:
        prefix = line.strip()
        if prefix and cwd_str.startswith(prefix):
            return True
    return False


@dataclass(frozen=True)
class WatchConfig:
    session_id: str
    transcript_path: Path
    cwd: Path
    # Carried through from --plugin-root but unused by the daemon; optional so a
    # hook that stops passing it (None) cannot crash the daemon. See main.py.
    plugin_root: Path | None
    token: str
    active_interval_s: int
    idle_interval_s: int

    @property
    def shutdown_sentinel(self) -> Path:
        return shutdown_sentinel(self.session_id)
