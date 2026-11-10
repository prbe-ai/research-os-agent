"""Which coding agent is running this hook, from the harness registry beside it.

The plugin hooks run under the system python3 with no `probe` package, so they
read `harnesses.json` (+ `_harness_registry.py`), the copies `make
sync-harnesses` puts in this folder. Every hook that used to answer
`"codex" if ... else "claude_code"` asks here instead.

How the harness is told apart, in order:
  0. `PROBE_HOOK_HARNESS`, which only the Probe service sets (it drops a caller's): the
     harness it verified for an event it runs from an extension (pi), under Claude
     Code's plugin root;
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
#: `harness_registry.WAKE_BY_STOP_HOOK` (a test holds them equal): the harness's Stop hook wakes it.
WAKE_BY_STOP_HOOK = "stop-hook"


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


#: The hook outputs (a row's `hook_output`) with a line shown to the PERSON and never the model:
#: Claude Code's `systemMessage`, which Codex shows too and pi's extension says. Kimi Code's
#: `message-json` has no such field (`harness_io.translate` drops it), and Cursor's `cursor-json`
#: shows a hook's `user_message` only for a refused prompt or tool call (CLI 2026.11.05), so the
#: Probe service drops it too (`cursor_agent/replies.get_context`).
PERSON_LINE_OUTPUTS = ("claude-json",)


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
        #: How it starts a turn for a late answer (`harness_registry.WAKE_BY_*`), None: it cannot.
        self.wake_by = raw.get("wake_by")

    def can(self, capability: str) -> bool:
        return (self._raw.get("capabilities") or {}).get(capability) is True

    def shows_person_line(self) -> bool:
        """Does this harness show a hook's person-only line (`systemMessage`) to anyone? Where it
        does not, a notice meant for the person once is never spent here (`server_notices`)."""
        return (self._raw.get("hook_output") or "claude-json") in PERSON_LINE_OUTPUTS

    def __getattr__(self, name):
        return getattr(registry().get(self.id), name)


def _names(raw: dict) -> list:
    return [raw["id"], *(alias.lower() for alias in raw.get("aliases") or [])]


#: The harness the Probe service verified for an event it runs (`hooks.run` v2).
VERIFIED_ENV = "PROBE_HOOK_HARNESS"


def current(env=None) -> Row:
    """The harness row for this hook's process."""
    values = os.environ if env is None else env
    document = _document()
    rows = document["harnesses"]
    # Set by the Probe service alone (a caller's own is dropped): the harness it
    # verified for a proxied event, whose routes run under Claude Code's root.
    verified = (values.get(VERIFIED_ENV) or "").strip().lower()
    for raw in rows:
        if verified and raw["id"] == verified:
            return Row(raw)
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


def instruction_file(env=None) -> "str | None":
    """This harness's global rules file (`agent_rules.memory_path`), or None when it has none here."""
    try:
        row = current(env)
        home = row.home_dir(env)
        name = (row.instructions or {}).get("global")
    except Exception:  # noqa: BLE001 -- no registry row to read: no file
        return None
    return os.path.join(str(home), name) if home is not None and name else None


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


#: A transcript file named for the thread that writes it ends in that thread's id:
#: `rollout-<time>-<uuid>.jsonl` (the registry's `uuid_suffix` naming). Matched and
#: compared with no regard to case: a UUID's hex digits name the same thread either way.
_THREAD_FILE = r"-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"


def from_helper_agent(payload: dict) -> bool:
    """Did a helper agent (subagent) fire this hook, not the agent the researcher
    talks to? What lands in a helper's context never reaches the researcher, and
    its prompt is the main agent's words. Any one mark is enough:

      * `agent_id`: on every hook fired inside a helper and never on the main
        thread (Claude Code 2.1.283's hook-input schema; Codex 0.166 sets it on
        a helper's prompt and tool calls too, live run 2026-11-06);
      * a transcript inside a `subagents/` folder (Claude Code's helper
        transcripts; its `transcript_path` stays the main one, so a second net);
      * a transcript named for a thread other than `session_id`: a helper hook
        that carries the root's `session_id` beside its own rollout and names no
        `agent_id` (the service's `get_thread_id` reads the same mark). A main
        thread's transcript is named for its own session."""
    if payload.get("agent_id"):
        return True
    path = payload.get("transcript_path")
    if not isinstance(path, str):
        return False
    path = path.replace("\\", "/")
    if "/subagents/" in path:
        return True
    root = payload.get("session_id")
    if not isinstance(root, str) or not root:
        return False
    import re  # noqa: PLC0415 -- json has loaded it already

    found = re.search(_THREAD_FILE, path, re.IGNORECASE)
    return found is not None and found.group(1).lower() != root.lower()


def manifest_dir(env=None) -> str:
    """The plugin manifest folder of the harness running this hook
    (`.claude-plugin`, `.codex-plugin`, `.kimi-plugin`)."""
    return (current(env).plugin or {}).get("manifest_dir") or ".claude-plugin"


if __name__ == "__main__":
    # `python3 _hook_harness.py manifest-dir`: for the shell hooks.
    if sys.argv[1:] == ["manifest-dir"]:
        print(manifest_dir())


#: Probe's production MCP host; this device's own MCP is `PROBE_MCP_URL`.
PRODUCTION_MCP_HOST = "mcp.research.prbe.ai"


def is_mcp_key_ours(identity, env=None) -> bool:
    """Is this hook's agent's MCP under `identity`'s key (Codex's config.toml `[mcp_servers.<key>]`) Probe's? A
    bare `mcp__<key>__` tool is Probe's only then. With a table: its url on Probe's production MCP host or on
    `PROBE_MCP_URL`'s. With none (an OAuth install declares its MCP in the plugin alone): Probe's tracking or
    daemon plugin is added from `identity`'s marketplace and that marketplace names `identity.repo`. Missing or
    unreadable: no. A line scan, not a TOML parser: the hooks run under a system python3 that may predate tomllib."""
    import re
    from urllib.parse import urlparse

    values = os.environ if env is None else env
    try:
        home = current(values).home_dir(values)
        text = (home / "config.toml").read_text(encoding="utf-8") if home else ""
    except Exception:  # noqa: BLE001 - no record to read: not ours
        return False

    def table(*keys: str):
        header = r"\.".join(r'"?' + re.escape(key) + r'"?' for key in keys)
        found = re.search(r"(?m)^\[" + header + r"\][ \t]*$(.*?)(?=^\[|\Z)", text, re.S)
        return found.group(1) if found else None

    server = table("mcp_servers", identity.mcp_key)
    if server is not None:
        url = re.search(r"""(?m)^[ \t]*url[ \t]*=[ \t]*["']([^"']+)["']""", server)
        host = urlparse(url.group(1)).netloc if url else ""
        ours = {PRODUCTION_MCP_HOST, urlparse((values.get("PROBE_MCP_URL") or "").strip()).netloc} - {""}
        return bool(host) and host in ours
    added = any(table("plugins", identity.make_plugin_id(name)) is not None
                for name in (identity.tracking_plugin, identity.daemon_plugin))
    marketplace = table("marketplaces", identity.marketplace)
    repo = r"(?i)(?<![\w-])" + re.escape(identity.repo) + r"(?![\w-])"
    return added and marketplace is not None and re.search(repo, marketplace) is not None
