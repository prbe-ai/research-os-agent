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

Kimi Code renders its own per-script manifest from the same routes.json
(tools/kimi_hooks.py) and never runs this.

Contract, as for every hook here: STDLIB ONLY, PYTHON 3.9, FAIL-OPEN. Started as
`python3 -S` (no site-packages: a dispatcher needs none, and it saves ~10ms on
every event). Never exits non-zero unless a route did (an exit of 2 is a veto on
PreToolUse/UserPromptSubmit, and only a route may cast one).

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

#: Lane -> the hook event it serves; the argument hooks.json passes.
LANE_EVENT = {
    "session-start": "SessionStart",
    "prompt": "UserPromptSubmit",
    "pre-compact": "PreCompact",
    "pre-tool": "PreToolUse",
    "post-tool": "PostToolUse",
    "session-end": "SessionEnd",
    "stop": "Stop",
}

#: Each lane's hooks.json entry `timeout`, in seconds (None: the harness's
#: default). Frozen with the entries: a lane's routes must each fit in
#: ENTRY_TIMEOUT[lane] - MARGIN_S (tests/test_hook_dispatch.py holds both).
#: `prompt` keeps 4s of headroom on purpose, since it can never grow.
ENTRY_TIMEOUT = {
    "session-start": None,
    "prompt": 10,
    "pre-compact": 11,
    "pre-tool": 6,
    "post-tool": 6,
    "session-end": 3,
    "stop": 16,
}

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
#: Never budget a SessionEnd below this, whatever the variable says.
MIN_SESSION_END_S = 0.1

#: The lanes every tool call reaches. Before parsing the payload, the
#: dispatcher reads every `"tool_name": "..."` in its raw bytes (JSON escaping
#: keeps file contents from faking one; a nested key only adds a name) and goes
#: on only if TOOL_FILTER takes one of them or, after a tool call, a Probe
#: daemon message waits: most tool calls cost a python start and nothing else.
TOOL_LANES = ("pre-tool", "post-tool")

#: Per tool lane: the exact tool names, and the substrings (the Probe MCP
#: prefix `probe[-_]research`), that some route's matcher accepts.
#: tests/test_hook_dispatch.py holds this equal to what routes.json's matchers
#: accept in both plugins: a new tool route needs its name here too.
TOOL_FILTER = {
    "pre-tool": (
        frozenset(("Bash", "AskUserQuestion", "Write", "Edit", "MultiEdit", "NotebookEdit")),
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
    python: a Probe daemon message, or a newer status, waits for this session."""
    sid = _safe_id(os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or "")  # harness-literal-ok: the reads route's own gate
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    root = os.path.join(base, "probe", "reads")
    messages = os.path.join(root, "messages")
    for folder in [sid] if sid else _listdir(messages):
        if any(n.endswith(".json") and not n.startswith(".") for n in _listdir(os.path.join(messages, folder))):
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


def named_tools(payload: bytes) -> set:
    """Every `tool_name` string value in the raw payload (a superset of the
    real one), read without parsing the payload or importing anything."""
    names = set()
    at = payload.find(_KEY)
    while at >= 0:
        i = at + len(_KEY)
        while i < len(payload) and payload[i] in _SPACE:
            i += 1
        if payload[i : i + 1] == b":":
            i += 1
            while i < len(payload) and payload[i] in _SPACE:
                i += 1
            if payload[i : i + 1] == b'"':
                end, escaped = i + 1, False
                while end < len(payload) and payload[end] != 0x22:
                    if payload[end] == 0x5C:
                        escaped = True
                        end += 1
                    end += 1
                raw = payload[i + 1 : end]
                if escaped:
                    import json

                    try:
                        names.add(json.loads(b'"' + raw + b'"'))
                    except ValueError:
                        names.add(raw.decode("utf-8", "replace"))
                else:
                    names.add(raw.decode("utf-8", "replace"))
        at = payload.find(_KEY, i)
    return names


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


def budget(lane: str) -> float:
    """Seconds this dispatcher may spend before printing what it has."""
    if lane == "session-end":
        if os.environ.get("PLUGIN_ROOT"):
            return CODEX_SESSION_END_S - SESSION_END_MARGIN_S
        raw = (os.environ.get("CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS") or "").strip()
        try:
            ms = int(raw) if raw.isascii() and raw.isdigit() else 0
        except ValueError:
            ms = 0
        ms = ms or CLAUDE_SESSION_END_DEFAULT_MS
        return max(ms / 1000.0 - SESSION_END_MARGIN_S, MIN_SESSION_END_S)
    timeout = ENTRY_TIMEOUT.get(lane)
    return (HARNESS_DEFAULT_TIMEOUT_S if timeout is None else timeout) - MARGIN_S


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
    if seconds:
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
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass

    stopping = []
    starting = [True]

    def stop_now() -> None:
        for child in children:
            if child is not None and child.poll() is None:
                kill(child)
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
    for child in children:
        if child is not None and child.poll() is None:
            kill(child)
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


def main(argv: list[str]):
    """0/2 (an exit code), or (route, payload, timeout) to run in-process."""
    if len(argv) < 2 or argv[1] not in LANE_EVENT:
        return 0
    lane = argv[1]
    payload = sys.stdin.buffer.read()
    waiting = lane == "post-tool" and reads_waiting()
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
    heartbeat(lane, parsed)
    if not isinstance(wiring, dict):
        return 0
    chosen = routes(lane, parsed, wiring)
    if lane == "post-tool" and not waiting:
        # The tool lanes' only catch-all is the reads route behind this same
        # fast path (tests/test_hook_dispatch.py holds that): nothing waits,
        # so its shell would only check again and exit.
        chosen = [h for h in chosen if not _is_catch_all(h, lane, wiring)]
        if not chosen:
            return 0
    if not chosen:
        return 0
    limit = budget(lane)
    if len(chosen) == 1:
        parts = plain_route(chosen[0]["command"])
        if parts is not None and os.path.isfile(os.path.join(HERE, parts[1])):
            own = _own_timeout(chosen[0])
            return (parts, payload, limit if own is None else min(own, limit))
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


def run(argv: list) -> None:
    """The entry point: `dispatch.sh` imports this module (so its compiled
    bytecode is cached, ~6ms saved on every event) and calls run([lane])."""
    try:
        code = main(["dispatch.py", *argv])
    except Exception:  # noqa: BLE001 - fail open: a broken dispatcher abstains
        code = 0
    if isinstance(code, tuple):
        # Outside the fail-open net: the route's own behavior, exits and
        # tracebacks included (an uncaught error is exit 1, as `python3 <route>` gives).
        run_inline(*code)
        code = 0
    sys.exit(code)


if __name__ == "__main__":
    run(sys.argv[1:])
