"""Plugin configuration: paths, env, sync intervals, killswitch.

State paths derive from PROBE_RESEARCH_TAP_PLUGIN_DIR (env override) or
~/.claude/plugins/probe-research-tap/ so hooks and the daemon agree
without coordination.

Credentials come from the probe CLI, not from any plugin-local file.
Resolution order (per field):

  ingest token:  PROBE_INGEST_TOKEN env  >  config.json `ingest_token`
  base URL:      PROBE_BASE_URL env      >  config.json `base_url`

where config.json is the probe CLI's file — $XDG_CONFIG_HOME/probe/config.json
(default ~/.config/probe/config.json), written by `probe login`. A
PROBE_CONFIG_PATH env override points at an alternate file (tests, dev).
There are deliberately NO other fallbacks: no plugin-local .token file, no
baked-in default host. A missing base URL raises APIBaseURLUnset so the
daemon stops cleanly instead of shipping to a guessed host; a missing
ingest token means "not configured" and the daemon/hook no-op.

Cadence model: the daemon runs adaptively. While the transcript is
advancing it ticks at the active interval (default 60s); after two
consecutive empty ticks it slows to the idle interval (default 300s)
to reduce backend load on idle CC sessions. A single legacy knob
(sync_interval_seconds) overrides both — set it if you want a flat
cadence with no adaptive switching.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PLUGIN_NAME = "probe-research-tap"

DEFAULT_ACTIVE_INTERVAL_SECONDS = 60
DEFAULT_IDLE_INTERVAL_SECONDS = 300

WEBHOOK_PATH = "/ingest/v1/sessions/claude-code"

# Env overrides for the credential pair. There is deliberately NO hardcoded
# base-URL fallback: a baked-in default is exactly what silently broke the
# hosted plugin's ingestion when its backend moved hosts — every tick kept
# hitting a dead host and failing with a cryptic DNS error instead of saying
# "not configured". See api_base_url().
ENV_BASE_URL = "PROBE_BASE_URL"
ENV_INGEST_TOKEN = "PROBE_INGEST_TOKEN"
ENV_CONFIG_PATH = "PROBE_CONFIG_PATH"


def plugin_dir() -> Path:
    env = os.environ.get("PROBE_RESEARCH_TAP_PLUGIN_DIR")
    if env:
        return Path(env)
    return Path.home() / ".claude" / "plugins" / PLUGIN_NAME


def config_file() -> Path:
    """Plugin-local .config — cadence knobs only, never credentials."""
    return plugin_dir() / ".config"


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


def api_base_url() -> str:
    """Resolve the backend base URL: env override > probe CLI config file.

    No hardcoded fallback — raises APIBaseURLUnset when unconfigured.
    """
    env = os.environ.get(ENV_BASE_URL, "").strip()
    if env:
        return env.rstrip("/")
    persisted = _read_probe_config().get("base_url")
    if isinstance(persisted, str) and persisted.strip():
        return persisted.strip().rstrip("/")
    raise APIBaseURLUnset(
        "no backend base URL configured — run `probe login` (writes base_url to "
        f"{probe_config_path()}) or set {ENV_BASE_URL}"
    )


def load_token() -> str | None:
    """Resolve the ingest token: env override > probe CLI config file.

    Returns None when unconfigured — callers treat that as a no-op state
    ("not configured"), never an error.

    An exported-but-empty/whitespace PROBE_INGEST_TOKEN is treated as UNSET
    (fall through to the config file), NOT as "" masking a valid file token.
    session-start.sh does the same (`[ -z "$PROBE_INGEST_TOKEN" ]` falls
    through), and this mirrors the probe CLI's own `env or file` precedence —
    so hook and daemon always agree on whether a token is configured.
    """
    env = os.environ.get(ENV_INGEST_TOKEN, "").strip()
    if env:
        return env
    tok = _read_probe_config().get("ingest_token")
    if isinstance(tok, str) and tok.strip():
        return tok.strip()
    return None


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
