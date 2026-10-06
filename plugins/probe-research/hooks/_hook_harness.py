"""Which coding agent is running this hook, from the harness registry beside it.

The plugin hooks run under the system python3 with no `probe` package, so they
read `harnesses.json` (+ `_harness_registry.py`), the copies `make
sync-harnesses` puts in this folder. Every hook that used to answer
`"codex" if ... else "claude_code"` asks here instead.

How the harness is told apart, in order:
  1. `PROBE_AGENT`, which the hook wrappers in routes.json export, when it names
     a hook-plugin harness;
  2. the harness's own plugin-root variable (`CLAUDE_PLUGIN_ROOT` is Claude
     Code's, `PLUGIN_ROOT` is Codex's; the more specific row comes first). A
     plugin root means a hook-plugin harness is running this hook, so a
     `PROBE_AGENT=pi` left in the shell is a leftover, not evidence, and must
     not cost Claude Code its question tool;
  3. `PROBE_AGENT` naming any other harness (no plugin root: a direct call);
  4. the harness's environment marker (`CLAUDECODE`, `CODEX_THREAD_ID`, ...);
  5. the registry's default (Claude Code): an unset environment is the Claude
     Code install and always has been.

`current()` and `session_id()` read the JSON with `json` alone. The status
line renders and the approvals hook runs on every prompt, and the full loader
(dataclasses, pathlib) cost them ~16ms of startup for a few plain fields. A
row's other facts (`capture`, `home_dir()`, ...) load the full registry on
first use.
"""

from __future__ import annotations

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_DOCUMENT = None


def load_sibling(name: str):
    """A module beside this file, by EXPLICIT path only (never a bare import:
    sys.path can carry the user's project, and a same-named stranger would run
    inside the hook). Registered in sys.modules under a private name, which
    dataclasses need to resolve their own annotations."""
    import importlib.util  # noqa: PLC0415

    key = f"_probe_hooks.{name}"
    if key in sys.modules:
        return sys.modules[key]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def registry():
    return load_sibling("_harness_registry").get_registry()


def _document() -> dict:
    global _DOCUMENT
    if _DOCUMENT is None:
        with open(os.path.join(_HERE, "harnesses.json"), encoding="utf-8") as fh:
            _DOCUMENT = json.load(fh)
    return _DOCUMENT


class Row:
    """One registry row: its plain fields from the JSON, everything else from
    the full loader's row, loaded on first use."""

    def __init__(self, raw: dict):
        self._raw = raw
        self.id = raw["id"]
        self.family = raw.get("family")
        self.question_tool = raw.get("question_tool")
        self.session_env = raw.get("session_env")
        self.plugin = raw.get("plugin")

    def can(self, capability: str) -> bool:
        return (self._raw.get("capabilities") or {}).get(capability) is True

    def __getattr__(self, name):
        return getattr(registry().get(self.id), name)


def _names(raw: dict) -> list:
    return [raw["id"], *(alias.lower() for alias in raw.get("aliases") or [])]


def current(env=None) -> Row:
    """The harness row for this hook's process."""
    values = os.environ if env is None else env
    document = _document()
    rows = document["harnesses"]
    wanted = (values.get("PROBE_AGENT") or "").strip().lower()
    named = next((raw for raw in rows if wanted and wanted in _names(raw)), None)
    if named is not None and named.get("family") == "hook-plugin":
        return Row(named)
    for raw in rows:
        root_env = (raw.get("plugin") or {}).get("root_env")
        if root_env and values.get(root_env):
            return Row(raw)
    if named is not None:
        return Row(named)
    for raw in rows:
        if any(values.get(name) for name in raw.get("detect_env") or []):
            return Row(raw)
    return Row(next(raw for raw in rows if raw["id"] == document["default"]))


def session_id(env=None) -> str:
    """This session's id from the environment ("" when none): the current
    harness's session variable first, then a captured harness's. Only captured
    rows: an id nobody captures names no transcript."""
    values = os.environ if env is None else env
    own = current(values).session_env
    if own and values.get(own):
        return values[own]
    for raw in _document()["harnesses"]:
        name = raw.get("session_env")
        if raw.get("captured") is True and name and values.get(name):
            return values[name]
    return ""


def manifest_dir(env=None) -> str:
    """The plugin manifest folder of the harness running this hook
    (`.claude-plugin`, `.codex-plugin`, `.kimi-plugin`)."""
    return (current(env).plugin or {}).get("manifest_dir") or ".claude-plugin"


if __name__ == "__main__":
    # `python3 _hook_harness.py manifest-dir`: for the shell hooks.
    if sys.argv[1:] == ["manifest-dir"]:
        print(manifest_dir())
