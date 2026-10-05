"""One row per capture source, built from the harness registry.

The rows are the captured harnesses of `harnesses.json` (the tap's copy of
agent/src/probe/harness/harnesses.json, read by `harness_registry.py` beside
it). Every value that used to be a `"codex" if capture_source() == "codex"
else ...` ternary comes from there. Adding a harness is adding a registry row
and its sanitizer; a value that is NOT in a row does not vary by source.

`webhook_path` does not derive from `source_id`: Claude Code's route has
always been `/sessions/claude-code` (hyphen) while its source id is
`claude_code` (underscore). Deriving one from the other would silently
re-point the oldest and busiest route.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from dataclasses import dataclass
from pathlib import Path

#: session_id_strategy values — how a transcript's filename stem yields its
#: session id. Kept as a closed set of named constants (not a bare bool)
#: because a future harness's filename shape is not guaranteed to be one of
#: only two forms forever; reconcile.session_id_for() dispatches on these.
SESSION_ID_STEM = "stem"
SESSION_ID_UUID_SUFFIX = "uuid_suffix"
#: The id is the trailing UUID of the nearest folder above the transcript
#: whose name ends in one (Kimi Code: <wd>/session_<uuid>/agents/main/wire.jsonl).
SESSION_ID_PARENT_UUID = "parent_uuid"


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
    #:   SESSION_ID_PARENT_UUID — every transcript has the same name, and the
    #:     id is the folder above it (Kimi Code: session_<uuid>/agents/main/).
    session_id_strategy: str


def _registry():
    """The registry copy beside this file, loaded by path so this module stays
    loadable on its own (tests exec it without the tap package on sys.path)."""
    key = "_probe_tap.harness_registry"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, Path(__file__).with_name("harness_registry.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key].get_registry()


def _rows() -> dict[str, Source]:
    return {
        h.id: Source(
            source_id=h.id,
            display_name=h.label,
            webhook_path=f"/ingest/v1/sessions/{h.route}",
            sanitizer_module=f"tap.{h.capture.sanitizer}",
            token_env=h.capture.token_env,
            plugin_dir_env=h.capture.plugin_dir_env,
            default_session_root=h.transcripts["root"],
            session_id_strategy=h.transcripts["session_id"],
        )
        for h in _registry().captured()
    }


_SOURCES: dict[str, Source] = _rows()

#: Read-only view of _SOURCES. The "ONLY source-dependent table" claim above
#: is an invariant, not just prose — MappingProxyType makes a stray
#: `sources.SOURCES["x"] = ...` a TypeError instead of a silent second way
#: for a source's shape to drift.
SOURCES: types.MappingProxyType[str, Source] = types.MappingProxyType(_SOURCES)

DEFAULT_SOURCE_ID = _registry().default


def get(source_id: str) -> Source:
    """The row for `source_id`, or KeyError. Never defaults."""
    return SOURCES[source_id]


def plugin_state_dir(source: Source, plugin_name: str) -> Path:
    """Per-source durable state root, before env overrides: the folder the
    registry names (`capture.plugin_dir`), with `plugin_name` as its last part."""
    capture = _registry().get(source.source_id).capture
    return (Path.home() / capture.plugin_dir).parent / plugin_name


def harness(source: Source):
    """The full registry row behind a Source (transcript watch mode, root
    override, discovery strategy, capture settings)."""
    return _registry().get(source.source_id)
