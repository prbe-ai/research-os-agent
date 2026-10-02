"""The coding-agent harnesses Probe integrates with: ONE list, read everywhere.

Every fact that differs between Claude Code, Codex, pi (and whatever comes
next) lives in ``harnesses.json`` beside this file: ids, labels, env markers,
home and transcript locations, capture settings, plugin conventions,
instruction files. Code that needs such a fact asks this module for the row
instead of writing ``"codex" if ... else "claude_code"``; behaviour that truly
differs lives in a per-harness adapter, found through the row.

STDLIB ONLY, AND COPIED BYTE FOR BYTE. The plugin hooks, the capture tap and
the server cannot import ``probe`` (they ship apart from the CLI), so this file
and the JSON are copied next to each consumer by ``make sync-harnesses``:

    agent/src/probe/harness/{registry.py,harnesses.json}     (source)
    agent/plugins/probe-research/hooks/{_harness_registry.py,harnesses.json}
    agent/plugins/probe-research-daemon/hooks/{_harness_registry.py,harnesses.json}
    agent/plugins/probe-research-tap/tap/{harness_registry.py,harnesses.json}
    agent/src/probe/tap_core/{harness_registry.py,harnesses.json}
    app/ingestion/{harness_registry.py,harnesses.json}
    dashboard/src/lib/harnesses.json, agent/plugins/probe-research-pi/src/harnesses.json

``agent/tests/test_harness_registry.py`` fails when any copy drifts. Each copy
reads the JSON that sits next to it, so no consumer needs another's path.

Adding a harness: ``agent/docs/adding-a-harness.md``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The file this module reads when no path is given: the copy beside it.
REGISTRY_FILE = Path(__file__).with_name("harnesses.json")

#: How a harness is integrated. Core code branches on these, never on an id.
FAMILY_HOOK_PLUGIN = "hook-plugin"  # a plugin with hooks, skills and MCP (Claude Code, Codex)
FAMILY_EXTENSION = "extension"  # a package whose extension calls the probe CLI (pi)
FAMILY_DETECT_ONLY = "detect-only"  # recognised in the environment, never integrated (Cursor)
FAMILIES = (FAMILY_HOOK_PLUGIN, FAMILY_EXTENSION, FAMILY_DETECT_ONLY)

#: What a hook or the extension can do inside the harness (mirrors the daemon
#: adapters' Capabilities.inject_line / .wake; the conformance test pins them).
CAPABILITY_PROMPT_CONTEXT = "prompt_context"  # add text to the next prompt from a hook
CAPABILITY_WAKE = "wake"  # start a turn for a late answer

#: How a transcript's filename yields its session id.
SESSION_ID_STEM = "stem"  # the whole stem is the id
SESSION_ID_UUID_SUFFIX = "uuid_suffix"  # the trailing UUID of a longer stem

#: What every hook-plugin harness's `plugin` object names.
PLUGIN_KEYS = ("root_env", "manifest_dir", "marketplace", "marketplace_source")

#: How a marketplace file names a plugin's directory: Claude Code's
#: `"source": "./plugins/x"`, or Codex's `{"source": "local", "path": "./plugins/x"}`.
MARKETPLACE_SOURCE_STRING = "string"
MARKETPLACE_SOURCE_OBJECT = "object"
MARKETPLACE_SOURCES = (MARKETPLACE_SOURCE_STRING, MARKETPLACE_SOURCE_OBJECT)

SUPPORTED_VERSION = 1


class RegistryError(ValueError):
    """The registry file is missing, malformed, or breaks one of its rules."""


@dataclass(frozen=True)
class Capture:
    token_env: str
    plugin_dir_env: str
    #: Relative to the home directory.
    plugin_dir: str
    watcher_prefix: str
    #: The sanitizer module's name inside tap_core / the tap package.
    sanitizer: str
    #: Whether the uploader may fall back to the probe CLI config's
    #: `ingest_token` (only the harness that token was minted for).
    cli_token_fallback: bool
    #: A plugin dir an older install used, still read when it exists and the
    #: current one does not (relative to the home directory).
    legacy_plugin_dir: str | None = None

    def state_dir(self, env: Mapping[str, str] | None = None) -> Path:
        """The capture plugin's state folder: the override variable, else the
        current folder, else a legacy folder an older install left behind."""
        values = os.environ if env is None else env
        override = (values.get(self.plugin_dir_env) or "").strip()
        if override:
            return Path(override)
        current = Path.home() / self.plugin_dir
        if self.legacy_plugin_dir:
            legacy = Path.home() / self.legacy_plugin_dir
            if legacy.exists() and not current.exists():
                return legacy
        return current


@dataclass(frozen=True)
class Harness:
    id: str
    label: str
    display: str
    aliases: tuple[str, ...]
    binary: str | None
    cli_flag: str | None
    family: str
    captured: bool
    installable: bool
    #: The knowledge engine knows this source: search may name it. A harness
    #: is added with False and flipped once the engine is deployed.
    searchable: bool
    #: The ingest route suffix: `/ingest/v1/sessions/<route>`.
    route: str | None
    icon: str | None
    detect_env: tuple[str, ...]
    session_env: str | None
    version_env: str | None
    min_version: tuple[int, int, int] | None
    #: {"path": relative to HOME, "env": the override variable}.
    home: Mapping[str, str] | None
    #: {"root": relative to HOME, "session_id": SESSION_ID_*}.
    transcripts: Mapping[str, str] | None
    capture: Capture | None
    #: {"root_env", "manifest_dir", "marketplace", "marketplace_source"} for
    #: hook-plugin harnesses: the plugin-root variable its hooks see, the
    #: manifest dir inside every released plugin, its marketplace file (relative
    #: to agent/) and how that file names a plugin (MARKETPLACE_SOURCE_*).
    plugin: Mapping[str, str] | None
    #: {"global": filename in the harness home, "project": [filenames]}.
    instructions: Mapping[str, Any] | None
    team_note: str | None
    mcp: str | None
    question_tool: str | None
    lean_profile: str | None
    #: Attribute a shell's session to this harness only when its capture is
    #: paired (its session variable is set in every shell, Probe or not).
    attribution_requires_pairing: bool = False
    capabilities: Mapping[str, bool] | None = None
    #: Whether Probe installs its status-line segment into this harness.
    statusline: bool = False
    #: Where the harness's "write reasoning summaries" setting lives, if it has
    #: one ("settings-json" | "config-toml"); None means nothing to switch.
    reasoning_setting: str | None = None
    #: The package an extension-family harness installs (a dir under agent/,
    #: rendered into the public mirror); None for the other families.
    package_dir: str | None = None

    def can(self, capability: str) -> bool:
        """Whether this harness has a CAPABILITY_* (unknown means no)."""
        return bool((self.capabilities or {}).get(capability))

    @property
    def names(self) -> tuple[str, ...]:
        """Every spelling that means this harness: its id, then its aliases."""
        return (self.id, *self.aliases)

    def home_dir(self, env: Mapping[str, str] | None = None) -> Path | None:
        """The harness's home directory, honouring its override variable."""
        if not self.home:
            return None
        values = os.environ if env is None else env
        override = (values.get(self.home["env"]) or "").strip()
        return Path(override).expanduser() if override else Path.home() / self.home["path"]


