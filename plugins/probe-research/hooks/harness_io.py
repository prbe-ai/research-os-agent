"""Run one Probe hook for a harness whose hook protocol is not Claude Code's.

    harness_io.py <hook file> [hook args...]

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

So a Kimi manifest runs every hook through this file instead of editing each
hook: the payload is normalized to Claude's shape, the hook runs unchanged, and
its output is translated by the registry row's `hook_output`. A harness whose
row says `claude-json` gets its payload and output through untouched. Claude
Code and Codex never run this file (their hooks.json is frozen).

A skill the person TYPES (`/probe on`) fires no UserPromptSubmit in Kimi: it
arrives only as TurnStarted with `origin_kind: skill_activation` and a leading
`<skill-loaded name=".." trigger="user-slash" args="..">` block that Kimi writes
itself. That turn is handed to the hook as the UserPromptSubmit of the typed
line, so Probe's switch flips as it does when typed in Claude Code; every other
TurnStarted is ignored (its prompt already came through UserPromptSubmit).

It also records which harness process runs this session
(`session_marker.record_harness_process`) on SessionStart and UserPromptSubmit,
for a harness with a `process_title`: that is how a `probe` command the harness
starts finds its session.

FAIL-OPEN: anything unexpected here runs the hook with the raw payload and
passes its output through; a broken adapter must never block the harness.
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

#: Hook events that name a (new) session for this harness process.
_RECORD_EVENTS = ("SessionStart", "UserPromptSubmit")
#: How much of the session index's end to read (as the tap's kimi_discovery).
_INDEX_TAIL_BYTES = 8 * 1024 * 1024
#: The one tool whose output is JSON Probe reads: an answered question.
_JSON_OUTPUT_TOOLS = ("AskUserQuestion",)


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
            _session_marker.record_harness_process(row.id, session_id, titles)


def main(argv: "list[str]") -> int:
    if not argv:
        return 0
    hook = argv[0] if os.path.isabs(argv[0]) else os.path.join(_HERE, argv[0])
    raw_in = sys.stdin.buffer.read()
    stdin = raw_in
    row = None
    quiet = False
    try:
        row = _hook_harness.current()
        payload = json.loads(raw_in.decode("utf-8") or "{}")
        if isinstance(payload, dict):
            typed = typed_skill(payload) if payload.get("hook_event_name") == _TURN_STARTED else None
            if payload.get("hook_event_name") == _TURN_STARTED and typed is None:
                return 0  # before `normalize`, which reads the session index
            normalized = normalize(payload, row)
            if typed is not None:
                # The typed line, as UserPromptSubmit would have carried it. Its
                # output is dropped: the skill itself tells the model what to say.
                normalized["hook_event_name"] = "UserPromptSubmit"
                normalized["prompt"] = typed
                quiet = True
            _record(normalized, row)
            stdin = json.dumps(normalized).encode("utf-8")
    except Exception:  # noqa: BLE001 -- fail-open: run the hook on the raw payload
        stdin = raw_in
    command = [sys.executable, hook] if hook.endswith(".py") else [hook]
    try:
        done = subprocess.run(command + argv[1:], input=stdin, capture_output=True, check=False)
    except OSError:
        return 0
    stdout = "" if quiet else done.stdout.decode("utf-8", "replace")
    stderr = done.stderr.decode("utf-8", "replace")
    code = 0 if quiet else done.returncode
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
