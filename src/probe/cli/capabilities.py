"""What is actually installed and switched on for this device.

ONE state struct with TWO renderings: `probe wizard` shows it as a menu with
toggles, `probe doctor` prints it as a diagnostic. They must never disagree,
which is why neither computes state of its own.

Everything here is READ-ONLY and fail-soft. A machine mid-install, offline, or
without Claude Code still produces a complete `Capabilities` -- fields go
False/None rather than raising, because both callers need to render *something*
and a diagnostic that crashes is worse than one reporting "unknown".

stdlib only, and imported lazily by cli/main.py: `probe log` runs inside
training loops, and this module must never become a reason for that to get
slower.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

TAP_PLUGIN_NAME = "probe-research-tap"
TRACKING_PLUGIN_NAME = "probe-research"
MARKETPLACE = "research-os-agent"
MARKETPLACE_REPO = "prbe-ai/research-os-agent"
#: PyPI distribution. NOT `probe-agent` -- that name belongs to an unrelated
#: project already on PyPI, so installing it fetches a stranger's package.
AGENT_INSTALL = "probe-research"
PLUGIN_ID = f"{TRACKING_PLUGIN_NAME}@{MARKETPLACE}"
TAP_PLUGIN_ID = f"{TAP_PLUGIN_NAME}@{MARKETPLACE}"

ENV_INGEST_TOKEN = "PROBE_INGEST_TOKEN"
ENV_TAP_PLUGIN_DIR = "PROBE_RESEARCH_TAP_PLUGIN_DIR"
ENV_CONFIG_PATH = "PROBE_CONFIG_PATH"

_CLAUDE_TIMEOUT_S = 20.0


class Capability(StrEnum):
    """A row in the menu. Deliberately NOT plugin names -- nobody knows what
    `probe-research-tap` is, and the consent decision is about what the thing
    does, not what it is called."""

    TRACKING = "tracking"
    """Experiment tracking skills + the read-only MCP search surface."""

    CAPTURE = "capture"
    """Streams this device's Claude Code sessions to the knowledgebase."""

    AUTO_UPDATE = "auto_update"
    """Keeps the CLI and plugins current in the background."""


class TokenSource(StrEnum):
    """WHERE a capture credential resolves from.

    This exists because "turn capture off" is not the same as "delete the paired
    token file". The uploader accepts three sources, and clearing only the first
    lets capture silently resume at the next session start while the menu
    reports it as off. ENV is the one the wizard cannot fix by itself -- it
    cannot unset a variable in the parent shell -- so it has to say so.
    """

    PAIRED_FILE = "paired_file"
    ENVIRONMENT = "environment"
    PROBE_CONFIG = "probe_config"


@dataclass(frozen=True)
class Capabilities:
    """A complete, fail-soft snapshot of this device."""

    cli_version: str | None = None
    install_method: str | None = None
    claude_available: bool = False

    logged_in_as: str | None = None
    base_url: str | None = None

    tracking_plugin_installed: bool = False
    capture_plugin_installed: bool = False

    capture_token_sources: tuple[TokenSource, ...] = ()
    capture_killswitched: bool = False
    capture_device_id: str | None = None

    auto_update_enabled: bool = False
    last_update_attempt: str | None = None

    warnings: list[str] = field(default_factory=list)

    @property
    def capture_on(self) -> bool:
        """Capture ships only if a credential resolves AND the killswitch is off.

        Both halves matter: a paired device with `.disabled` present sends
        nothing, and a machine with no credential sends nothing regardless of
        which plugins are installed.
        """
        return bool(self.capture_token_sources) and not self.capture_killswitched

    @property
    def tracking_on(self) -> bool:
        return self.tracking_plugin_installed and self.logged_in_as is not None

    @property
    def configured(self) -> bool:
        """Whether this device has been set up before.

        Deliberately NOT `any(enabled)`. Someone who ran setup and turned
        everything OFF has still configured this machine, and treating that as
        fresh would let `probe wizard --yes` silently switch tracking and
        auto-update back on from the defaults. Evidence of installation is the
        right signal, not evidence of anything being enabled.
        """
        return any(
            (
                self.tracking_plugin_installed,
                self.capture_plugin_installed,
                self.logged_in_as is not None,
                bool(self.capture_token_sources),
                self.capture_killswitched,
                self.auto_update_enabled,
                self.last_update_attempt is not None,
            )
        )

    def enabled(self) -> dict[Capability, bool]:
        return {
            Capability.TRACKING: self.tracking_on,
            Capability.CAPTURE: self.capture_on,
            Capability.AUTO_UPDATE: self.auto_update_enabled,
        }