@dataclass(frozen=True)
class Registry:
    default: str
    harnesses: tuple[Harness, ...]

    def all(self) -> tuple[Harness, ...]:
        return self.harnesses

    def ids(self) -> tuple[str, ...]:
        return tuple(h.id for h in self.harnesses)

    def get(self, harness_id: str) -> Harness:
        """The row for an exact id. KeyError for anything else; never a default."""
        for harness in self.harnesses:
            if harness.id == harness_id:
                return harness
        raise KeyError(harness_id)

    def find(self, name: str | None) -> Harness | None:
        """The row an id or alias names (case and surrounding space ignored), or None."""
        wanted = (name or "").strip().lower()
        if not wanted:
            return None
        for harness in self.harnesses:
            if wanted in harness.names:
                return harness
        return None

    def captured(self) -> tuple[Harness, ...]:
        return tuple(h for h in self.harnesses if h.captured)

    def installable(self) -> tuple[Harness, ...]:
        return tuple(h for h in self.harnesses if h.installable)

    def detect(self, env: Mapping[str, str] | None = None) -> Harness | None:
        """The harness whose environment marker is set, first row wins."""
        values = os.environ if env is None else env
        for harness in self.harnesses:
            if any(values.get(name) for name in harness.detect_env):
                return harness
        return None


