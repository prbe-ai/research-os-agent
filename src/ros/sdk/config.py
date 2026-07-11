"""SDK credential + endpoint resolution.

Precedence (highest first): explicit argument -> environment -> config file.

Env vars:
  ROS_BASE_URL        e.g. https://api.research.prbe.ai
  ROS_TOKEN           a user API token (ros_pat_...) for /v1
  ROS_INGEST_TOKEN    an ingest token (ros_ing_...) for /ingest
  ROS_HMAC_SECRET     optional shared secret for the X-Signature body HMAC on /ingest

Config file: $XDG_CONFIG_HOME/ros/config.json (default ~/.config/ros/config.json),
written by ``exp login``. This is the air-gap-friendly paste-token path; a device
flow is future work (see research-os TODOS).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://api.research.prbe.ai"


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "ros" / "config.json"


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
    path.write_text(json.dumps(data, indent=2, sort_keys=True))
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
    ingest_token: str | None = None
    hmac_secret: str | None = None


def resolve(
    *,
    base_url: str | None = None,
    token: str | None = None,
    ingest_token: str | None = None,
    hmac_secret: str | None = None,
) -> Settings:
    """Merge explicit args, env, and the config file into one Settings object."""
    file = load_file()
    return Settings(
        base_url=(
            base_url
            or os.environ.get("ROS_BASE_URL")
            or file.get("base_url")
            or DEFAULT_BASE_URL
        ).rstrip("/"),
        token=token or os.environ.get("ROS_TOKEN") or file.get("token"),
        ingest_token=(
            ingest_token or os.environ.get("ROS_INGEST_TOKEN") or file.get("ingest_token")
        ),
        hmac_secret=(
            hmac_secret or os.environ.get("ROS_HMAC_SECRET") or file.get("hmac_secret")
        ),
    )
