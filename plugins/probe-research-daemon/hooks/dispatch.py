"""Run the hooks one event's fixed hooks.json entry stands for.

hooks.json carries ONE entry per event, with no tool matcher, and those entries
never change: Codex trusts each entry by a hash of the whole entry and silently
skips a changed one until the researcher re-approves it in `/hooks`. 21 of the 23
edits to the old per-script hooks.json (2026-08-07 to 2026-10-05) did that to
every Codex user. Each entry runs `dispatch.sh <lane>`, which runs this; WHAT
runs, and on which tools, is `routes.json` beside it, free to change in any
release.

`routes.json` is the old per-script wiring, byte for byte (same events,
matchers, commands, timeouts, profile exports, shell fast paths), with a `lane`
on each group. For the event's lane this runs every route whose matcher fits the
payload's `tool_name` (as the harness matches: empty or `*` is every tool, a
plain name or `A|B` list is exact, anything else a regex search), EXACTLY as the
harness ran it before:

  * one plain python route (the shape every `python3 hooks/<x>.py` entry had,
    rebuilt byte for byte from its parts by `plain_route` and compared whole):
    run in-process with runpy, its exports, argv and stdin as bash would have
    set them, bounded by its own timeout (an alarm), and no second interpreter
    start, so a lone guard costs what it did before. Output and exit code are
    the route's own.
  * anything else, one route or several (a shell fast path, a .sh target):
    each as its own child process in its own process group, in parallel (both
    Claude Code and Codex ran an event's hooks in parallel), bounded by its own
    timeout and this lane's budget, and killed with everything it started when
    it overruns or when this process is told to stop; their outputs merged into
    one (MERGE), a lone output byte for byte.

Kimi Code runs this too, through its own manifest (tools/kimi_hooks.py renders
one entry per routes.json event): `harness_io.py --event <event>` puts Kimi's
payload in Claude's shape, calls `serve` here with the event's lane (warm host,
else here, exactly as `dispatch.sh` does), and translates the merged output
back. Only it uses ADAPTER_LANES and the UNSHOWN option.

The warm host (THE HOST, plan A0): an event goes to this folder's host, which
the Probe service runs, when it answers within HOST_WAIT_S; else it runs here.
Such a miss (A MISS, plan D35/D36) runs here only what the agent waits on (a
decision, context, a message) and hands the record-keeping routes (routes.json
`"record_keeping": true`) to the service, which runs them once, by the event's
id (`hooks.queue`; with no service answering, straight into its queue, which
the next service drains). Then it asks for the host, or starts the service
(the CLI's own `client.ensure`, never waited on), for the next event. Whatever
the service cannot take runs here, as before.

Contract, as for every hook here: STDLIB ONLY, PYTHON 3.9, FAIL-OPEN. Started as
`python3 -S` (no site-packages: a dispatcher needs none, and it saves ~10ms on
every event). Never exits non-zero unless a route did (an exit of 2 is a veto on
PreToolUse/UserPromptSubmit, and only a route may cast one).

Hand-over: Claude Code fixes a session's plugin folder at its start
(`CLAUDE_PLUGIN_ROOT`), and Codex does too (`PLUGIN_ROOT`), so a session left
open across releases keeps running the hooks it started with while what it reads
(the managed CLAUDE.md block, the CLI) moves on: a 0.129.0 guard refused `probe
paper find`, the command the block named (2026-10-13). Before anything else, this
hands the event to the installed copy, when that is another version folder of
this same plugin (`installed_root`, `hand_over`): under Claude Code the one its
install ledger names, under Codex the newest complete version in Codex's plugin
cache (`codex_installed_root`). So every event hooks.json sends here runs the
installed hooks. The Stop wake (claude-reads.json, outside this dispatcher) does
the same in reads_hook.py. Skills follow too: Claude Code reads Probe's from its
own skills folder (copies of the plugin's, which it re-reads when they change),
and the pre-tool route on the Skill tool (`root_skills_sync.py skill-call`) brings
those copies to the installed copy's before the call (`is_installed_copy`); Codex
lists a plugin's skills again on every turn from its cache, so it needs nothing.

Heartbeat: under Codex (`PLUGIN_ROOT` set), the session-start and prompt lanes
touch `$XDG_STATE_HOME/probe/hooks/alive/<session>`. The capture plugin's prompt
hook (probe-research-tap, hooks/probe-hooks-trust.sh) tells the researcher to
approve Probe's hooks in `/hooks` while a session that expects this file has
none. Keep the path and the session-id rule in step with that file.
"""

from __future__ import annotations

import os
import sys
import time

# Everything else (json, re, subprocess, threading, signal, runpy) loads only on
# the paths that need it: this starts on EVERY tool call, and most of them
# (Read, Grep, Glob, ...) leave at the byte filter below with os and sys alone.

HERE = os.path.dirname(os.path.abspath(__file__))
#: When this interpreter reached the dispatcher (monotonic): as near the hook's
#: own start as this process can see (bash and the interpreter's start come
#: first, which STARTUP_ALLOWANCE_S covers). `hand_over` passes it to the copy
#: it runs, so a hosted event's client counts from here (`host_end`).
PROCESS_STARTED = time.monotonic()

#: Lane -> the hook event it serves; the argument hooks.json passes.
LANE_EVENT = {
    "session-start": "SessionStart",
    "prompt": "UserPromptSubmit",
    "pre-compact": "PreCompact",
    "pre-tool": "PreToolUse",
    "post-tool": "PostToolUse",
    "session-end": "SessionEnd",
    "stop": "Stop",
    "turn-started": "TurnStarted",
}

#: Lanes no hooks.json entry names: only `harness_io.py --event` runs them (the
#: Kimi Code manifest). Kimi fires no UserPromptSubmit for a skill the person
#: types (`/probe on`), only TurnStarted; harness_io hands that turn to its
#: routes as the typed line's UserPromptSubmit.
ADAPTER_LANES = frozenset({"turn-started"})

#: Each lane's hooks.json entry `timeout`, in seconds (None: the harness's
#: default). Frozen with the entries: a lane's routes must each fit in
#: ENTRY_TIMEOUT[lane] - MARGIN_S (tests/test_hook_dispatch.py holds both).
#: `prompt` keeps 4s of headroom on purpose, since it can never grow. Kimi
#: Code's entries take theirs from here too (tools/kimi_hooks.py), ADAPTER_LANES
#: included.
ENTRY_TIMEOUT = {
    "session-start": None,
    "prompt": 10,
    "pre-compact": 11,
    "pre-tool": 6,
    "post-tool": 6,
    "session-end": 3,
    "stop": 16,
    "turn-started": 6,
}

#: An option after the lane: the harness shows no one this event's output (Kimi
#: Code fires PostToolUse and forgets it). Then nothing waits on post-tool, so
#: the catch-all reads route never runs there: it would claim a daemon message
#: nobody sees. The next prompt delivers it instead.
UNSHOWN = "--unshown"

#: With no entry timeout the harness's own default applies: Claude Code's is
#: 60s (Codex's 600s), so a lane with none is budgeted as 60s.
HARNESS_DEFAULT_TIMEOUT_S = 60

#: Stop this long before the entry's timeout, so the harness never kills the
#: dispatcher (and with it every route's output) first.
MARGIN_S = 1.0
#: After killing an overrun route, how long to wait for it to go.
KILL_WAIT_S = 0.2
#: How often a waiting dispatcher checks whether a route has exited while
#: something it started still holds its output pipe.
POLL_S = 0.05
#: Slack on top of the budget when joining the waiting threads.
JOIN_SLACK_S = 0.1

#: SessionEnd is bounded by the harness, not the entry: Claude Code cancels the
#: whole phase at CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS (default 1.5s, and a
#: plugin's own timeout does not count); Codex clamps each hook to 3s. The
#: harness cancelled overrunning SessionEnd hooks; so does this, a little sooner.
SESSION_END_MARGIN_S = 0.2
CLAUDE_SESSION_END_DEFAULT_MS = 1500
CODEX_SESSION_END_S = 3.0
#: Codex KILLS a SessionEnd hook CODEX_SESSION_END_S after starting it, so what
#: this process does outside its budget must fit before then too. Worst case,
#: from Codex starting the hook to this process exiting:
#:
#:     STARTUP_ALLOWANCE_S    0.2  bash, `python3 -S`, the payload and routes.json,
#:                                 all before the budget's clock starts (11ms
#:                                 median, 18ms max on the devbox; 0.2 is for a
#:                                 cold or loaded machine)
#:   + budget                 2.3  CODEX_SESSION_END_S - CODEX_SESSION_END_MARGIN_S
#:   + KILL_WAIT_S            0.2  an overrun route is killed, then waited for
#:   + JOIN_SLACK_S           0.1  the join on that wait
#:   = 2.8s: SESSION_END_MARGIN_S (0.2s) inside Codex's 3.0s.
#:
#: Through plugin 0.120.0 the budget was 3.0 - SESSION_END_MARGIN_S = 2.8s, so
#: the same worst case was 3.1s plus startup: past Codex's kill. Claude Code's
#: SessionEnd budget is unchanged. tests/test_hook_dispatch.py holds this sum.
STARTUP_ALLOWANCE_S = 0.2
CODEX_SESSION_END_MARGIN_S = STARTUP_ALLOWANCE_S + KILL_WAIT_S + JOIN_SLACK_S + SESSION_END_MARGIN_S
#: Never budget a SessionEnd below this, whatever the variable says.
MIN_SESSION_END_S = 0.1

