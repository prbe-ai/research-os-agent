"""Run Probe's hooks for a harness whose hook protocol is not Claude Code's.

    harness_io.py --event <event> [--unshown]   one event, every route of it
    harness_io.py <hook file> [hook args...]    one hook (the old entries)

The hooks in this folder speak Claude Code's protocol (Codex copied it): a
payload with `transcript_path`, a string `prompt`, a `tool_response` object, a
`permission_mode`; and output `{"hookSpecificOutput": {"additionalContext": ...}}`.
Kimi Code's differs (agent/docs/adding-a-harness.md, Kimi facts):

  in   `session_id` is `session_<uuid>`; `prompt` is a list of content parts;
       `tool_output` is a string (an answered AskUserQuestion is JSON text);
       no `transcript_path` and no `permission_mode`.
  out  `{"message": "..."}` or plain text reaches the model; any other JSON is
       injected as raw text. A PreToolUse deny in Claude's shape is understood.

The session's permission mode is deliberately NOT derived for Kimi: its
transcript gets no new `permission.set_mode` on resume, so an old `auto` would
read as bypass in a session that now asks. Without a mode the hooks ask.

So a Kimi manifest runs its hooks through this file instead of editing each
hook: the payload is normalized to Claude's shape, the hooks run unchanged, and
their output is translated by the registry row's `hook_output`. A harness whose
row says `claude-json` gets its payload and output through untouched. Claude
Code and Codex never run this file (their hooks.json is frozen).

`--event` (one manifest entry per event, tools/kimi_hooks.py): first the event
moves to the installed copy's adapter when that is another folder (`hand_over`,
as `dispatch.sh` moves Claude Code's and Codex's), so a Kimi session left open
across an update runs the installed hooks; then the payload is
normalized ONCE, and the event's lane runs through the dispatcher
(`dispatch.serve`, the same path `dispatch.sh` takes for Claude Code and Codex:
the Probe service's warm host, else here), with this process's stdout caught so
the dispatcher's merged output is translated once. Other harnesses' session-id
variables, inherited when Kimi runs inside one, are dropped first
(`foreign_session_envs`): route gates read them before the payload. `--unshown` goes to the
dispatcher as is (`dispatch.UNSHOWN`: the harness shows no one this event's
output). `<hook file>` is how every manifest through plugin 0.163.0 ran each
hook, one entry per script; a Kimi session left open keeps those entries, and
the folder they run from (each version is installed into a folder of its own
and never changed; a version pruned after a week becomes a link to the kept
one), so they keep working unchanged.

A skill the person TYPES (`/probe on`) fires no UserPromptSubmit in Kimi: it
arrives only as TurnStarted with `origin_kind: skill_activation` and a leading
`<skill-loaded name=".." trigger="user-slash" args="..">` block that Kimi writes
itself. That turn is handed to the hook (under `--event`, the dispatcher's
`turn-started` lane: the switch alone) as the UserPromptSubmit of the typed
line, so Probe's switch flips as it does when typed in Claude Code; every other
TurnStarted is ignored (its prompt already came through UserPromptSubmit).

It also records which harness process runs this session
(`session_marker.record_harness_process`) on SessionStart and UserPromptSubmit,
for a harness with a `process_title`: that is how a `probe` command the harness
starts finds its session.

FAIL-OPEN: anything unexpected here runs the hooks with the raw payload and
passes their output through, and a missing dispatcher runs nothing; a broken
adapter must never block the harness.
"""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_hook_harness():
    """`_hook_harness.py` beside this file, by EXPLICIT path (a bare import could
    pick up a same-named module from the user's project on sys.path)."""
    import importlib.util  # noqa: PLC0415

    key = "_probe_hooks._hook_harness"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, os.path.join(_HERE, "_hook_harness.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


_hook_harness = _load_hook_harness()
_session_marker = _hook_harness.load_sibling("_session_marker")
#: The folder this event's entry ran from: the session's own, which Kimi loaded
#: (a hand-over passes it on in HANDED_OVER). Set by `main`, read by `_record`.
_LOADED_ROOT = ""

#: Hook events that name a (new) session for this harness process.
_RECORD_EVENTS = ("SessionStart", "UserPromptSubmit")
#: How much of the session index's end to read (as the tap's kimi_discovery).
_INDEX_TAIL_BYTES = 8 * 1024 * 1024
#: The one tool whose output is JSON Probe reads: an answered question.
_JSON_OUTPUT_TOOLS = ("AskUserQuestion",)
#: `harness_io.py --event <event>`: the event's whole lane, through the dispatcher.
EVENT_FLAG = "--event"
#: Passed on to the dispatcher as is (`dispatch.UNSHOWN`, which a test keeps equal).
UNSHOWN_FLAG = "--unshown"
#: Tells a hook its output reaches no model (`tracking_guard.QUIET_ENV`).
_QUIET_ENV = "PROBE_HOOK_QUIET"


#: The event a typed skill arrives as (see the module docstring), and Kimi's
#: own marks of one the person typed.
_TURN_STARTED = "TurnStarted"
_SKILL_ACTIVATION = "skill_activation"
_USER_SLASH = "user-slash"
_SKILL_LOADED_TAG = re.compile(r"<skill-loaded\b([^>]*)>")
#: The switch's skills (`tracking_guard.TOGGLE_SKILL_SLUGS`, which a test keeps
#: equal: that module runs as a script and is not importable from here).
_SWITCH_SLUGS = frozenset({"probe", "track-work", "toggle-research-tracking", "research-tracking"})
_ATTRIBUTE = re.compile(r'([A-Za-z_-]+)="([^"]*)"')


def typed_skill(payload: dict) -> "str | None":
    """The line a person typed (`/probe on`) when this is the turn a typed skill
    starts, else None. Only Kimi's own leading block counts, and only with
    `trigger="user-slash"`: a skill the model loads is not a person's switch."""
    if payload.get("hook_event_name") != _TURN_STARTED or payload.get("origin_kind") != _SKILL_ACTIVATION:
        return None
    text = _prompt_text(payload.get("prompt"))
    if not isinstance(text, str):
        return None
    tag = _SKILL_LOADED_TAG.search(text)
    # Kimi's own block opens a line near the top (after one sentence of
    # preamble); a tag quoted mid-line or further down is content, not the call.
    if tag is None or text.find("<skill-loaded") != tag.start():
        return None
    if (tag.start() and text[tag.start() - 1] != "\n") or text.count("\n", 0, tag.start()) > 3:
        return None
    attributes = {key: html.unescape(value) for key, value in _ATTRIBUTE.findall(tag.group(1))}
    name = attributes.get("name", "").strip()
    if attributes.get("trigger") != _USER_SLASH or not name or any(c.isspace() for c in name):
        return None
    # Only Probe's switch: any other typed skill is none of these hooks' business
    # (and running them would claim a notice nobody sees).
    if name.split(":")[-1] not in _SWITCH_SLUGS:
        return None
    args = " ".join(attributes.get("args", "").split())
    return f"/{name} {args}".rstrip()


def _strip_prefix(session_id: object, prefix: "str | None") -> object:
    if isinstance(session_id, str) and prefix and session_id.startswith(prefix):
        return session_id[len(prefix):]
    return session_id


def _prompt_text(prompt: object) -> object:
    """A list of content parts as the text a person typed."""
    if not isinstance(prompt, list):
        return prompt
    texts = [part.get("text") for part in prompt if isinstance(part, dict) and part.get("type") == "text"]
    return "\n".join(text for text in texts if isinstance(text, str))


def _transcript(raw_session_id: str, row) -> "str | None":
    """The main transcript of a session, from the harness's own session index
    (newest line wins), else None."""
    try:
        home = row.home_dir()
        index = os.path.join(str(home), "session_index.jsonl") if home else None
        if not index or not os.path.isfile(index):
            return None
        with open(index, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _INDEX_TAIL_BYTES))
            lines = fh.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("sessionId") == raw_session_id:
                session_dir = entry.get("sessionDir")
                if isinstance(session_dir, str) and os.path.basename(session_dir.rstrip("/")) == raw_session_id:
                    path = os.path.join(session_dir, "agents", "main", "wire.jsonl")
                    return path if os.path.isfile(path) else None
        return None
    except Exception:  # noqa: BLE001
        return None


