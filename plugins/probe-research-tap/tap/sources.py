"""One row per capture source. The tap's ONLY source-dependent table.

Every value that used to be a `"codex" if capture_source() == "codex" else ...`
ternary lives here instead. Adding a harness is adding a row; a value that is
NOT in this table is a value that does not vary by source, which is most of
them.

`webhook_path` does not derive from `source_id`: Claude Code's route has
always been `/sessions/claude-code` (hyphen) while its source id is
`claude_code` (underscore). Deriving one from the other would silently
re-point the oldest and busiest route.
"""

from __future__ import annotations

import types
from dataclasses import dataclass
from pathlib import Path

#: session_id_strategy values — how a transcript's filename stem yields its
#: session id. Kept as a closed set of named constants (not a bare bool)
#: because a future harness's filename shape is not guaranteed to be one of
#: only two forms forever; reconcile.session_id_for() dispatches on these.
SESSION_ID_STEM = "stem"
SESSION_ID_UUID_SUFFIX = "uuid_suffix"


@dataclass(frozen=True)
class Source:
    source_id: str
    display_name: str
    webhook_path: str
    sanitizer_module: str
    token_env: str
    plugin_dir_env: str
    #: DEFAULT/primary root this harness writes sessions under, relative to
    #: $HOME — not necessarily the only one. A later phase's pi_discovery.py
    #: may scan additional configurable roots on top of this one for a
    #: source whose sessions aren't confined to a single fixed path.
    default_session_root: str
    #: How reconcile.session_id_for() recovers a session id from a
    #: transcript's filename stem:
    #:   SESSION_ID_STEM — the whole stem IS the id
    #:     (Claude Code: <session_id>.jsonl).
    #:   SESSION_ID_UUID_SUFFIX — the id is the trailing UUID of a longer
    #:     stem (Codex: rollout-<ts>-<uuid>.jsonl; pi: <ts>_<uuid>.jsonl —
    #:     both prefix the uuid with other content, so only the tail is
    #:     trustworthy).
    session_id_strategy: str


_SOURCES: dict[str, Source] = {
    "claude_code": Source(
        source_id="claude_code",
        display_name="Claude Code",
        webhook_path="/ingest/v1/sessions/claude-code",
        sanitizer_module="tap.sanitize",
        token_env="PROBE_INGEST_TOKEN",
        plugin_dir_env="PROBE_RESEARCH_TAP_PLUGIN_DIR",
        default_session_root=".claude/projects",
        session_id_strategy=SESSION_ID_STEM,
    ),
    "codex": Source(
        source_id="codex",
        display_name="Codex",
        webhook_path="/ingest/v1/sessions/codex",
        sanitizer_module="tap.codex_sanitize",
        token_env="PRBE_CODEX_TAP_TOKEN",
        plugin_dir_env="PRBE_CODEX_TAP_PLUGIN_DIR",
        default_session_root=".codex/sessions",
        session_id_strategy=SESSION_ID_UUID_SUFFIX,
    ),
    "pi": Source(
        source_id="pi",
        display_name="pi",
        webhook_path="/ingest/v1/sessions/pi",
        sanitizer_module="tap.pi_sanitize",
        token_env="PROBE_PI_TAP_TOKEN",
        plugin_dir_env="PROBE_PI_TAP_PLUGIN_DIR",
        default_session_root=".pi/agent/sessions",
        session_id_strategy=SESSION_ID_UUID_SUFFIX,
    ),
}

#: Read-only view of _SOURCES. The "ONLY source-dependent table" claim above
#: is an invariant, not just prose — MappingProxyType makes a stray
#: `sources.SOURCES["x"] = ...` a TypeError instead of a silent second way
#: for a source's shape to drift.
SOURCES: types.MappingProxyType[str, Source] = types.MappingProxyType(_SOURCES)

DEFAULT_SOURCE_ID = "claude_code"


def get(source_id: str) -> Source:
    """The row for `source_id`, or KeyError. Never defaults."""
    return SOURCES[source_id]


def plugin_state_dir(source: Source, plugin_name: str) -> Path:
    """Per-source durable state root, before env overrides."""
    if source.source_id == "codex":
        return Path.home() / ".codex" / "state" / plugin_name
    if source.source_id == "pi":
        return Path.home() / ".pi" / "agent" / "state" / plugin_name
    return Path.home() / ".claude" / "plugins" / plugin_name