#: The lanes every tool call reaches. Before parsing the payload, the
#: dispatcher reads every `"tool_name": "..."` in its raw bytes (JSON escaping
#: keeps file contents from faking one; a nested key only adds a name) and goes
#: on only if TOOL_FILTER takes one of them or, after a tool call, a Probe
#: daemon message waits: most tool calls cost a python start and the install
#: ledger's byte scan (`installed_root`; under Codex, one listing of its plugin
#: folder), and nothing else.
TOOL_LANES = ("pre-tool", "post-tool")

#: Per tool lane: the exact tool names, and the substrings (the Probe MCP
#: prefix `probe[-_]research`), that some route's matcher accepts.
#: tests/test_hook_dispatch.py holds this equal to what routes.json's matchers
#: accept in both plugins: a new tool route needs its name here too.
TOOL_FILTER = {
    "pre-tool": (
        frozenset(("Bash", "AskUserQuestion", "Write", "Edit", "MultiEdit", "NotebookEdit", "WebSearch", "Skill")),
        ("probe-research", "probe_research"),
    ),
    "post-tool": (
        frozenset(("Bash", "Skill", "SlashCommand", "AskUserQuestion")),
        ("probe-research", "probe_research"),
    ),
}

HEARTBEAT_LANES = ("session-start", "prompt")
#: Heartbeats older than this are removed at session start.
HEARTBEAT_KEEP_S = 14 * 24 * 3600

_PRECEDENCE = {"deny": 3, "ask": 2, "allow": 1}

#: Events whose output may carry `hookSpecificOutput` (Claude Code rejects it on
#: the rest), and the two where a bare stdout is context for the model.
_SPECIFIC_EVENTS = ("PreToolUse", "PostToolUse", "UserPromptSubmit", "SessionStart")
_PLAIN_CONTEXT_EVENTS = ("UserPromptSubmit", "SessionStart")


def _safe_id(text: str) -> str:
    """The shell's `${x//[^A-Za-z0-9_-]/}`."""
    return "".join(c for c in text if c.isascii() and (c.isalnum() or c in "_-"))


def _state_dir() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "probe", "hooks", "alive")


def heartbeat(lane: str, payload: dict) -> None:
    """Prove to the capture plugin that Probe's hooks run in this Codex session."""
    if lane not in HEARTBEAT_LANES or not os.environ.get("PLUGIN_ROOT"):
        return
    sid = _safe_id(str(payload.get("session_id") or ""))
    if not sid:
        return
    try:
        folder = _state_dir()
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sid)
        with open(path, "a", encoding="utf-8"):
            pass
        os.utime(path, None)
        if lane == "session-start":
            cutoff = time.time() - HEARTBEAT_KEEP_S
            for entry in os.scandir(folder):
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
    except OSError:
        pass


def matches(matcher, tool: str) -> bool:
    """A route's matcher against a tool name, as the harness reads it."""
    if not matcher or matcher == "*":
        return True
    names = matcher.split("|")
    if all(n and all(c.isascii() and (c.isalnum() or c == "_") for c in n) for n in names):
        return tool in names
    import re

    try:
        return re.search(matcher, tool) is not None
    except re.error:
        return False


def reads_waiting() -> bool:
    """The reads route's own shell fast path (routes.json, PostToolUse), in
    python: a Probe daemon message, a service claim its caller never received
    (`claimed/<sid>/<stem>.<token>.json`, the shell's `*.*.json`), or a newer
    status, waits for this session."""
    sid = _safe_id(os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "")  # harness-literal-ok: the reads route's own gate
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    root = os.path.join(base, "probe", "reads")
    for kind, dots in (("messages", 1), ("claimed", 2)):
        folder = os.path.join(root, kind)
        for sub in [sid] if sid else _listdir(folder):
            names = _listdir(os.path.join(folder, sub))
            if any(n.endswith(".json") and not n.startswith(".") and n.count(".") >= dots for n in names):
                return True
    if not sid:
        return False
    status = os.path.join(root, "status", sid + ".json")
    try:
        newer = os.stat(status).st_mtime
    except OSError:
        return False
    try:
        return newer > os.stat(os.path.join(root, "status", sid + ".shown")).st_mtime
    except OSError:
        return True


def _listdir(path: str) -> list:
    try:
        return os.listdir(path)
    except OSError:
        return []


_KEY = b'"tool_name"'
_SPACE = b" \t\r\n"


def string_values(data: bytes, key: bytes) -> set:
    """Every string value of `key` (quoted, e.g. b'"tool_name"') anywhere in
    raw JSON bytes (a superset of the real ones), read without parsing the JSON
    or importing anything."""
    values = set()
    at = data.find(key)
    while at >= 0:
        i = at + len(key)
        while i < len(data) and data[i] in _SPACE:
            i += 1
        if data[i : i + 1] == b":":
            i += 1
            while i < len(data) and data[i] in _SPACE:
                i += 1
            if data[i : i + 1] == b'"':
                end, escaped = i + 1, False
                while end < len(data) and data[end] != 0x22:
                    if data[end] == 0x5C:
                        escaped = True
                        end += 1
                    end += 1
                raw = data[i + 1 : end]
                if escaped:
                    import json

                    try:
                        values.add(json.loads(b'"' + raw + b'"'))
                    except ValueError:
                        values.add(raw.decode("utf-8", "replace"))
                else:
                    values.add(raw.decode("utf-8", "replace"))
        at = data.find(key, i)
    return values


def named_tools(payload: bytes) -> set:
    """Every `tool_name` string value in the raw payload (a superset of the
    real one), read without parsing the payload or importing anything."""
    return string_values(payload, _KEY)


def could_route(lane: str, payload: bytes) -> bool:
    """On a tool lane: could any route's matcher take this call? Never False
    when one would (TOOL_FILTER is held to routes.json by the tests)."""
    exact, parts = TOOL_FILTER[lane]
    names = named_tools(payload)
    if not names:
        # No literal "tool_name" key (an escaped spelling, which no harness
        # writes, or an odd payload): parse it rather than guess.
        return True
    return any(n in exact or any(p in n for p in parts) for n in names)


# --- HAND-OVER -----------------------------------------------------------------

#: Set while this dispatcher hands an event to the installed copy (to the folder
#: it came from): that copy's `hand_over` takes it out of the environment, so an
#: event moves at most once and nothing a route starts inherits it.
HANDED_OVER = "PROBE_HOOKS_HANDED_OVER"
_INSTALL_PATH = b'"installPath"'
#: A ledger bigger than this is not read (it is ~2 KB with a handful of plugins).
LEDGER_MAX_BYTES = 1 << 20


def _ledger(env) -> str:
    """Claude Code's install ledger, where Claude Code itself keeps it: in its
    plugins folder, `$CLAUDE_CODE_PLUGIN_CACHE_DIR` when that moves it, else
    `$CLAUDE_CONFIG_DIR` or `~/.claude`, then `plugins/`; the file is
    `installed_plugins.json` (as `probe.cli.root_skills` reads it)."""
    moved = (env.get("CLAUDE_CODE_PLUGIN_CACHE_DIR") or "").strip()
    if moved:
        return os.path.join(os.path.expanduser(moved), "installed_plugins.json")
    home = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    base = os.path.expanduser(home) if home else os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "plugins", "installed_plugins.json")


