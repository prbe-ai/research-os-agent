"""Probe's install names: plugin ids, marketplace, mirror repo, MCP key, rules-block markers. ONE file, read everywhere.

Two identities live in ``install_names.json`` beside this file: ``old`` (``probe-research@research-os-agent``) and
``new`` (``probe@probe``). Release N (the bridge) installs ``new`` on a machine that carries nothing of ``old`` and
keeps ``old`` wherever it is installed; every reader accepts both. Which one an agent on a machine uses is asked in
one place (``probe.shared.harness.install_identity``), never here.

STDLIB ONLY, PYTHON 3.9, COPIED BYTE FOR BYTE by ``make -C agent sync-install-names`` (the plugin hooks and Kimi's
``_kimi_live.py`` run without ``probe``); each copy reads the JSON beside it. ``agent/tests/test_install_names.py``
fails when a copy drifts. Edit ``contracts/probe_contracts/harness/install_names.{py,json}``, never a copy.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: The file this module reads when no path is given: the copy beside it.
NAMES_FILE = Path(__file__).with_name("install_names.json")
OLD = "old"
NEW = "new"
_ROLES = ("tracking_plugin", "daemon_plugin", "tap_plugin", "shim_plugin")


@dataclass(frozen=True)
class Identity:
    """One complete set of install names: what a plugin, its marketplace and its MCP are called."""

    key: str
    marketplace: str
    repo: str
    tracking_plugin: str
    daemon_plugin: str
    tap_plugin: str
    shim_plugin: str
    mcp_key: str
    setup_command: str
    rules_begin: str
    rules_end: str
    #: Exact tool-name prefixes of this identity's MCP under every agent.
    mcp_tool_prefixes: tuple[str, ...]
    #: Bare-key prefixes (`mcp__probe__`), this identity's only where the agent names MCP tools by their server
    #: key alone (Codex, whose MCP lives in config.toml): elsewhere another product's MCP may carry that key.
    bare_key_mcp_tool_prefixes: tuple[str, ...]

    @property
    def plugins(self) -> tuple[str, ...]:
        """The plugins that ship from this identity's marketplace (the frozen shim included)."""
        return (self.tracking_plugin, self.daemon_plugin, self.tap_plugin, self.shim_plugin)

    @property
    def rules_begin_prefix(self) -> str:
        """The begin marker without its note: what a reader matches (`<!-- probe-research:begin`)."""
        return self.rules_begin.split(" (", 1)[0]

    def make_plugin_id(self, name: str) -> str:
        """`<name>@<marketplace>` of this identity."""
        return f"{name}@{self.marketplace}"


@dataclass(frozen=True)
class InstallNames:
    """Both identities, and which one's rules-block markers a writer writes."""

    old: Identity
    new: Identity
    markers_written: Identity

    def get_all(self) -> tuple[Identity, ...]:
        return (self.old, self.new)

    def find_plugin(self, name: object) -> Identity | None:
        """The identity whose plugin is called `name` (the shared shim: old), else None."""
        for identity in self.get_all():
            if name in identity.plugins:
                return identity
        return None

    def find_marketplace(self, marketplace: object) -> Identity | None:
        for identity in self.get_all():
            if marketplace == identity.marketplace:
                return identity
        return None

    def is_probe_plugin_id(self, plugin_id: object) -> bool:
        """Is `<plugin>@<marketplace>` one of Probe's, under either identity (never a mix)?"""
        if not isinstance(plugin_id, str):
            return False
        name, _, marketplace = plugin_id.partition("@")
        identity = self.find_marketplace(marketplace)
        return identity is not None and name in identity.plugins

    def get_new_name(self, name: str) -> str:
        """An old plugin name's new one (`probe-research-tap` -> `probe-tap`); any other name unchanged."""
        return self.get_name_in(name, self.new)

    def get_name_in(self, name: str, identity: Identity) -> str:
        """A Probe plugin name (either identity's) as `identity` spells it; any other name unchanged."""
        for role in _ROLES:
            for each in self.get_all():
                if getattr(each, role) == name:
                    return getattr(identity, role)
        return name

    def get_rules_begin_prefixes(self) -> tuple[str, ...]:
        return tuple(identity.rules_begin_prefix for identity in self.get_all())

    def get_mcp_tool_prefixes(self, is_bare_key: bool = False) -> tuple[str, ...]:
        """Every exact prefix a Probe MCP tool name starts with; `is_bare_key`: the agent names tools by server key."""
        found = []
        for identity in self.get_all():
            found += identity.mcp_tool_prefixes
            if is_bare_key:
                found += identity.bare_key_mcp_tool_prefixes
        return tuple(found)

    def is_probe_mcp_tool(self, tool_name: object, is_bare_key: bool = False) -> bool:
        """Is this tool one of Probe's MCP tools (exact prefixes, so another product's `probe_x` is not)?"""
        return isinstance(tool_name, str) and tool_name.startswith(self.get_mcp_tool_prefixes(is_bare_key))


def _make_identity(key: str, row: dict) -> Identity:
    plugins = row["plugins"]
    return Identity(
        key=key,
        marketplace=row["marketplace"],
        repo=row["repo"],
        tracking_plugin=plugins["tracking"],
        daemon_plugin=plugins["daemon"],
        tap_plugin=plugins["tap"],
        shim_plugin=plugins["shim"],
        mcp_key=row["mcp_key"],
        setup_command=row["setup_command"],
        rules_begin=row["rules_begin"],
        rules_end=row["rules_end"],
        mcp_tool_prefixes=tuple(row["mcp_tool_prefixes"]),
        bare_key_mcp_tool_prefixes=tuple(row["bare_key_mcp_tool_prefixes"]),
    )


def load(path: Path | None = None) -> InstallNames:
    """Both identities from `path` (default: the JSON beside this file)."""
    document = json.loads((path or NAMES_FILE).read_text(encoding="utf-8"))
    rows = document["identities"]
    old, new = _make_identity(OLD, rows[OLD]), _make_identity(NEW, rows[NEW])
    written = old if document["markers_written"] == OLD else new
    return InstallNames(old=old, new=new, markers_written=written)


_cached: InstallNames | None = None


def get_install_names() -> InstallNames:
    """`load()`, read once per process."""
    global _cached
    if _cached is None:
        _cached = load()
    return _cached