def normalize(payload: dict, row) -> dict:
    """The payload in Claude Code's shape (a new dict)."""
    raw = row._raw  # noqa: SLF001 -- the plain JSON row
    out = dict(payload)
    raw_session = payload.get("session_id")
    out["session_id"] = _strip_prefix(raw_session, raw.get("session_id_prefix"))
    if "prompt" in out:
        out["prompt"] = _prompt_text(out["prompt"])
    if out.get("tool_call_id") and not out.get("tool_use_id"):
        # The approvals hook pairs a question with its answer by this id.
        out["tool_use_id"] = out["tool_call_id"]
    if "tool_output" in out and "tool_response" not in out:
        output = out["tool_output"]
        parsed = output
        if out.get("tool_name") in _JSON_OUTPUT_TOOLS and isinstance(output, str):
            try:
                parsed = json.loads(output)
            except ValueError:
                parsed = output
        out["tool_response"] = parsed
    if not out.get("transcript_path") and isinstance(raw_session, str):
        transcript = _transcript(raw_session, row)
        if transcript:
            out["transcript_path"] = transcript
    return out


def translate(stdout: str, row) -> "tuple[str, str, int | None]":
    """The hook's stdout in the harness's own output protocol:
    (stdout, extra stderr, forced exit code or None)."""
    if row._raw.get("hook_output") != "message-json":  # noqa: SLF001
        return stdout, "", None
    text = stdout.strip()
    if not text:
        return "", "", None
    try:
        obj = json.loads(text)
    except ValueError:
        return stdout, "", None  # plain text is what Kimi injects as context
    if not isinstance(obj, dict):
        return "", "", None
    hso = obj.get("hookSpecificOutput")
    hso = hso if isinstance(hso, dict) else {}
    if hso.get("permissionDecision") == "deny":
        reason = hso.get("permissionDecisionReason") or ""
        deny = {"permissionDecision": "deny", "permissionDecisionReason": reason}
        return json.dumps({"hookSpecificOutput": deny}) + "\n", "", None
    if obj.get("decision") == "block":
        reason = obj.get("reason") or "Blocked by Probe."
        return "", str(reason) + "\n", 2
    context = hso.get("additionalContext")
    if isinstance(context, str) and context.strip():
        return json.dumps({"message": context}) + "\n", "", None
    # `continue`, `systemMessage` (shown to a person, never the model) and the
    # like have no Kimi equivalent: say nothing rather than inject raw JSON.
    return "", "", None


