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
payload in Claude's shape, calls `serve` here with the event's lane, and
translates the merged output back. Only it uses ADAPTER_LANES and the UNSHOWN option.

WHO RUNS THE ROUTES (service-only plan, task 13): only the Probe service, on its
warm host or as a fresh `dispatch.sh` (`PROBE_HOOK_VIA` set: `run_by_service`).
A harness that runs this file itself (a legacy hooks.json entry, on a machine or
a window that has not moved to the frozen shim) gets plumbing: `forward` hands
the event to the service (`_hook_forward.py`: `hooks.run {plugin_root, lane,
harness}`, the service started when none answers); with still no service a
record-keeping lane is spooled for the next one and a gate passes. Nothing here
connects to a warm host (D7): the host is the service's.

Contract, as for every hook here: STDLIB ONLY, PYTHON 3.9, POSIX, FAIL-OPEN. Started as
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
installed hooks. Kimi Code's entries run `harness_io.py`, which hands its event
the same way, to the copy Kimi's plugin list records (`kimi_installed_root`).
The Stop wake (claude-reads.json, outside this dispatcher) does
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

Yield (codex-capture-fixes D35): where an agent moved to the frozen hook shim
(`probe-hooks`, which hands every event to the Probe service), this plugin
stays installed for its skills, commands and MCP, and an event of an agent
process born after the switch is the shim's: `run` steps aside
(`yields_to_shim`, `_hook_switches.py`). reads_hook.py's Stop wake and the
tap's hooks do the same.
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
        frozenset(("Bash", "AskUserQuestion", "AskQuestion", "Write", "Edit", "MultiEdit", "NotebookEdit",
                   "WebSearch", "Skill")),
        ("probe-research", "probe_research"),
    ),
    "post-tool": (
        frozenset(("Bash", "Skill", "SlashCommand", "AskUserQuestion", "AskQuestion")),
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


def payload_session(payload: bytes) -> str:
    """The event's own session id: the one `session_id` value in its raw JSON (every lane's
    payload carries it in Claude Code's shape; Kimi's and Cursor's are put in it before they get
    here), read without parsing. "" when there is none or more than one (a tool's output may
    hold a `session_id` of its own): never a guess."""
    values = string_values(payload, _SESSION_KEY) if payload else set()
    return _safe_id(next(iter(values))) if len(values) == 1 else ""


def reads_waiting(payload: bytes = b"") -> bool:
    """The reads route's own shell fast path (routes.json, PostToolUse), in
    python: a Probe daemon message, a service claim its caller never received
    (`claimed/<sid>/<stem>.<token>.json`, the shell's `*.*.json`), or a newer
    status, waits for this session. The session is the payload's (`payload_session`),
    else the harness's session variable; with neither, any session's counts. Kimi
    Code and Cursor export no variable the shell reads, so their every event looked
    at every session's messages before the payload was asked."""
    sid = payload_session(payload)
    if not sid:
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
#: The key of the event's session id (`payload_session`).
_SESSION_KEY = b'"session_id"'
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


#: Kimi Code's plugin folders and its plugin list, side by side
#: (`probe.cli.kimi_config`): `<kimi>/plugins/managed/<id>@<version>` (Kimi's
#: own `/plugins install`: `managed/<id>`) and `<kimi>/plugins/installed.json`.
KIMI_MANAGED = "managed"
KIMI_LIST = "installed.json"
_KIMI_RECORD_ROOT = b'"root"'


def kimi_installed_root(root: str) -> "str | None":
    """`installed_root` for Kimi Code: the folder Kimi's plugin list records for
    this plugin (the copy Kimi loads at its next start), when that is another
    folder of this plugin beside `root` in `managed/`, carries Kimi's manifest
    and the adapter its entries run (`harness_io.py`), and is `_complete`; None
    to run the event here.

    Anchored on `root` itself (`<kimi>/plugins/managed/<folder>`, the list one
    level up), so no variable decides where to look. Probe installs a version
    per folder and keeps the old ones a week (`kimi_config._prune_versions`),
    then leaves a link to the kept one in their place: an event from a pruned
    folder already runs the installed copy (its real path is that copy). It
    follows the record both ways, as the ledger is followed for Claude Code.

    One read of the list per event; parsed only when a byte scan finds another
    folder of this plugin recorded there."""
    here = os.path.realpath(root)
    managed = os.path.dirname(here)
    if os.path.basename(managed) != KIMI_MANAGED:
        return None
    plugin = os.path.basename(here).split("@", 1)[0]
    raw = _read_ledger(os.path.join(os.path.dirname(managed), KIMI_LIST))
    if raw is None or not plugin:
        return None

    def other(path: str) -> bool:
        real = os.path.realpath(path)
        name = os.path.basename(real)
        return real != here and os.path.dirname(real) == managed and name.split("@", 1)[0] == plugin

    try:
        if not any(other(p) for p in string_values(raw, _KIMI_RECORD_ROOT)):
            return None
        import json

        records = json.loads(raw.decode("utf-8"))["plugins"]
        roots = {
            os.path.realpath(r["root"]) for r in records
            if isinstance(r, dict) and r.get("id") == plugin and isinstance(r.get("root"), str)
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        # ValueError also: a path with a NUL byte in it.
        return None
    if len(roots) != 1:
        return None
    real = roots.pop()
    if real == here or os.path.dirname(real) != managed:
        return None
    if not os.path.isfile(os.path.join(real, ".kimi-plugin", "plugin.json")):
        return None
    if not os.path.isfile(os.path.join(real, "hooks", "harness_io.py")) or not _complete(real):
        return None
    return real


def root_variable(env) -> str:
    """Where the harness gave this plugin's folder, and so where a route reads
    it: `PLUGIN_ROOT` under Codex (a route's `$ROOT` reads it first), else
    `CLAUDE_PLUGIN_ROOT`."""
    return "PLUGIN_ROOT" if env.get("PLUGIN_ROOT") else "CLAUDE_PLUGIN_ROOT"


def installed_root(root: str, env) -> "str | None":
    """The plugin folder to hand this event to, or None to run it here.

    Only once per event. Under Codex (`PLUGIN_ROOT` marks it), the newest copy
    in its plugin cache (`codex_installed_root`). A folder in Kimi Code's
    `managed/` (its entries run `harness_io.py`): the copy its plugin list
    records (`kimi_installed_root`). Under Claude Code, `root` sits
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
    if os.path.basename(os.path.dirname(os.path.realpath(root))) == KIMI_MANAGED:
        return kimi_installed_root(root)
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

    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    signal.alarm(max(1, int(round(seconds))))


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


def _kill_routes(children) -> None:
    """End routes and what they started: each one's process group (each route leads its own)."""
    import signal

    for child in children:
        if child is not None and child.poll() is None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
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
            child = subprocess.Popen(  # noqa: S603 - the routes' own commands
                ["/bin/bash", "-c", hook["command"]],
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


def waiting_after_tool(lane: str, options, payload: bytes = b"") -> bool:
    """On post-tool: does a daemon message wait for this event (`payload`, its raw
    JSON: whose session it is) to deliver it? Never when the harness shows no one
    the output (UNSHOWN)."""
    return lane == "post-tool" and UNSHOWN not in options and reads_waiting(payload)


def record_keeping(hook: dict) -> bool:
    """Is this route record-keeping (routes.json `"record_keeping": true`)?
    Nothing waits on its outcome: no decision, no context, no message: a queued
    event runs only these (RECORD_KEEPING, plan D36). tests/test_hook_dispatch.py
    holds which routes say so."""
    return hook.get("record_keeping") is True


def main(argv: list[str], payload: "bytes | None" = None):
    """0/2 (an exit code), or (route, payload, timeout) to run in-process: the Probe
    service's run of the event (`serve`, the host's child).
    `payload`: the event, when the caller read it already (`serve`, the host).
    argv: `dispatch.py <lane> [UNSHOWN] [RECORD_KEEPING | WAITING]`; RECORD_KEEPING (the
    service running a queued event) runs only the record-keeping routes, WAITING
    (pi's turn end, whose record-keeping comes with its own later event) only the others."""
    if len(argv) < 2 or argv[1] not in LANE_EVENT:
        return 0
    lane = argv[1]
    if payload is None:
        payload = sys.stdin.buffer.read()
    kept_only = RECORD_KEEPING in argv[2:]
    waiting_only = not kept_only and WAITING in argv[2:]
    waiting = not kept_only and waiting_after_tool(lane, argv[2:], payload)
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
    if is_foreign_event(parsed, os.environ):
        return 0
    heartbeat(lane, parsed)
    if not isinstance(wiring, dict):
        return 0
    chosen = routes(lane, parsed, wiring)
    if kept_only:
        chosen = [h for h in chosen if record_keeping(h)]
    elif waiting_only:
        chosen = [h for h in chosen if not record_keeping(h)]
    elif lane == "post-tool" and not waiting:
        # The tool lanes' only catch-all is the reads route behind this same
        # fast path (tests/test_hook_dispatch.py holds that): nothing waits,
        # so its shell would only check again and exit.
        chosen = [h for h in chosen if not _is_catch_all(h, lane, wiring)]
        if not chosen:
            return 0
    if not chosen:
        return 0
    limit = budget(lane)
    if lane == "session-end":
        # The harness's clock started before this process did (`serve` set the deadline).
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


# --- THE SERVICE'S RUN, AND A HARNESS'S --------------------------------------------

#: Set by the Probe service on every event it runs (`probe.service.hooks.hooks.VIA_ENV`, the
#: proxy's too): routes run only under it. A harness that runs this dispatcher itself (a legacy
#: hooks.json entry, Kimi's `harness_io.py`) gets plumbing instead: the event goes to the service
#: (`_hook_forward.py`), and with no service it is spooled or let pass; never run here (D4).
VIA_ENV = "PROBE_HOOK_VIA"
#: The lane option the Probe service runs a queued event with: its record-keeping
#: routes only (`main`). The service hands it to the host's child, or to
#: `dispatch.sh`, which passes every argument on.
RECORD_KEEPING = "--record-keeping"
#: The other half: every route but the record-keeping ones (pi's `agent_before_settle`,
#: whose record-keeping runs on its `agent_settled`, `--record-keeping`).
WAITING = "--waiting"
#: What a harness-run dispatcher hands on after the lane (`protocol.HOOK_RUN_OPTIONS`).
FORWARDED_OPTIONS = (UNSHOWN,)


def run_by_service(env=None) -> bool:
    """Is this the Probe service's run of the event (its fresh dispatcher, its host's child)? Its
    mark (VIA_ENV); or, from a service before the mark was on every run, what it sets on a fresh
    dispatcher it starts for this very folder (`PROBE_HOOK_HOST=off` with the hand-over naming it),
    until `ensure --min-version` replaces such a service (plan D15)."""
    env = os.environ if env is None else env
    if env.get(VIA_ENV):
        return True
    named = env.get(HANDED_OVER)
    if env.get("PROBE_HOOK_HOST") != "off" or not named:
        return False
    return os.path.realpath(named) == os.path.realpath(os.path.dirname(HERE))


def event_end(lane: str) -> float:
    """When (monotonic) a forwarded event's client stops waiting: the harness's own limit for
    the event (`harness_limit`), counted from this process's start (PROCESS_STARTED, less
    STARTUP_ALLOWANCE_S for bash and the interpreter before it)."""
    return PROCESS_STARTED + harness_limit(lane) - STARTUP_ALLOWANCE_S


def run_hosted(argv: list, payload: bytes) -> None:
    """The event inside the service's warm host's child (`zygote.py`): `main`, with the payload the
    service handed it."""
    try:
        code = main(["dispatch.py", *argv], payload=payload)
    except Exception:  # noqa: BLE001 - fail open: a broken dispatcher abstains
        code = 0
    if isinstance(code, tuple):
        run_inline(*code)
        code = 0
    sys.exit(code)


def agent_id(env) -> "str | None":
    """The harness whose hook this is: the PROBE_AGENT its entry exports (Kimi's), else the row
    whose plugin folder variable it set (`root_variable`)."""
    named = (env.get("PROBE_AGENT") or "").strip()
    if named and named.isascii() and named.replace("_", "").isalnum():
        return named
    try:
        row = _agent_row(env)
    except (OSError, ValueError):
        return None
    return row.get("id") if row else None


def forward(argv: list, payload: "bytes | None" = None) -> int:
    """A harness's run of this dispatcher: hand the event to the Probe service (`_hook_forward`)."""
    lane = argv[0]
    if payload is None:
        payload = sys.stdin.buffer.read()
    if lane in TOOL_LANES and not waiting_after_tool(lane, argv[1:], payload) and not could_route(lane, payload):
        return 0
    try:
        client = _load_by_path("_hook_forward")
        options = [option for option in argv[1:] if option in FORWARDED_OPTIONS]
        return client.forward(os.path.dirname(HERE), lane, options, payload, deadline=event_end(lane),
                              harness=agent_id(os.environ), root_env=root_variable(os.environ))
    except Exception:  # noqa: BLE001 - fail open: nothing to run, nothing waits
        return 0


def serve(argv: list, payload: "bytes | None" = None) -> int:
    """One event: its exit status. `argv`: `[lane, options...]`; `payload`: the event, when the
    caller read it already (`harness_io.py --event`), else stdin. The service's run runs the lane's
    routes here; a harness's run hands the event to the service (`forward`).

    What `run` does after the hand-over, returning instead of exiting, so a
    harness adapter can run an event the way `dispatch.sh` does and still read
    what it printed. An in-process route's own exit (SystemExit) and uncaught
    error leave it as they leave `python3 <route>`."""
    lane = argv[0] if argv else ""
    if lane not in LANE_EVENT:
        return 0
    if not run_by_service():
        return forward(argv, payload)
    if lane == "session-end":
        # The harness's SessionEnd budget runs from about now (`main` caps the lane's budget by it).
        os.environ[DEADLINE_ENV] = repr(time.time() + budget(lane))
    try:
        code = main(["dispatch.py", *argv], payload=payload)
    except Exception:  # noqa: BLE001 - fail open: a broken dispatcher abstains
        code = 0
    if isinstance(code, tuple):
        # Outside the fail-open net: the route's own behavior, exits and
        # tracebacks included (an uncaught error is exit 1, as `python3 <route>` gives).
        run_inline(*code)
        code = 0
    return code


def _load_by_path(name: str):
    """A module beside this file, by explicit path (never a bare import: the
    user's project may carry a same-named one)."""
    import importlib.util  # noqa: PLC0415

    key = f"_probe_hooks.{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def is_foreign_event(payload: dict, env) -> bool:
    """Is this another agent's event under this plugin's name? Cursor runs Claude Code's
    plugins too, with its own payloads (the registry's `hook_payload_marker`): those are
    Probe's Cursor routes', which the service runs with PROBE_AGENT naming Cursor."""
    import json

    try:
        with open(os.path.join(HERE, "harnesses.json"), encoding="utf-8") as handle:
            rows = json.load(handle).get("harnesses") or []
    except (OSError, ValueError):
        return False
    named = env.get("PROBE_AGENT") or ""
    return any(isinstance(row, dict) and row.get("hook_payload_marker") and row["hook_payload_marker"] in payload
               and row.get("id") != named for row in rows)


def _agent_row(env) -> "dict | None":
    """The harness registry row of the agent running this event: the one whose
    plugin folder variable it set (`root_variable`)."""
    import json

    with open(os.path.join(HERE, "harnesses.json"), encoding="utf-8") as handle:
        rows = json.load(handle).get("harnesses") or []
    wanted = root_variable(env)
    return next((row for row in rows if (row.get("plugin") or {}).get("root_env") == wanted), None)


def yields_to_shim() -> bool:
    """Does this event belong to the frozen hook shim instead (codex-capture-fixes
    D35)? A machine whose agent moved to it keeps this plugin installed (its
    skills, commands and MCP), so both fire on every event: an agent process
    born after the switch is the shim's, and this plugin steps aside. Never in
    the service's or the runner's own run of these files (`PROBE_HOOK_VIA`).
    One listdir when no machine here ever switched; any doubt: run."""
    try:
        if os.environ.get("PROBE_HOOK_VIA"):
            return False
        # Free when nothing switched (the hot path: every tool call): one listdir, no import.
        # `_hook_switches.get_service_root`'s rule (tests hold them equal).
        root = os.path.join(os.path.expanduser("~"), ".local", "state", "probe", "service")
        try:
            names = os.listdir(root)
        except OSError:
            return False
        if not any(os.path.isfile(os.path.join(root, name, "hooks", "migrations.json")) for name in names):
            return False
        switches = _load_by_path("_hook_switches")
        sp = _load_by_path("_service_paths")  # the machine key: only once some machine here switched
        row = _agent_row(os.environ)
        if row is None:
            return False
        return switches.should_yield(sp.service_dir(), row["id"], row.get("home"),
                                     (row.get("binary"), row.get("process_title")))
    except Exception:  # noqa: BLE001 - fail open: run the event here
        return False


def run(argv: list) -> None:
    """The entry point: `dispatch.sh` imports this module (so its compiled
    bytecode is cached, ~6ms saved on every event) and calls run([lane]). An
    event the frozen hook shim answers (`yields_to_shim`) ends here, unread."""
    if not os.environ.get(VIA_ENV) and run_by_service():
        # An older service's run (`run_by_service`): marked as every service run is, before
        # `hand_over` takes the hand-over that told it, so its routes read the mark too.
        os.environ[VIA_ENV] = "service"
    if yields_to_shim():
        sys.exit(0)
    hand_over(argv)
    sys.exit(serve(argv))


if __name__ == "__main__":
    run(sys.argv[1:])