def _tuple(value: Any, where: str) -> tuple:
    if not isinstance(value, list):
        raise RegistryError(f"{where} must be a list")
    return tuple(value)


def _bool(value: Any, where: str) -> bool:
    """A real JSON boolean, absent meaning false. A string "false" is an
    error, never true: some of these flags decide which credential a harness
    may use."""
    if value is None:
        return False
    if not isinstance(value, bool):
        raise RegistryError(f"{where} must be true or false")
    return value


def _object_or_none(value: Any, where: str, required: tuple[str, ...]) -> Mapping[str, Any] | None:
    """An object naming every `required` key with a non-empty string, or null."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RegistryError(f"{where} must be an object or null")
    for key in required:
        if not isinstance(value.get(key), str) or not value[key]:
            raise RegistryError(f"{where}.{key} must be a non-empty string")
    return value


def _capture(value: Any, where: str) -> Capture | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise RegistryError(f"{where} must be an object or null")
    try:
        capture = Capture(**value)
    except TypeError as exc:
        raise RegistryError(f"{where}: {exc}") from exc
    for name in ("token_env", "plugin_dir_env", "plugin_dir", "watcher_prefix", "sanitizer"):
        if not isinstance(getattr(capture, name), str) or not getattr(capture, name):
            raise RegistryError(f"{where}.{name} must be a non-empty string")
    _bool(capture.cli_token_fallback, f"{where}.cli_token_fallback")
    return capture


def _str_or_none(value: Any, where: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise RegistryError(f"{where} must be a string or null")
    return value


def _required_str(row: Mapping[str, Any], key: str, where: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise RegistryError(f"{where}.{key} must be a non-empty string")
    return value


def _harness(row: Any, index: int) -> Harness:
    where = f"harnesses[{index}]"
    if not isinstance(row, dict):
        raise RegistryError(f"{where} must be an object")
    hid = _required_str(row, "id", where)
    where = f"harness {hid!r}"
    family = _required_str(row, "family", where)
    if family not in FAMILIES:
        raise RegistryError(f"{where}.family must be one of {FAMILIES}")
    capture = _capture(row.get("capture"), f"{where}.capture")
    min_version = row.get("min_version")
    if min_version is not None:
        if not (
            isinstance(min_version, list)
            and len(min_version) == 3
            and all(isinstance(part, int) for part in min_version)
        ):
            raise RegistryError(f"{where}.min_version must be [major, minor, patch] or null")
        min_version = tuple(min_version)
    transcripts = _object_or_none(row.get("transcripts"), f"{where}.transcripts", ("root",))
    if transcripts is not None and transcripts.get("session_id") not in (
        SESSION_ID_STEM,
        SESSION_ID_UUID_SUFFIX,
    ):
        raise RegistryError(
            f"{where}.transcripts.session_id must be {SESSION_ID_STEM!r} or {SESSION_ID_UUID_SUFFIX!r}"
        )
    harness = Harness(
        id=hid,
        label=_required_str(row, "label", where),
        display=_required_str(row, "display", where),
        aliases=tuple(
            alias.lower() for alias in _tuple(row.get("aliases", []), f"{where}.aliases")
        ),
        binary=_str_or_none(row.get("binary"), f"{where}.binary"),
        cli_flag=_str_or_none(row.get("cli_flag"), f"{where}.cli_flag"),
        family=family,
        captured=_bool(row.get("captured"), f"{where}.captured"),
        installable=_bool(row.get("installable"), f"{where}.installable"),
        searchable=_bool(row.get("searchable"), f"{where}.searchable"),
        route=_str_or_none(row.get("route"), f"{where}.route"),
        icon=_str_or_none(row.get("icon"), f"{where}.icon"),
        detect_env=_tuple(row.get("detect_env", []), f"{where}.detect_env"),
        session_env=_str_or_none(row.get("session_env"), f"{where}.session_env"),
        version_env=_str_or_none(row.get("version_env"), f"{where}.version_env"),
        min_version=min_version,
        home=_object_or_none(row.get("home"), f"{where}.home", ("path", "env")),
        transcripts=transcripts,
        capture=capture,
        plugin=row.get("plugin"),
        instructions=row.get("instructions"),
        team_note=_str_or_none(row.get("team_note"), f"{where}.team_note"),
        mcp=_str_or_none(row.get("mcp"), f"{where}.mcp"),
        question_tool=_str_or_none(row.get("question_tool"), f"{where}.question_tool"),
        lean_profile=_str_or_none(row.get("lean_profile"), f"{where}.lean_profile"),
        attribution_requires_pairing=_bool(
            row.get("attribution_requires_pairing"), f"{where}.attribution_requires_pairing"
        ),
        capabilities=row.get("capabilities") or {},
        statusline=_bool(row.get("statusline"), f"{where}.statusline"),
        reasoning_setting=_str_or_none(row.get("reasoning_setting"), f"{where}.reasoning_setting"),
        package_dir=_str_or_none(row.get("package_dir"), f"{where}.package_dir"),
    )
    if harness.captured and (
        harness.route is None or harness.capture is None or harness.transcripts is None
    ):
        raise RegistryError(f"{where} is captured, so it needs route, capture and transcripts")
    if harness.family == FAMILY_HOOK_PLUGIN:
        if not harness.plugin:
            raise RegistryError(f"{where} is a hook-plugin harness, so it needs plugin")
        for key in PLUGIN_KEYS:
            if not isinstance(harness.plugin.get(key), str) or not harness.plugin[key]:
                raise RegistryError(f"{where}.plugin.{key} must be a non-empty string")
        if harness.plugin["marketplace_source"] not in MARKETPLACE_SOURCES:
            raise RegistryError(f"{where}.plugin.marketplace_source must be one of {MARKETPLACE_SOURCES}")
    if harness.family == FAMILY_EXTENSION and harness.installable and not harness.package_dir:
        raise RegistryError(f"{where} is an installable extension harness, so it needs package_dir")
    if not harness.detect_env:
        raise RegistryError(f"{where}.detect_env must name at least one variable")
    return harness


def parse(data: Any) -> Registry:
    """Validate a decoded registry document and build the Registry."""
    if not isinstance(data, dict):
        raise RegistryError("the registry must be a JSON object")
    if data.get("version") != SUPPORTED_VERSION:
        raise RegistryError(f"unsupported registry version {data.get('version')!r}")
    rows = data.get("harnesses")
    if not isinstance(rows, list) or not rows:
        raise RegistryError("harnesses must be a non-empty list")
    harnesses = tuple(_harness(row, index) for index, row in enumerate(rows))
    seen: dict[str, str] = {}
    for harness in harnesses:
        for name in harness.names:
            if name in seen:
                raise RegistryError(f"{name!r} names both {seen[name]!r} and {harness.id!r}")
            seen[name] = harness.id
    routes = [h.route for h in harnesses if h.route]
    if len(routes) != len(set(routes)):
        raise RegistryError("two harnesses share an ingest route")
    default = data.get("default")
    if default not in {h.id for h in harnesses}:
        raise RegistryError(f"default {default!r} is not a harness id")
    return Registry(default=default, harnesses=harnesses)


def load(path: Path | str | None = None, *, extra: Iterable[Mapping[str, Any]] = ()) -> Registry:
    """Read and validate the registry (the copy beside this module by default).

    `extra` appends rows, for tests that prove a new harness needs only a row
    and its adapters.
    """
    source = Path(path) if path is not None else REGISTRY_FILE
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RegistryError(f"cannot read {source}: {exc}") from exc
    if extra:
        data = {**data, "harnesses": [*data.get("harnesses", []), *extra]}
    return parse(data)


_CACHED: Registry | None = None


def get_registry() -> Registry:
    """The registry beside this module, read once per process."""
    global _CACHED
    if _CACHED is None:
        _CACHED = load()
    return _CACHED