def _record(payload: dict, row) -> None:
    title = row._raw.get("process_title")  # noqa: SLF001
    if title and payload.get("hook_event_name") in _RECORD_EVENTS:
        session_id = payload.get("session_id")
        if isinstance(session_id, str):
            # The retitled name (Linux /proc) or the binary it was started as
            # (macOS `ps` shows `kimi`, never the new title).
            binary = row._raw.get("binary")  # noqa: SLF001
            titles = (title, binary) if isinstance(binary, str) and binary else (title,)
            loaded = None
            if _LOADED_ROOT:
                # What this Kimi process loaded (kimi/pi update parity K4): the
                # update's "no restart needed" is proven from it, never from disk.
                try:
                    loaded = _hook_harness.load_sibling("_kimi_live").loaded_info(
                        _LOADED_ROOT, str(payload.get("hook_event_name") or ""), str(payload.get("source") or ""))
                except Exception:  # noqa: BLE001 -- no record: the restart line stays
                    loaded = None
            _session_marker.record_harness_process(row.id, session_id, titles, loaded=loaded)


def _adapt(raw_in: bytes):
    """(row or None, the payload the hooks read, quiet), or None when the event
    runs no hook at all (a TurnStarted that is not a typed switch). FAIL-OPEN:
    anything unexpected leaves the raw payload."""
    stdin = raw_in
    row = None
    quiet = False
    try:
        row = _hook_harness.current()
        payload = json.loads(raw_in.decode("utf-8") or "{}")
        if isinstance(payload, dict):
            typed = typed_skill(payload) if payload.get("hook_event_name") == _TURN_STARTED else None
            if payload.get("hook_event_name") == _TURN_STARTED and typed is None:
                return None  # before `normalize`, which reads the session index
            normalized = normalize(payload, row)
            if typed is not None:
                # The typed line, as UserPromptSubmit would have carried it. Its
                # output is dropped: the skill itself tells the model what to say.
                normalized["hook_event_name"] = "UserPromptSubmit"
                normalized["prompt"] = typed
                quiet = True
            _record(normalized, row)
            stdin = json.dumps(normalized).encode("utf-8")
    except Exception:  # noqa: BLE001 -- fail-open: run the hooks on the raw payload
        stdin = raw_in
    return row, stdin, quiet