def tap_plugin_dir() -> Path:
    env = os.environ.get(ENV_TAP_PLUGIN_DIR)
    if env:
        return Path(env)
    return Path.home() / ".claude" / "plugins" / TAP_PLUGIN_NAME


def probe_config_path() -> Path:
    env = os.environ.get(ENV_CONFIG_PATH)
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "probe" / "config.json"


def _read_json(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def probe_config_credentials() -> dict:
    """The probe CLI config flattened the way the UPLOADER reads it.

    This MUST mirror `tap/config.py:_read_probe_config()` exactly. The CLI writes
    v2 (named contexts) as of the workspace-context pass, and when `contexts`
    exists the uploader reads ONLY the active context -- it does not fall back to
    the top level.

    Reading just the top-level key would therefore miss the credential on every
    modern config, and this function backs the off switch: a miss means we clear
    nothing, re-verify nothing, and report "capture is off" while it keeps
    shipping. The parity test in tests/test_setup_wizard.py guards the pairing.
    """
    raw = _read_json(probe_config_path())
    contexts = raw.get("contexts")
    if isinstance(contexts, dict):
        active = contexts.get(raw.get("current_context") or "default")
        return active if isinstance(active, dict) else {}
    return raw


def capture_token_sources() -> tuple[TokenSource, ...]:
    """Every place a capture credential currently resolves from, in the
    uploader's own precedence order.

    Mirrors `tap/config.py:load_token()`. It is duplicated rather than imported
    because the tap is a separate plugin package living in Claude Code's plugin
    cache -- the CLI cannot import it. Any change there must change here, which
    is what the parity test in tests/test_capabilities.py exists to catch.
    """
    found: list[TokenSource] = []
    token_file = tap_plugin_dir() / ".token"
    try:
        if token_file.read_text().strip():
            found.append(TokenSource.PAIRED_FILE)
    except OSError:
        pass
    if (os.environ.get(ENV_INGEST_TOKEN) or "").strip():
        found.append(TokenSource.ENVIRONMENT)
    if str(probe_config_credentials().get("ingest_token") or "").strip():
        found.append(TokenSource.PROBE_CONFIG)
    return tuple(found)


def installed_plugins(*, claude_bin: str | None = None) -> set[str]:
    """Plugin names Claude Code reports as installed. Empty when `claude` is
    absent, which is normal on a GPU pod and must not be an error."""
    binary = claude_bin or shutil.which("claude")
    if not binary:
        return set()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, no shell
            [binary, "plugin", "list"],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return set()
    names: set[str] = set()
    for line in completed.stdout.splitlines():
        # `probe-research` is a PREFIX of `probe-research-tap`, so a line naming
        # the tap contains both. Longest match wins per line, otherwise having
        # only the tap installed would read as tracking being on too.
        if TAP_PLUGIN_NAME in line:
            names.add(TAP_PLUGIN_NAME)
        elif TRACKING_PLUGIN_NAME in line:
            names.add(TRACKING_PLUGIN_NAME)
    return names


def capture_device_id() -> str | None:
    """The paired device id the uploader recorded, if any.

    Read straight from the tap's SQLite state rather than shelling out to it:
    `probe doctor` must work when the plugin's Python cannot run at all, which
    is exactly the situation someone runs a doctor command in.
    """
    db = tap_plugin_dir() / "state.db"
    if not db.exists():
        return None
    import sqlite3

    try:
        with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
            row = conn.execute("SELECT v FROM meta WHERE k = 'device_id'").fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row and row[0] else None