def _read_ledger(path: str):
    """The ledger's bytes, or None: a regular file only (never blocks on a FIFO),
    at most LEDGER_MAX_BYTES."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        import stat

        if not stat.S_ISREG(info.st_mode) or info.st_size > LEDGER_MAX_BYTES:
            return None
        chunks, size = [], 0
        while size <= LEDGER_MAX_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
            size += len(chunk)
        return None
    except OSError:
        return None
    finally:
        os.close(fd)


def _complete(real: str) -> bool:
    """Does the copy at `real` hold a whole dispatcher: dispatch.sh and
    dispatch.py, a routes.json that parses, and every hooks/ file its routes
    name? A copy still being written (or half deleted) never takes an event."""
    import json
    import re

    hooks = os.path.join(real, "hooks")
    if not os.access(os.path.join(hooks, "dispatch.sh"), os.X_OK):
        return False
    if not os.access(os.path.join(hooks, "dispatch.py"), os.R_OK):
        return False
    try:
        with open(os.path.join(hooks, "routes.json"), encoding="utf-8") as handle:
            wiring = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(wiring, dict) or not isinstance(wiring.get("hooks"), dict):
        return False
    named = set(re.findall(r"\$ROOT/hooks/([A-Za-z0-9_.-]+)", json.dumps(wiring)))
    return all(os.path.isfile(os.path.join(hooks, name)) for name in named)


def _codex_cache(env) -> str:
    """Codex's plugin cache: `$CODEX_HOME` or `~/.codex`, then `plugins/cache`
    (as `probe.cli.codex_config` and probe-research-tap's hooks read it)."""
    home = (env.get("CODEX_HOME") or "").strip()
    base = os.path.expanduser(home) if home else os.path.join(os.path.expanduser("~"), ".codex")
    return os.path.join(base, "plugins", "cache")


def _version(name: str):
    """A version folder's name as a tuple to compare (`0.161.1` -> (0, 161, 1)),
    or None for any other name: numbers, not text, so 0.10.0 is newer than 0.9.9."""
    parts = name.split(".")
    if len(parts) < 2 or not all(p and p.isascii() and p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def codex_installed_root(root: str, env) -> "str | None":
    """`installed_root` for Codex: the newest complete version of this plugin
    in Codex's cache, when that is newer than `root`; None to run the event here.

    Codex caches a plugin as `<cache>/<marketplace>/<plugin>/<version>` and sets
    `PLUGIN_ROOT` to that folder once, at session start. Installing a version
    replaces the folder, and nothing else records which version is installed
    (config.toml holds only `enabled`), so the newest version folder is the
    copy Codex runs now: by version number, never by mtime (the frozen entries'
    own fallback for a deleted start folder picks by mtime; this only ever moves
    an event forward from a folder that still exists). It must sit beside
    `root` inside Codex's own cache (a symlink out of it, a checkout or another
    plugin's folder never does), carry Codex's manifest, and be `_complete`.
    Anything else: stay.

    One listing of the plugin's folder on every event, and no `json` import
    unless a newer copy is there."""
    here = os.path.realpath(root)
    folder = os.path.dirname(here)
    if os.path.dirname(os.path.dirname(folder)) != os.path.realpath(_codex_cache(env)):
        return None
    current = _version(os.path.basename(here))
    if current is None:
        return None
    newer = sorted(
        ((version, name) for name in _listdir(folder) for version in [_version(name)]
         if version is not None and version > current),
        reverse=True,
    )
    # Newest first, and the first COMPLETE one: a newer copy still being
    # written (or half deleted) must not keep the session on an older one.
    for _version_key, name in newer:
        real = os.path.realpath(os.path.join(folder, name))
        if os.path.dirname(real) != folder or not os.path.isfile(os.path.join(real, ".codex-plugin", "plugin.json")):
            continue
        if _complete(real):
            return real
    return None


def root_variable(env) -> str:
    """Where the harness gave this plugin's folder, and so where a route reads
    it: `PLUGIN_ROOT` under Codex (a route's `$ROOT` reads it first), else
    `CLAUDE_PLUGIN_ROOT`."""
    return "PLUGIN_ROOT" if env.get("PLUGIN_ROOT") else "CLAUDE_PLUGIN_ROOT"


def installed_root(root: str, env) -> "str | None":
    """The plugin folder to hand this event to, or None to run it here.

    Only once per event. Under Codex (`PLUGIN_ROOT` marks it), the newest copy
    in its plugin cache (`codex_installed_root`). Under Claude Code, `root` sits
    in its cache as `<marketplace>/<plugin>/<version>`; the
    copy to hand to is the one folder the ledger lists under that same
    `<plugin>@<marketplace>`, when every entry there is user scope (a
    project-scoped install is per project, and this does not know the project),
    it resolves to another version folder beside `root` (a symlink out of the
    cache, a `..` or a `--plugin-dir` checkout never does), and it is
    `_complete`. Anything else: stay. It follows the ledger both ways, so a
    release rolled back stops running in open sessions too.

    The ledger is read on every event: a byte scan first, so the usual answer
    (it lists this folder) costs no `json` import (~10 ms); the ledger is
    parsed only when the scan finds another copy beside this one."""
    if env.get(HANDED_OVER):
        return None
    if env.get("PLUGIN_ROOT"):
        return codex_installed_root(root, env)
    raw = _read_ledger(_ledger(env))
    if raw is None:
        return None
    here = os.path.realpath(root)
    folder = os.path.dirname(here)

    def other(path: str) -> bool:
        real = os.path.realpath(path)
        return real != here and os.path.dirname(real) == folder

    if not any(other(p) for p in string_values(raw, _INSTALL_PATH)):
        return None
    import json

    try:
        entries = json.loads(raw.decode("utf-8"))["plugins"][ledger_key(here)]
        if isinstance(entries, list) and any(not isinstance(e, dict) or e.get("scope") != "user" for e in entries):
            return None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    listed = _install_paths(entries)
    if listed is None or len(listed) != 1:
        return None
    real = listed.pop()
    if real == here or os.path.dirname(real) != folder or not _complete(real):
        return None
    return real


def ledger_key(root: str) -> str:
    """The ledger's key for the plugin a cache folder holds: Claude Code caches
    `<marketplace>/<plugin>/<version>` and keys it `<plugin>@<marketplace>`."""
    folder = os.path.dirname(os.path.realpath(root))
    return os.path.basename(folder) + "@" + os.path.basename(os.path.dirname(folder))


def _install_paths(entries) -> "set | None":
    """The install folders (resolved) a ledger key's entries name, whatever
    their scope, or None for anything but a non-empty list of entries."""
    if not isinstance(entries, list) or not entries:
        return None
    try:
        return {os.path.realpath(e["installPath"]) for e in entries}
    except (KeyError, TypeError, AttributeError, ValueError):
        # ValueError: a path with a NUL byte in it.
        return None


def installed_copies(env) -> "dict | None":
    """Claude Code's install ledger as `{"<plugin>@<marketplace>": {install
    folders}}`, or None when it cannot be read (absent, not a file, too big,
    not the shape it has)."""
    raw = _read_ledger(_ledger(env))
    if raw is None:
        return None
    import json

    try:
        plugins = json.loads(raw.decode("utf-8"))["plugins"]
    except (ValueError, KeyError, TypeError):
        return None
    if not isinstance(plugins, dict):
        return None
    found = {}
    for key, entries in plugins.items():
        paths = _install_paths(entries)
        if isinstance(key, str) and paths:
            found[key] = paths
    return found


def is_installed_copy(root: str, env, copies: "dict | None" = None) -> bool:
    """Is `root` THE copy Claude Code has installed of its plugin: every entry
    the install ledger keeps under its `<plugin>@<marketplace>`, whatever its
    scope, names this one folder? False when the ledger names another copy or
    several, does not name this one (a `--plugin-dir` checkout, a folder outside
    the cache), or cannot be read. `copies`: `installed_copies`, already read.

    Asked before writing what every session reads (`root_skills_sync.py
    skill-call`): after `hand_over` the installed copy runs the event, and a
    copy that could not hand over is an older one, whose text must not
    overwrite the installed one's."""
    copies = installed_copies(env) if copies is None else copies
    here = os.path.realpath(root)
    return bool(copies) and copies.get(ledger_key(here)) == {here}


def hand_over(argv: list) -> None:
    """Run this event in the installed copy (`installed_root`) instead, in this
    process: its dispatch.py is imported in place of this one, with sys.path[0]
    (where its routes import their modules from) and the harness's root
    variable (`root_variable`: where its routes' commands run from) pointing at
    it, and its `run` takes the event; stdin is still unread. If it will not
    import, the event runs here. Once its `run` starts, the event is its own,
    exits and tracebacks included.

    Never for a queued event's run (`RECORD_KEEPING`): it belongs to the
    folder that queued it, and another copy (a rollback's older dispatcher)
    may know no `--record-keeping` and run every route, claiming messages and
    audit lines whose output nobody reads."""
    if os.environ.pop(HANDED_OVER, None) is not None:
        return
    if RECORD_KEEPING in argv[1:]:
        return
    try:
        root = os.path.dirname(HERE)
        target = installed_root(root, os.environ)
        if target is None:
            return
        name = __name__
        if name == "__main__":
            return
        mine = sys.modules.pop(name, None)
        sys.path[0] = os.path.join(target, "hooks")
        try:
            target_module = __import__(name)
            entry = target_module.run
            # Its clock is this process's: the hook started when this one did.
            target_module.PROCESS_STARTED = PROCESS_STARTED
        except Exception:  # noqa: BLE001 - a copy that will not import: run here
            sys.path[0] = HERE
            sys.modules.pop(name, None)
            if mine is not None:
                sys.modules[name] = mine
            return
        os.environ[root_variable(os.environ)] = target
        os.environ[HANDED_OVER] = root
    except Exception:  # noqa: BLE001 - fail open: run the event here
        return
    entry(argv)
    sys.exit(0)


def routes(lane: str, payload: dict, wiring: dict) -> list[dict]:
    """The hooks to run for this lane and payload, in routes.json order."""
    event = LANE_EVENT[lane]
    tool = payload.get("tool_name") if isinstance(payload.get("tool_name"), str) else ""
    chosen = []
    for group in wiring.get("hooks", {}).get(event, []):
        if group.get("lane") != lane or not matches(group.get("matcher"), tool):
            continue
        for hook in group.get("hooks", []):
            if hook.get("type", "command") == "command" and isinstance(hook.get("command"), str):
                chosen.append(hook)
    return chosen


def _claude_session_end_s() -> float:
    """Claude Code's whole SessionEnd phase, in seconds (its variable, else 1.5 s)."""
    raw = (os.environ.get("CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS") or "").strip()
    try:
        ms = int(raw) if raw.isascii() and raw.isdigit() else 0
    except ValueError:
        ms = 0
    return (ms or CLAUDE_SESSION_END_DEFAULT_MS) / 1000.0


def budget(lane: str) -> float:
    """Seconds this dispatcher may spend before printing what it has."""
    if lane == "session-end":
        if os.environ.get("PLUGIN_ROOT"):
            return CODEX_SESSION_END_S - CODEX_SESSION_END_MARGIN_S
        return max(_claude_session_end_s() - SESSION_END_MARGIN_S, MIN_SESSION_END_S)
    timeout = ENTRY_TIMEOUT.get(lane)
    return (HARNESS_DEFAULT_TIMEOUT_S if timeout is None else timeout) - MARGIN_S


def harness_limit(lane: str) -> float:
    """Seconds the harness gives this event, from the hook's start, before it
    cancels or kills it: Claude Code's SessionEnd phase, Codex's SessionEnd
    kill (CODEX_SESSION_END_S), every other lane its frozen entry's timeout."""
    if lane == "session-end":
        return CODEX_SESSION_END_S if os.environ.get("PLUGIN_ROOT") else _claude_session_end_s()
    timeout = ENTRY_TIMEOUT.get(lane)
    return float(HARNESS_DEFAULT_TIMEOUT_S if timeout is None else timeout)


#: The `probe` commands a route runs wait for an update in progress only until
#: this (epoch seconds; `probe.service.barrier.DEADLINE_ENV`): a hook that gives
#: up busy beats one the harness kills at its timeout (plan D12).
DEADLINE_ENV = "PROBE_HOOK_DEADLINE"


def _deadline(seconds: float) -> None:
    """Tell every `probe` this event's routes start when the event must be done.
    Never later than a deadline already set (a route that runs a hook)."""
    deadline = time.time() + max(0.0, seconds)
    try:
        inherited = float(os.environ.get(DEADLINE_ENV) or "inf")
    except ValueError:
        inherited = float("inf")
    os.environ[DEADLINE_ENV] = repr(min(deadline, inherited))


def _deadline_left() -> float:
    """Seconds until the deadline `_deadline` set (inf when none is set)."""
    try:
        return float(os.environ.get(DEADLINE_ENV) or "inf") - time.time()
    except ValueError:
        return float("inf")


def _own_timeout(hook: dict):
    timeout = hook.get("timeout")
    return timeout if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0 else None


# --- MERGE ---------------------------------------------------------------------


def _parse(text: str):
    import json

    text = text.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
    except ValueError:
        return text
    return value if isinstance(value, dict) else text


def _vetoes(text: str) -> bool:
    value = _parse(text)
    if not isinstance(value, dict):
        return False
    specific = value.get("hookSpecificOutput")
    return value.get("decision") == "block" or value.get("continue") is False or (
        isinstance(specific, dict) and specific.get("permissionDecision") == "deny"
    )


def merge(event: str, outputs: list[str]) -> str:
    """Several routes' stdout -> the one output the harness reads.

    One non-empty output passes through byte for byte. More, each read on its
    own (one malformed output never costs the others theirs): additionalContext
    joined in route order (a bare stdout is context where Claude Code reads it
    so); the strongest permission decision wins (deny > ask > allow) with the
    reasons given for it; systemMessage joined; `continue: false` and a `block`
    decision win; `hookSpecificOutput` only for events that take it.
    """
    present = [o for o in outputs if o.strip()]
    if len(present) <= 1:
        return present[0] if present else ""
    import json

    merged: dict = {}
    special: dict = {}
    contexts: list[str] = []
    messages: list[str] = []
    decision = None
    reasons: dict = {}
    block_reasons: list[str] = []
    for raw in present:
        try:
            value = _parse(raw)
            if isinstance(value, str):
                if event in _PLAIN_CONTEXT_EVENTS:
                    contexts.append(value)
                continue
            for key, item in value.items():
                if key == "hookSpecificOutput" and isinstance(item, dict):
                    said = item.get("permissionDecision")
                    said = said if isinstance(said, str) and said in _PRECEDENCE else None
                    if said and (decision is None or _PRECEDENCE[said] > _PRECEDENCE[decision]):
                        decision = said
                    why = item.get("permissionDecisionReason")
                    if said and isinstance(why, str) and why.strip():
                        reasons.setdefault(said, []).append(why)
                    for skey, sitem in item.items():
                        if skey == "additionalContext":
                            if isinstance(sitem, str) and sitem.strip():
                                contexts.append(sitem)
                        elif skey not in ("hookEventName", "permissionDecision", "permissionDecisionReason"):
                            special.setdefault(skey, sitem)
                elif key == "systemMessage":
                    if isinstance(item, str) and item.strip():
                        messages.append(item)
                elif key == "continue":
                    merged["continue"] = bool(merged.get("continue", True)) and bool(item)
                elif key == "decision":
                    if item == "block":
                        merged["decision"] = "block"
                        if isinstance(value.get("reason"), str):
                            block_reasons.append(value["reason"])
                    else:
                        merged.setdefault("decision", item)
                elif key != "reason":
                    merged.setdefault(key, item)
        except Exception:  # noqa: BLE001 - one bad output must not cost the rest
            continue
    if block_reasons:
        merged["reason"] = "\n\n".join(block_reasons)
    if messages:
        merged["systemMessage"] = "\n".join(messages)
    if event in _SPECIFIC_EVENTS and (contexts or decision or special):
        out = {"hookEventName": event}
        if decision:
            out["permissionDecision"] = decision
            if reasons.get(decision):
                out["permissionDecisionReason"] = "\n\n".join(reasons[decision])
        if contexts:
            out["additionalContext"] = "\n\n".join(contexts)
        for skey, sitem in special.items():
            out.setdefault(skey, sitem)
        merged["hookSpecificOutput"] = out
    return json.dumps(merged) + "\n" if merged else ""


# --- RUN -----------------------------------------------------------------------

#: The python-route shape, from the old hooks.json: optional PROBE_* exports,
#: the stale-root recovery, a file check, Codex's PROBE_AGENT export, the exec.
_EXPORT = r"export (PROBE_[A-Z_]+=[a-z_]+); "
_SCRIPT = r'exec "\$PY" "\$ROOT/hooks/([A-Za-z0-9_]+\.py)"((?: [a-z][a-z-]*)*)\'$'
_CODEX_AGENT = '[ -n "${PLUGIN_ROOT:-}" ] && export PROBE_AGENT=codex; '
#: What that clause exports, read off the clause itself (`KEY=value`).
_CODEX_EXPORT = _CODEX_AGENT.split("export ", 1)[1].rstrip("; ")


def _bash() -> str:
    """The bash a route's own command line runs under: `/bin/bash`, and on
    Windows the Git Bash this dispatcher was started from (first on the PATH
    it handed down; never System32's WSL launcher), where `/bin/bash` is no
    file a native Python can start."""
    if os.name != "nt":
        return "/bin/bash"
    import shutil

    system = os.path.normcase(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        found = shutil.which("bash", path=folder) if folder else None
        if found and not os.path.normcase(found).startswith(system):
            return found
    return "bash"


def _plain_command(exports: list, script: str, codex: bool, args: list) -> str:
    return (
        "/bin/bash -c '"
        + "".join(f"export {e}; " for e in exports)
        + 'ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"; ROOT="${ROOT%/}"; '
        + f'if [ ! -f "$ROOT/hooks/{script}" ] && [ -n "${{PLUGIN_ROOT:-}}" ]; then '
        + 'ROOT="$(ls -dt "${ROOT%/*}"/[0-9]*.[0-9]*/.codex-plugin/plugin.json 2>/dev/null | head -1)"; '
        + 'ROOT="${ROOT%/.codex-plugin/plugin.json}"; [ -n "$ROOT" ] && export PLUGIN_ROOT="$ROOT"; fi; '
        + f'[ -f "$ROOT/hooks/{script}" ] || exit 0; '
        + (_CODEX_AGENT if codex else "")
        + 'PY="$(command -v python3)" || exit 0; '
        + f'exec "$PY" "$ROOT/hooks/{script}"'
        + "".join(f" {a}" for a in args)
        + "'"
    )


def plain_route(command: str):
    """(exports, script, codex, args) when `command` is exactly a plain python
    route, else None. Exact: the parts are rebuilt into the whole string."""
    import re

    found = re.search(_SCRIPT, command)
    if not found:
        return None
    prefix = command[len("/bin/bash -c '") :].split('ROOT="', 1)[0]
    exports = re.findall(_EXPORT, prefix)
    codex = _CODEX_AGENT in command
    args = found.group(2).split()
    parts = (exports, found.group(1), codex, args)
    return parts if _plain_command(*parts) == command else None


def _arm_alarm(seconds) -> None:
    """Bound an in-process route by its own timeout, as the harness did:
    SIGALRM's default action ends the process."""
    if not seconds:
        return
    import signal

    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, signal.SIG_DFL)
        signal.alarm(max(1, int(round(seconds))))
        return
    # Windows has no SIGALRM: a timer thread ends the process the same way
    # (fail open: the exit the harness's own timeout would have left).
    import threading

    timer = threading.Timer(seconds, lambda: os._exit(0))
    timer.daemon = True
    timer.start()


def run_inline(parts, payload: bytes, timeout=None) -> None:
    """Run a plain python route in this process, as its bash line would have."""
    import io
    import runpy

    exports, script, codex, args = parts
    for export in exports:
        key, _, value = export.partition("=")
        os.environ[key] = value
    if codex and os.environ.get("PLUGIN_ROOT"):
        key, _, value = _CODEX_EXPORT.partition("=")
        os.environ[key] = value
    path = os.path.join(HERE, script)
    sys.argv = [path, *args]
    # newline="\n": what CPython gives a POSIX stdin, so a CR reads as a CR.
    sys.stdin = io.TextIOWrapper(io.BytesIO(payload), encoding="utf-8", newline="\n")
    _arm_alarm(timeout)
    runpy.run_path(path, run_name="__main__")


def _route_argv(hook: dict) -> "tuple[list, dict | None]":
    """What a route's child runs, and its environment (None: this process's).
    Its bash line, under `_bash()`; on Windows a plain python route starts its
    python at once (what its bash line would exec), since every bash start
    there costs a few hundred milliseconds more, on every event."""
    command = hook["command"]
    if os.name == "nt":
        parts = plain_route(command)
        if parts is not None:
            exports, script, codex, args = parts
            path = os.path.join(HERE, script)
            if os.path.isfile(path) and sys.executable:
                env = dict(os.environ)
                for export in exports:
                    key, _, value = export.partition("=")
                    env[key] = value
                if codex and env.get("PLUGIN_ROOT"):
                    key, _, value = _CODEX_EXPORT.partition("=")
                    env[key] = value
                return [sys.executable, path, *args], env
    return [_bash(), "-c", command], None


_WINDOWS = os.name == "nt"
#: Windows: how long ending routes' process trees may take, all of them at once
#: (one `taskkill` each, started together), before each route is ended itself.
KILL_TREE_S = 0.5
_KILLED: dict = {}


def _kill_routes(children) -> None:
    """End routes and what they started. POSIX: each one's process group (each
    route leads its own). Windows has no group to signal (`start_new_session`
    is ignored there, and `os.killpg` does not exist): each one's process
    tree by pid (its bash's children included), every route's at once and
    for KILL_TREE_S at most, then the route itself; a route is ended once
    however many threads ask (the waiter, the final sweep, a signal)."""
    import subprocess

    live = [child for child in children if child is not None and child.poll() is None]
    if not _WINDOWS:
        import signal

        for child in live:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except OSError:
                pass
        return
    mine = object()
    # setdefault is one step under the GIL: of threads asking at once, one wins.
    fresh = [child for child in live if _KILLED.setdefault(child.pid, mine) is mine]
    trees = []
    for child in fresh:
        try:
            trees.append(subprocess.Popen(  # noqa: S603,S607 -- Windows' own taskkill
                ["taskkill", "/F", "/T", "/PID", str(child.pid)], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=0x08000000))  # CREATE_NO_WINDOW: no console flashes up
        except OSError:
            pass
    end = time.monotonic() + KILL_TREE_S
    for tree in trees:
        try:
            tree.wait(timeout=max(end - time.monotonic(), 0.0))
        except subprocess.TimeoutExpired:
            pass  # still walking a slow machine's tree: it finishes on its own
    for child in fresh:
        try:
            child.kill()
        except OSError:
            pass


def run_all(chosen: list[dict], payload: bytes, deadline: float):
    """Every route as a child, in parallel. -> [(stdout, exit code or None)].

    Each route runs in its own process group, so an overrun kills everything it
    started that stayed in that group (what detaches on purpose, a setsid'd
    sender, is left to run as it always was). Each child's stderr comes back
    through this process and is forwarded in route order, never handed the
    harness's own pipe. A route that has exited counts as done even if something
    it left behind still holds its output pipe. Told to stop (SIGTERM, SIGINT,
    SIGHUP), this kills every route's group first, including one it was still
    starting."""
    import signal
    import subprocess
    import threading

    results: list = [("", None)] * len(chosen)
    errors: list = [b""] * len(chosen)
    children: list = []

    def kill(child) -> None:
        _kill_routes([child])

    stopping = []
    starting = [True]

    def stop_now() -> None:
        _kill_routes(children)
        os._exit(0)

    def stop(signum, _frame) -> None:
        stopping.append(signum)
        if not starting[0]:
            stop_now()

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)

    for hook in chosen:
        try:
            argv, env = _route_argv(hook)
            child = subprocess.Popen(  # noqa: S603 - the routes' own commands
                argv,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            child = None
        children.append(child)
        if stopping:
            # Told to stop between a fork and the list: this route is in it now.
            stop_now()
    starting[0] = False
    if stopping:
        stop_now()

    def feed(pipe) -> None:
        try:
            pipe.write(payload)
        except (OSError, ValueError):
            pass
        finally:
            try:
                pipe.close()
            except (OSError, ValueError):
                pass

    def wait(index: int, child, own) -> None:
        end = deadline if own is None else min(deadline, time.monotonic() + own)
        # The payload goes in from its own thread: communicate() only sends
        # input on its first call, and the loop below calls it again every
        # POLL_S, so a route slower to read than that would never get the rest.
        pipe, child.stdin = child.stdin, None
        threading.Thread(target=feed, args=(pipe,), daemon=True).start()
        while True:
            left = end - time.monotonic()
            try:
                out, err = child.communicate(timeout=max(min(POLL_S, left), 0.0))
                results[index] = (out.decode("utf-8", "replace"), child.returncode)
                errors[index] = err or b""
                return
            except subprocess.TimeoutExpired as late:
                if child.poll() is not None:
                    # Exited. Read once more for what it wrote on the way out; if
                    # something it started still holds the pipe, what came is its output.
                    try:
                        out, err = child.communicate(timeout=POLL_S)
                    except subprocess.TimeoutExpired as last:
                        out, err = last.stdout, last.stderr
                    except (OSError, ValueError):
                        out, err = late.stdout, late.stderr
                    results[index] = ((out or b"").decode("utf-8", "replace"), child.returncode)
                    errors[index] = err or b""
                    return
                if left <= 0:
                    kill(child)
                    try:
                        _, err = child.communicate(timeout=KILL_WAIT_S)
                        errors[index] = err or b""
                    except (subprocess.TimeoutExpired, OSError, ValueError):
                        pass
                    return
            except (OSError, ValueError):
                return

    threads = []
    for index, (hook, child) in enumerate(zip(chosen, children)):
        if child is None:
            continue
        thread = threading.Thread(target=wait, args=(index, child, _own_timeout(hook)), daemon=True)
        thread.start()
        threads.append(thread)
    for thread in threads:
        thread.join(max(deadline - time.monotonic(), 0.0) + KILL_WAIT_S + JOIN_SLACK_S)
    _kill_routes(children)
    for err in errors:
        if err:
            try:
                sys.stderr.buffer.write(err)
                sys.stderr.flush()
            except (OSError, ValueError):
                pass
    return results


def _is_catch_all(hook: dict, lane: str, wiring: dict) -> bool:
    for group in wiring.get("hooks", {}).get(LANE_EVENT[lane], []):
        if group.get("lane") == lane and not group.get("matcher") and hook in group.get("hooks", []):
            return True
    return False


def waiting_after_tool(lane: str, options) -> bool:
    """On post-tool: does a daemon message wait for this event to deliver it?
    Never when the harness shows no one the output (UNSHOWN)."""
    return lane == "post-tool" and UNSHOWN not in options and reads_waiting()


def record_keeping(hook: dict) -> bool:
    """Is this route record-keeping (routes.json `"record_keeping": true`)?
    Nothing waits on its outcome: no decision, no context, no message. On a
    host miss it is queued for the Probe service instead of run here (plan
    D36, `Miss`). tests/test_hook_dispatch.py holds which routes say so."""
    return hook.get("record_keeping") is True


def main(argv: list[str], payload: "bytes | None" = None, miss: "Miss | None" = None):
    """0/2 (an exit code), or (route, payload, timeout) to run in-process.
    `payload`: the event, when the caller read it already (`run`, the host).
    `miss`: no warm host took this event (`serve`); its record-keeping routes
    go to the Probe service when it takes them (`Miss.queue`).
    argv: `dispatch.py <lane> [UNSHOWN] [RECORD_KEEPING]`; RECORD_KEEPING (the
    service running a queued event) runs only the record-keeping routes."""
    if len(argv) < 2 or argv[1] not in LANE_EVENT:
        return 0
    lane = argv[1]
    if payload is None:
        payload = sys.stdin.buffer.read()
    kept_only = RECORD_KEEPING in argv[2:]
    waiting = not kept_only and waiting_after_tool(lane, argv[2:])
    if lane in TOOL_LANES and not waiting and not could_route(lane, payload):
        return 0
    import json

    try:
        with open(os.path.join(HERE, "routes.json"), encoding="utf-8") as handle:
            wiring = json.load(handle)
    except (OSError, ValueError):
        wiring = None
    try:
        parsed = json.loads(payload.decode("utf-8", "replace") or "{}")
    except ValueError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    if miss is not None:
        miss.parsed = parsed
    heartbeat(lane, parsed)
    if not isinstance(wiring, dict):
        return 0
    chosen = routes(lane, parsed, wiring)
    if kept_only:
        chosen = [h for h in chosen if record_keeping(h)]
    elif lane == "post-tool" and not waiting:
        # The tool lanes' only catch-all is the reads route behind this same
        # fast path (tests/test_hook_dispatch.py holds that): nothing waits,
        # so its shell would only check again and exit.
        chosen = [h for h in chosen if not _is_catch_all(h, lane, wiring)]
        if not chosen:
            return 0
    if miss is not None and any(record_keeping(h) for h in chosen) and miss.queue(lane, payload, parsed):
        # D36: the service runs them, once, by this event's id; here only
        # what the agent waits on (a decision, context, a message).
        chosen = [h for h in chosen if not record_keeping(h)]
    if not chosen:
        return 0
    limit = budget(lane)
    if miss is not None:
        # The entry's timeout runs from the harness's start: what the miss
        # spent (the host's wait, the service's) is not the routes' to spend.
        limit = max(limit - (time.monotonic() - miss.started), MIN_SESSION_END_S)
    if lane == "session-end":
        # The harness's clock started before this process did, and a host
        # that never answered spent part of it (`serve` set the deadline).
        limit = max(min(limit, _deadline_left()), MIN_SESSION_END_S)
    if len(chosen) == 1:
        parts = plain_route(chosen[0]["command"])
        if parts is not None and os.path.isfile(os.path.join(HERE, parts[1])):
            own = _own_timeout(chosen[0])
            timeout = limit if own is None else min(own, limit)
            _deadline(timeout)
            return (parts, payload, timeout)
    _deadline(limit)
    results = run_all(chosen, payload, time.monotonic() + limit)
    outputs = [out for out, code in results if code == 0]
    try:
        text = merge(LANE_EVENT[lane], outputs)
    except Exception:  # noqa: BLE001 - never lose every route's output to a merge bug
        # A veto first: a merge bug must never cost the guard its denial.
        present = [o for o in outputs if o.strip()]
        text = next((o for o in present if _vetoes(o)), present[0] if present else "")
    if text:
        sys.stdout.write(text)
        sys.stdout.flush()
    return 2 if any(code == 2 for _, code in results) else 0


# --- THE HOST (plan A0) ----------------------------------------------------------

#: Hand the event to this folder's warm host (`zygote.py`, run by the Probe
#: service) when it is ready within this; else run the event here (plan D28).
HOST_WAIT_S = 0.3
#: Lanes never handed to the host: SessionEnd. Claude Code gives the whole
#: phase 1.5 s (Codex 3 s) and waits for its output pipes to close: a host
#: holds the harness's stdout and stderr (they ride the event to it), so a
#: stalled host kept the phase open past the dispatcher's own cutoff (2.0 s
#: measured against 1.5 s, #3300 review). It runs here, all of its routes,
#: as through plugin 0.164 (no warm host, so no miss and nothing queued).
UNHOSTED_LANES = frozenset({"session-end"})
#: `off`: never hand an event to the host (a test, a fault being chased).
HOST_ENV = "PROBE_HOOK_HOST"
#: Set in the host's children: an event already hosted is never handed on.
HOSTED_ENV = "PROBE_HOOK_HOSTED"
#: A miss asks the service for a host at most this often.
ASK_EVERY_S = 30.0


def host_socket(root: str) -> str:
    """`/tmp/probe-<uid>/hooks-<hash of the folder>.sock`: this machine's own
    (a home folder shared by many machines never shares it), private to the user."""
    import hashlib

    digest = hashlib.sha256(os.fsencode(os.path.realpath(root))).hexdigest()[:16]
    return os.path.join("/tmp", "probe-%d" % os.getuid(), "hooks-%s.sock" % digest)


def _private(folder: str) -> bool:
    try:
        st = os.lstat(folder)
    except OSError:
        return False
    import stat

    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o022


def hostable(lane: str) -> bool:
    """Can this event be handed to a warm host at all? Never with hosting off,
    in a host's own child, off POSIX, below Python 3.9 (`send_fds`), on an
    unhosted lane, or in a folder with no host."""
    if os.environ.get(HOST_ENV) == "off" or os.environ.get(HOSTED_ENV) or os.name != "posix":
        return False
    if sys.version_info < (3, 9) or lane in UNHOSTED_LANES:
        return False
    return os.path.isfile(os.path.join(HERE, "zygote.py"))


def host_end(lane: str) -> float:
    """When (monotonic) a hosted event's client stops waiting for the host:
    the harness's own limit for the event (`harness_limit`), counted from this
    process's start (PROCESS_STARTED, less STARTUP_ALLOWANCE_S for bash and the
    interpreter before it), so a stalled host never holds the hook past what
    the harness gives it: 1.3 s of Claude Code's 1.5 s SessionEnd, 2.8 s of
    Codex's 3 s. Past it the event stays the host's, whose child ends its
    routes by the event's own deadline."""
    return PROCESS_STARTED + harness_limit(lane) - STARTUP_ALLOWANCE_S


def via_host(lane: str, argv: list, payload: bytes, started: "float | None" = None) -> "int | None":
    """Run this event in the folder's warm host, exactly once (zygote.py's
    handshake): its exit status, or None to run it here. None whenever the host
    is not there or not ready in time (a miss: `Miss` then asks the service for
    one); once it said ready and was told to go, the event is the host's, and a
    host lost after that, or still silent at `host_end`, reads as exit 0.
    `started`: when the event began here (monotonic; default now), for the
    D28 wait; every wait is bounded by `host_end`."""
    if not hostable(lane):
        return None
    root = os.path.dirname(HERE)
    path = host_socket(root)
    wait = HOST_WAIT_S
    started = time.monotonic() if started is None else started
    end = host_end(lane)

    def left() -> float:
        return max(end - time.monotonic(), 0.001)

    import socket

    if not _private(os.path.dirname(path)):
        return None
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(min(wait, left()))
        try:
            sock.connect(path)
        except OSError:
            return None
        import json

        env = dict(os.environ)
        env[HOSTED_ENV] = "1"
        header = {
            "v": 1, "argv": list(argv), "cwd": os.getcwd(), "env": env, "parent": os.getppid(),
            "deadline": time.time() + budget(lane) + MARGIN_S, "payload": len(payload),
        }
        data = json.dumps(header).encode("utf-8") + b"\n" + payload
        try:
            # The fds ride the first send, which may take less than it was given.
            sent = socket.send_fds(sock, [data[:65536]], [1, 2])
            if sent < len(data):
                sock.sendall(data[sent:])
            sock.settimeout(max(0.01, min(wait - (time.monotonic() - started), left())))
            if sock.recv(1) != b"R":
                return None
            sock.sendall(b"G")
        except OSError:
            return None
        # Told to go: from here only an explicit `N` (it will not run it:
        # stopping, no fork) runs it here. A late `S`, a timeout or a lost host
        # may mean it runs there: never here too.
        try:
            sock.settimeout(left())
            answer = sock.recv(1)
        except OSError:
            return 0
        if answer == b"N":
            return None
        if answer != b"S":
            return 0
        # From here the event is the host's.
        try:
            reply = b""
            while not reply.endswith(b"\n"):
                sock.settimeout(left())
                chunk = sock.recv(4096)
                if not chunk:
                    return 0
                reply += chunk
            code = json.loads(reply.decode("utf-8")).get("exit")
            return code if isinstance(code, int) and 0 <= code < 256 else 0
        except (OSError, ValueError, AttributeError):
            return 0
    finally:
        sock.close()


def run_hosted(argv: list, payload: bytes) -> None:
    """The event inside the host's child (`zygote.py`): what `run` does after
    the hand-over, with the payload the client read."""
    try:
        code = main(["dispatch.py", *argv], payload=payload)
    except Exception:  # noqa: BLE001 - fail open: a broken dispatcher abstains
        code = 0
    if isinstance(code, tuple):
        run_inline(*code)
        code = 0
    sys.exit(code)


# --- A MISS (plan D35, D36) ------------------------------------------------------

#: The lane option the Probe service runs a queued event with: its record-keeping
#: routes only (`main`). The service hands it to the host's child, or to
#: `dispatch.sh`, which passes every argument on.
RECORD_KEEPING = "--record-keeping"
#: A miss gives the Probe service this long, from the connect, to take its
#: record-keeping routes (`hooks.queue`).
QUEUE_WAIT_S = 0.3
#: A queued event (its id, folder, lane, payload, environment and working
#: folder, as one JSON object) is at most this big (`protocol.MAX_HOOK_QUEUE_ITEM`),
#: and at most MAX_QUEUED wait in the queue (`hookqueue.MAX_QUEUED`): past
#: either, its routes run here, as before. Its environment and folder are held
#: to `protocol.need_hook_queue`'s limits too.
MAX_QUEUED_BYTES = 256 * 1024
MAX_QUEUED = 128
MAX_QUEUED_ENV = 1024
MAX_QUEUED_PATH = 4096
#: A miss that found no service does not look for one again for this long:
#: the misses right after it run as before, at the cost of a stat (a session
#: set off, or a machine whose service will not start, pays no more than that).
DOWN_RETRY_S = 2.0
#: With no service answering, a miss starts one at most this often (the CLI's
#: own client, `client.ensure`, in a detached process: `ensure_service`).
ENSURE_EVERY_S = 10.0
#: What this dispatcher sets in its own environment: never part of a queued
#: event's (the service sets its own when it runs it).
OWNED_ENV = (HANDED_OVER, HOSTED_ENV, HOST_ENV, DEADLINE_ENV, "PROBE_HOOK_PARENT_PID")
#: What a queued event keeps of its environment (`protocol.hook_queue_env`; a
#: test holds the two equal): what a record-keeping route reads, never a
#: credential but Probe's own, nor a proxy URL with a user and password in it
#: (such an event runs here: `proxy_credentials`). The queue is a file on disk.
QUEUE_ENV_NAMES = frozenset(
    {
        "HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TERM",
        "PLUGIN_ROOT", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR", "PIPX_HOME",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
    }
)
QUEUE_ENV_PREFIXES = ("PROBE_", "CLAUDE", "CODEX_", "KIMI_", "PI_", "XDG_", "LC_")
QUEUE_ENV_SECRET = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "COOKIE", "AUTH")
QUEUE_ENV_CREDENTIAL = "PROBE_TOKEN"
#: Where a Probe CLI install shows, under $HOME (`paths.runtime_path`,
#: `paths.fallback_python`; tests/test_service_hooks_queue.py holds them equal):
#: with neither, no service ever runs here, and a miss asks nothing of one.
CLI_MARKS = (
    (".local", "state", "probe", "service", "runtime.json"),
    (".local", "share", "uv", "tools", "probe-research", "bin", "python"),
)


def cli_here() -> bool:
    """Is a Probe CLI installed for this user? Two stats, no import: what a
    machine with the plugin and no CLI pays for every miss."""
    home = os.path.expanduser("~")
    return any(os.path.exists(os.path.join(home, *mark)) for mark in CLI_MARKS)


def _proxy_userinfo(name: str, value: str) -> bool:
    """Is this a proxy variable whose URL carries a user (and a password):
    `http://user:secret@proxy:8080`, `user@proxy`? A credential, never queued."""
    if not name.upper().endswith("_PROXY"):
        return False
    return "@" in value.split("://", 1)[-1].split("/", 1)[0]


def proxy_credentials(environ) -> bool:
    """Does this environment reach the network through a proxy with credentials?
    Its event needs them, and they are never stored: it runs here."""
    return any(_proxy_userinfo(name, value) for name, value in environ.items())


def queue_env(environ) -> dict:
    """The part of this event's environment a queued event keeps."""
    kept = {}
    for name, value in environ.items():
        if name in OWNED_ENV or _proxy_userinfo(name, value):
            continue
        if name != QUEUE_ENV_CREDENTIAL:
            upper = name.upper()
            if any(word in upper for word in QUEUE_ENV_SECRET):
                continue
            if name not in QUEUE_ENV_NAMES and not name.startswith(QUEUE_ENV_PREFIXES):
                continue
        kept[name] = value
    return kept


def service_mark(kind: str) -> str:
    """`/tmp/probe-<uid>/service-<hash of $HOME>.<kind>`: a miss's own marks
    (`down`, `ensured`), in the hosts' private folder, one set per HOME, read
    with a stat and no import of the service's paths."""
    import hashlib

    home = os.path.realpath(os.path.expanduser("~"))
    digest = hashlib.sha256(os.fsencode(home)).hexdigest()[:16]
    return os.path.join("/tmp", "probe-%d" % os.getuid(), "service-%s.%s" % (digest, kind))


def _fresh(path: str, seconds: float) -> bool:
    try:
        return time.time() - os.stat(path).st_mtime < seconds
    except OSError:
        return False


def _touch(path: str) -> bool:
    """Mark `path` now, in a private folder only (made if missing)."""
    folder = os.path.dirname(path)
    try:
        if not os.path.isdir(folder):
            os.makedirs(folder, mode=0o700, exist_ok=True)
        if not _private(folder):
            return False
        with open(path, "a"):
            pass
        os.utime(path, None)
        return True
    except OSError:
        return False


class _Error:
    """An error answer (an older service with no such method, a hello it refused)."""


_ERROR = _Error()


def _service_paths():
    """`_service_paths.py` beside this file: the service's own `paths.py`."""
    if HERE not in sys.path:
        sys.path.insert(1, HERE)
    import _service_paths  # noqa: PLC0415 - only on a miss

    return _service_paths


def _runtime(sp) -> dict:
    """runtime.json (`client.write_runtime`): the CLI's interpreter and version."""
    import json

    raw = _read_ledger(str(sp.runtime_path()))
    try:
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def runtime_python(sp) -> "str | None":
    """The interpreter the service starts with, as `client.runtime_python`
    finds it (runtime.json's, else the uv tool env's); None: no CLI here, so
    no service will ever start."""
    candidates = []
    python = _runtime(sp).get("python")
    if isinstance(python, str):
        candidates.append(python)
    candidates.append(str(sp.fallback_python()))
    for candidate in candidates:
        if os.path.isabs(candidate) and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def session_off(payload: dict) -> bool:
    """Is this session's Probe switch `off`, by its own state or else the
    machine's default (as the guard reads it)? A session set off never starts
    the Probe service. True when it cannot be read: nothing is started then."""
    try:
        if HERE not in sys.path:
            sys.path.insert(1, HERE)
        import _session_marker as marker  # noqa: PLC0415 - only on a miss with no service

        sid = payload.get("session_id")
        state = marker.session_state(sid) if isinstance(sid, str) else None
        if state is None:
            cwd = payload.get("cwd")
            state, _source = marker.resolve_state_default(cwd if isinstance(cwd, str) and cwd else None)
        return state == marker.STATE_OFF
    except Exception:  # noqa: BLE001 - unreadable: start nothing
        return True


def ensure_service(sp, allowed) -> None:
    """No service answered: start one for the next event, at most every
    ENSURE_EVERY_S (the `ensured` mark: set before `allowed()` is asked, so a
    session set off pays its costly check at most that often too), when
    `allowed()` (the session's switch). The CLI's own client does it
    (`client.ensure`: its start lock, its back-off, its clean environment), in
    a detached process this never waits on, so the event goes on at once
    (plan D35)."""
    marker = service_mark("ensured")
    if _fresh(marker, ENSURE_EVERY_S):
        return
    python = runtime_python(sp)
    if python is None or not _touch(marker) or not allowed():
        return
    import subprocess

    env = {k: v for k, v in os.environ.items() if k not in OWNED_ENV}
    try:
        subprocess.Popen(  # noqa: S603 - the CLI's own interpreter and client
            [python, "-I", "-m", "probe.service.client", "ensure"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, cwd="/", close_fds=True, start_new_session=True,
        )
    except OSError:
        pass


def queue_folder(sp, any_drainer: bool) -> "str | None":
    """The service's hook queue (`paths.hook_queue_dir`), when an event written
    there will run: it exists (a service that drains it made it) and, unless
    `any_drainer` (a running service was just sent the event), the service
    that made it is the CLI version the next one starts as (its
    `HOOK_QUEUE_DRAINER` stamp against runtime.json's `cli_version`): an older
    CLI's service never drains it."""
    folder = str(sp.hook_queue_dir())
    if not _private(folder):
        return None
    if any_drainer:
        return folder
    raw = _read_ledger(os.path.join(folder, sp.HOOK_QUEUE_DRAINER))
    drainer = raw.decode("utf-8", "replace").strip() if raw else ""
    return folder if drainer and drainer == _runtime(sp).get("cli_version") else None


def write_queued(folder: str, item: dict, data: bytes) -> bool:
    """`<id>.json` in the service's queue, whole or not at all (a private
    temporary file renamed into place), while fewer than MAX_QUEUED wait.
    True when it is there, or was there and taken already (`<id>.done`)."""
    name = item["id"]
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    if name + ".done" in names or name + ".json" in names:
        return True  # the service has it: one id runs once
    if sum(1 for n in names if n.endswith(".json") and not n.startswith(".")) >= MAX_QUEUED:
        return False
    tmp = os.path.join(folder, ".%s.%d.tmp" % (name, os.getpid()))
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        os.replace(tmp, os.path.join(folder, name + ".json"))
        return True
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False


def claim_event(folder: str, event_id: str) -> "bool | None":
    """Claim a sent, unanswered event for running here: `<id>.claim` in the
    service's queue, made only if it is not there (O_EXCL), which the service's
    store honors (`hookqueue.HookQueue.store` makes the same claim before it
    stores). True: claimed here, so the service never stores it late; False:
    the service claimed it first, so it runs there; None: neither can tell."""
    try:
        os.close(os.open(os.path.join(folder, event_id + ".claim"),
                         os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600))
        return True
    except FileExistsError:
        return False
    except OSError:
        return None


def queued_item(root: str, lane: str, payload: bytes) -> "tuple[dict, bytes] | None":
    """This event as `hooks.queue` takes it (and the queue file holds it): a
    new id, the folder, the lane, the payload as the harness wrote it, what
    the routes read of its environment (`queue_env`) and its working folder.
    None when it is past `protocol.need_hook_queue`'s limits (MAX_QUEUED_*):
    it then runs here."""
    if len(payload) > MAX_QUEUED_BYTES:
        return None  # a huge tool result: never encoded only to be refused
    if proxy_credentials(os.environ):
        return None  # its routes need a proxy credential the queue never stores
    import json

    root = os.path.realpath(root)
    cwd = os.getcwd()
    env = queue_env(os.environ)
    if len(env) > MAX_QUEUED_ENV or max(len(root), len(cwd)) > MAX_QUEUED_PATH:
        return None
    item = {
        "id": os.urandom(16).hex(),
        "plugin_root": root,
        "lane": lane,
        # surrogateescape: bytes that are not UTF-8 come back as they were.
        "payload": payload.decode("utf-8", "surrogateescape"),
        "env": env,
        "cwd": cwd,
        "at": time.time(),
    }
    data = json.dumps(item).encode("utf-8")
    return (item, data) if len(data) <= MAX_QUEUED_BYTES else None


class Miss:
    """An event no warm host took (`via_host` said None): what it asks of the
    Probe service, each at most once (plan D35, D36).

      queue    its record-keeping routes, for the service to run once, by this
               event's id (`hooks.queue`); with no service answering, written
               straight into the service's queue (`queue_folder`), which a
               service drains when it starts. True: they do not run here.
      finish   the folder's warm host for the next event (`hooks.host`, at most
               every ASK_EVERY_S), and, when no service answered, the service
               itself (`ensure_service`), unless the session is set off.

    A service found missing is not looked for again for DOWN_RETRY_S (its
    `down` mark), and nothing is connected to when nothing is due. Never waits
    past QUEUE_WAIT_S. Fails open: whatever goes wrong, the routes run here,
    as before."""

    #: `connect`'s answers besides a socket.
    DOWN = "down"  # no service answered: this miss may start one
    SKIPPED = "skipped"  # one was found missing moments ago: ask nothing

    def __init__(self, root: str, payload: bytes, started: float) -> None:
        self.root = root
        self.payload = payload
        #: When the event began here (monotonic): its budget counts from then.
        self.started = started
        self.parsed: "dict | None" = None
        self.sock = None
        self.state: "str | None" = None
        self.buffer = b""
        self.next_id = 1
        self.off: "bool | None" = None

    def _session_off(self) -> bool:
        if self.off is None:
            parsed = self.parsed
            if parsed is None:
                import json

                try:
                    parsed = json.loads(self.payload.decode("utf-8", "replace") or "{}")
                except ValueError:
                    parsed = {}
            self.off = session_off(parsed if isinstance(parsed, dict) else {})
        return self.off

    def connect(self):
        """The service's socket (this machine's, in a folder only this user
        can write), connected once; else DOWN (none answered, now marked so)
        or SKIPPED (none answered moments ago)."""
        if self.state is None:
            if _fresh(service_mark("down"), DOWN_RETRY_S):
                self.state = self.SKIPPED
                return self.state
            import socket

            sock = None
            try:
                sp = _service_paths()
                path = sp.socket_path()
                sp.require_private(path.parent)
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(QUEUE_WAIT_S)
                sock.connect(str(path))
                self.sock, sock = sock, None
                self.state = "connected"
            except Exception:  # noqa: BLE001 - no service: `finish` starts one
                self.state = self.DOWN
                _touch(service_mark("down"))
            finally:
                if sock is not None:
                    sock.close()
        return self.sock if self.sock is not None else self.state

    def _request(self, method: str, params: dict) -> int:
        """Send one request (the hello before the first): its id."""
        import json

        lines = []
        if self.next_id == 1:
            lines.append({"jsonrpc": "2.0", "id": 1, "method": "service.hello",
                          "params": {"client": "cli", "client_version": "hooks", "api": 1, "keepalive": False}})
            self.next_id = 2
        request_id = self.next_id
        self.next_id += 1
        lines.append({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        self.sock.sendall(b"".join(json.dumps(line).encode("utf-8") + b"\n" for line in lines))
        return request_id

    def _answer(self, request_id: int, until: float):
        """Request `request_id`'s result, _ERROR for an error answer, or None
        when none came by `until` (monotonic)."""
        import json

        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    message = json.loads(line.decode("utf-8"))
                except ValueError:
                    return None
                if isinstance(message, dict) and message.get("id") == request_id:
                    return _ERROR if "error" in message else message.get("result")
            left = until - time.monotonic()
            if left <= 0:
                return None
            self.sock.settimeout(left)
            chunk = self.sock.recv(65536)
            if not chunk:
                return None
            self.buffer += chunk

    def queue(self, lane: str, payload: bytes, parsed: dict) -> bool:
        """Hand this event's record-keeping routes to the service: True when it
        took them (or holds them in its queue), False to run them here."""
        self.parsed = parsed
        try:
            made = queued_item(self.root, lane, payload)
            if made is None:
                return False
            item, data = made
            reached = self.connect()
            if reached == self.SKIPPED:
                return False
            sp = _service_paths()
            if reached != self.DOWN:
                started = time.monotonic()
                try:
                    answer = self._answer(self._request("hooks.queue", item), started + QUEUE_WAIT_S)
                except OSError:
                    answer = None
                if answer is _ERROR:
                    return False  # a service from before hooks.queue: it never drains a queue
                if isinstance(answer, dict):
                    return answer.get("queued") is True  # a refusal runs them here
                # No answer in time: it may hold it already, so it goes beside
                # it in the queue (one id runs once). Only a write that took
                # keeps them from running here: a request sent and never
                # answered may be an older service's late refusal.
                folder = queue_folder(sp, any_drainer=True)
                if folder is None:
                    return False
                if write_queued(folder, item, data):
                    return True
                # Not written (the queue full): it runs here only once claimed
                # here, so a store still on its way never runs it too.
                # Claimed by the service, or no claim could be made (a full
                # disk): never run here too, at most once (a late store may
                # still claim and run it there).
                return claim_event(folder, item["id"]) is not True
            folder = queue_folder(sp, any_drainer=False)
            if folder is None or self._session_off():
                return False
            return write_queued(folder, item, data)
        except Exception:  # noqa: BLE001 - fail open: run them here
            return False

    def _ask_for_host(self, marker: str) -> None:
        """The folder's warm host for the next event (`hooks.host`), marked
        only once asked. Not waited on: the service starts it on a thread of
        its own."""
        folder = os.path.dirname(marker)
        if not os.path.isdir(folder):
            os.makedirs(folder, mode=0o700, exist_ok=True)
        if not _private(folder):
            return
        self._request("hooks.host", {"root": os.path.realpath(self.root), "python": sys.executable})
        _touch(marker)

    def finish(self) -> None:
        """After the event: a host for the next one (when an ask is due), or,
        with no service, the service (D35). Connects only when the queue did
        or a host ask is due (a stat, otherwise). Never raises, never waits on
        either."""
        try:
            asked = host_socket(self.root)[: -len(".sock")] + ".asked"
            host_due = not _fresh(asked, ASK_EVERY_S)
            if self.state is None and not host_due:
                # Nothing asked of the service yet and no host ask due: no
                # import, no connect. A service gone meanwhile is found by the
                # next miss with something to queue, or a host ask due.
                return
            reached = self.connect()
            if reached == self.SKIPPED:
                return
            if reached == self.DOWN:
                ensure_service(_service_paths(), lambda: not self._session_off())
            elif host_due:
                self._ask_for_host(asked)
        except Exception:  # noqa: BLE001 - only the next event's speed is at stake
            pass
        finally:
            if self.sock is not None:
                try:
                    self.sock.close()
                except OSError:
                    pass
                self.sock = None


def serve(argv: list, payload: "bytes | None" = None) -> int:
    """One event, in this folder's warm host when it answers in time, else
    here; its exit status. `argv`: `[lane, options...]`; `payload`: the event,
    when the caller read it already (`harness_io.py --event`), else stdin.

    A miss (no host took it) runs here only what the agent waits on, and hands
    the record-keeping routes to the Probe service (`Miss`, plan D36); then it
    asks for a host, or starts the service, for the next event.

    What `run` does after the hand-over, returning instead of exiting, so a
    harness adapter can run an event the way `dispatch.sh` does and still read
    what it printed. An in-process route's own exit (SystemExit) and uncaught
    error leave it as they leave `python3 <route>`."""
    lane = argv[0] if argv else ""
    started = time.monotonic()
    if lane == "session-end":
        # The harness's SessionEnd budget runs from about now, wherever the
        # routes end up running (`main` caps the lane's budget by it).
        os.environ[DEADLINE_ENV] = repr(time.time() + budget(lane))
    miss = None
    if lane in LANE_EVENT and hostable(lane):
        try:
            if payload is None:
                payload = sys.stdin.buffer.read()
            if lane in TOOL_LANES and not waiting_after_tool(lane, argv[1:]) and not could_route(lane, payload):
                return 0
            hosted = via_host(lane, argv, payload, started)
            if hosted is None and RECORD_KEEPING not in argv[1:] and cli_here():
                miss = Miss(os.path.dirname(HERE), payload, started)
        except Exception:  # noqa: BLE001 - the host is a speed-up; the event runs here instead
            hosted = None
        if hosted is not None:
            return hosted
    try:
        code = main(["dispatch.py", *argv], payload=payload, miss=miss)
    except Exception:  # noqa: BLE001 - fail open: a broken dispatcher abstains
        code = 0
    if miss is not None:
        miss.finish()
    if isinstance(code, tuple):
        # Outside the fail-open net: the route's own behavior, exits and
        # tracebacks included (an uncaught error is exit 1, as `python3 <route>` gives).
        run_inline(*code)
        code = 0
    return code


def run(argv: list) -> None:
    """The entry point: `dispatch.sh` imports this module (so its compiled
    bytecode is cached, ~6ms saved on every event) and calls run([lane])."""
    hand_over(argv)
    sys.exit(serve(argv))


if __name__ == "__main__":
    run(sys.argv[1:])