def _reply(stdout: str, stderr: str, code: int, row, quiet: bool) -> int:
    """The hooks' output, written in the harness's own protocol; the exit status."""
    if quiet:
        stdout, code = "", 0
    try:
        if row is not None:
            stdout, extra, forced = translate(stdout, row)
            stderr += extra
            if forced is not None:
                code = forced
    except Exception:  # noqa: BLE001
        pass
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    return code


def _load_dispatch():
    """`dispatch.py` beside this file, by EXPLICIT path (as `_load_hook_harness`)."""
    import importlib.util  # noqa: PLC0415

    key = "_probe_hooks.dispatch"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, os.path.join(_HERE, "dispatch.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(key, None)
            raise
    return sys.modules[key]


def _exit_status(code) -> int:
    """A SystemExit's code as the process would exit with it."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    try:
        sys.stderr.write(f"{code}\n")
    except Exception:  # noqa: BLE001
        pass
    return 1


def captured(serve, argv: list, payload: bytes) -> "tuple[str, int]":
    """`serve(argv, payload)` with this process's stdout sent to a scratch file
    instead of the harness: (what it printed, its exit status).

    File descriptor 1 itself, not only sys.stdout: the warm host and the routes'
    children write to the descriptor they are handed. stderr goes to the harness
    as it is. An in-process route's own exit and uncaught error end the event as
    they end `python3 <route>`: its status; a traceback and 1."""
    import tempfile  # noqa: PLC0415

    code = 0
    with tempfile.TemporaryFile() as sink:
        sys.stdout.flush()
        saved = os.dup(1)
        try:
            os.dup2(sink.fileno(), 1)
            try:
                code = serve(argv, payload)
            except SystemExit as stop:
                code = _exit_status(stop.code)
            except BaseException:  # noqa: BLE001 -- a route's own error, as it ends `python3 <route>`
                import traceback  # noqa: PLC0415

                traceback.print_exc()
                code = 1
            finally:
                for stream in (sys.stdout, sys.__stdout__):
                    try:
                        stream.flush()
                    except Exception:  # noqa: BLE001
                        pass
        finally:
            os.dup2(saved, 1)
            os.close(saved)
        sink.seek(0)
        out = sink.read()
    return out.decode("utf-8", "replace"), code


def foreign_session_envs(row) -> list:
    """Every other harness's session-id variable (its registry `session_env`).

    A harness started inside another one inherits the parent's (Kimi run from
    Claude Code's or Codex's shell), and the dispatcher's routes read those
    BEFORE the payload: the reads route's shell gate (and `dispatch.reads_waiting`)
    would look in the PARENT's inbox, skip the hook while Kimi's own answer
    waits, and start a new turn for the parent. This event's session is the
    payload's, so `--event` runs its routes without them."""
    rows = _hook_harness._document()["harnesses"]  # noqa: SLF001 -- the plain JSON rows
    return [raw["session_env"] for raw in rows if raw.get("id") != row.id and raw.get("session_env")]


def run_event(argv: "list[str]") -> int:
    """`--event <event> [--unshown]`: every route of the event's lane, through
    the dispatcher (`dispatch.serve`: warm host, else here), on the payload put
    in Claude's shape once; its merged output translated once."""
    event = argv[0] if argv else ""
    options = [a for a in argv[1:] if a == UNSHOWN_FLAG]
    raw_in = sys.stdin.buffer.read()
    adapted = _adapt(raw_in)
    if adapted is None:
        return 0
    row, payload, quiet = adapted
    if event == _TURN_STARTED and not quiet:
        return 0  # a turn it could not read: no typed switch to hand on
    try:
        dispatch = _load_dispatch()
        lane = next((lane for lane, served in dispatch.LANE_EVENT.items() if served == event), None)
        if lane is None:
            return 0
        # The routes read the plugin's folder where Claude Code puts it (Kimi's
        # entry unset every other harness's root): never Codex's, which would
        # mark them Codex's. PROBE_AGENT, exported by the entry, names Kimi.
        os.environ.pop("PLUGIN_ROOT", None)
        os.environ[dispatch.root_variable(os.environ)] = os.path.dirname(_HERE)
        for name in foreign_session_envs(row) if row is not None else ():
            os.environ.pop(name, None)
        if quiet:
            os.environ[_QUIET_ENV] = "1"
    except Exception:  # noqa: BLE001 -- no dispatcher (a half-installed copy): abstain
        return 0
    try:
        stdout, code = captured(dispatch.serve, [lane, *options], payload)
    except OSError:
        return 0  # no stdout to catch (closed, no scratch file): abstain
    return _reply(stdout, "", code, row, quiet)


