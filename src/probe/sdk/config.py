"""SDK credential + endpoint resolution.

Precedence (highest first): explicit argument -> environment -> config file.

Env vars:
  PROBE_BASE_URL      e.g. https://api.research.prbe.ai
  PROBE_TOKEN         a user API token (probe_pat_...) for /v1
  PROBE_MCP_TOKEN     a read-only token for the MCP surface (see mcp_token below)
  PROBE_INGEST_TOKEN  an ingest token (ros_ing_...) for /ingest
  PROBE_HMAC_SECRET   optional shared secret for the X-Signature body HMAC on /ingest

Config file: $XDG_CONFIG_HOME/probe/config.json (default ~/.config/probe/config.json),
written by ``probe login``. ``probe login --device`` captures the token via the browser
handoff; ``probe login --token`` is the air-gap-friendly paste path.

``mcp_token`` is deliberately a separate credential from ``token``: the MCP surface is
read-only, so it holds a ``scopes:['read']`` token that cannot write even if it leaks
(it is handed to an MCP client, which is a wider blast radius than the CLI). Nothing
falls back from one to the other — ``probe mcp token set`` writes it.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://api.research.prbe.ai"


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "probe" / "config.json"


def load_file() -> dict:
    path = config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_file(data: dict) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a complete file then swap it in: a crash mid-write would otherwise leave
    # truncated JSON, which load_file() reads as {} — silently losing every credential.
    # The temp file is created 0600 and lives in the target dir so os.replace is atomic.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    # tokens live here; keep it user-only.
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def clear_file() -> None:
    path = config_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


@dataclass
class Settings:
    base_url: str
    token: str | None = None
    mcp_token: str | None = None
    ingest_token: str | None = None
    hmac_secret: str | None = None


def resolve(
    *,
    base_url: str | None = None,
    token: str | None = None,
    mcp_token: str | None = None,
    ingest_token: str | None = None,
    hmac_secret: str | None = None,
) -> Settings:
    """Merge explicit args, env, and the config file into one Settings object."""
    file = load_file()
    return Settings(
        base_url=(
            base_url
            or os.environ.get("PROBE_BASE_URL")
            or file.get("base_url")
            or DEFAULT_BASE_URL
        ).rstrip("/"),
        token=token or os.environ.get("PROBE_TOKEN") or file.get("token"),
        # Env first keeps every shell that already exports PROBE_MCP_TOKEN working
        # unchanged. Never falls back to `token`: that one can write.
        mcp_token=mcp_token or os.environ.get("PROBE_MCP_TOKEN") or file.get("mcp_token"),
        ingest_token=(
            ingest_token or os.environ.get("PROBE_INGEST_TOKEN") or file.get("ingest_token")
        ),
        hmac_secret=(
            hmac_secret or os.environ.get("PROBE_HMAC_SECRET") or file.get("hmac_secret")
        ),
    )