#: Set while this adapter hands an event to the installed copy (`dispatch.HANDED_OVER`,
#: which a test keeps equal): that copy takes it out of the environment, so an
#: event moves at most once and nothing a route starts inherits it.
HANDED_OVER = "PROBE_HOOKS_HANDED_OVER"
#: The private names this folder's modules load under (`_load_dispatch`,
#: `_hook_harness.load_sibling`): another copy's must load its own.
_PRIVATE_PREFIX = "_probe_hooks."


def hand_over(argv: "list[str]") -> None:
    """`--event`: run this event in the installed copy's adapter instead, in this
    process, when that is another folder (`dispatch.kimi_installed_root`: the
    one Kimi's plugin list records; a Kimi session keeps the folder it started
    with, and with it this file, while updates install new folders beside it).
    Its `harness_io.py` is loaded in place of this one, with sys.path[0] (where
    in-process routes import from), this folder's private modules and
    KIMI_PLUGIN_ROOT pointing at it; stdin is still unread. One that will not
    load: the event runs here. Once its `main` starts, the event is its own.

    Only `--event`: the per-script entries (plugin 0.163.0 and before) run the
    harness_io.py of a folder from before this hand-over existed."""
    if os.environ.pop(HANDED_OVER, None) is not None:
        return
    here = os.path.dirname(_HERE)
    try:
        target = _load_dispatch().kimi_installed_root(here)
        if target is None:
            return
        import importlib.util  # noqa: PLC0415

        hooks = os.path.join(target, "hooks")
        mine = {name: sys.modules.pop(name) for name in list(sys.modules) if name.startswith(_PRIVATE_PREFIX)}
        first = sys.path[0] if sys.path else None
        if sys.path:
            sys.path[0] = hooks
        try:
            spec = importlib.util.spec_from_file_location(_PRIVATE_PREFIX + "harness_io", os.path.join(hooks, "harness_io.py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            entry = module.main
        except Exception:  # noqa: BLE001 -- a copy that will not load: run here
            for name in [n for n in sys.modules if n.startswith(_PRIVATE_PREFIX)]:
                sys.modules.pop(name, None)
            sys.modules.update(mine)
            if first is not None:
                sys.path[0] = first
            return
        os.environ["KIMI_PLUGIN_ROOT"] = target
        os.environ[HANDED_OVER] = here
    except Exception:  # noqa: BLE001 -- fail open: run the event here
        return
    sys.exit(entry(argv))


def main(argv: "list[str]") -> int:
    global _LOADED_ROOT
    if not argv:
        return 0
    # Before `hand_over` takes it: the folder the session's entry ran from.
    _LOADED_ROOT = os.environ.get(HANDED_OVER) or os.environ.get("KIMI_PLUGIN_ROOT") or ""
    if argv[0] == EVENT_FLAG:
        hand_over(argv)
        return run_event(argv[1:])
    hook = argv[0] if os.path.isabs(argv[0]) else os.path.join(_HERE, argv[0])
    raw_in = sys.stdin.buffer.read()
    adapted = _adapt(raw_in)
    if adapted is None:
        return 0
    row, stdin, quiet = adapted
    command = [sys.executable, hook] if hook.endswith(".py") else [hook]
    # A hook whose output is dropped is told so: what it says reaches no model,
    # so it must not record that it told one (`tracking_guard.QUIET_ENV`).
    env = {**os.environ, _QUIET_ENV: "1"} if quiet else None
    try:
        done = subprocess.run(command + argv[1:], input=stdin, capture_output=True, check=False, env=env)
    except OSError:
        return 0
    return _reply(done.stdout.decode("utf-8", "replace"), done.stderr.decode("utf-8", "replace"),
                  done.returncode, row, quiet)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
