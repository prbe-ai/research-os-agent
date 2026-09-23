"""Read the harness's own transcript into the events the Probe daemon decides on.

Capture's sanitizers (`sanitize.py`, `codex_sanitize.py`, `pi_sanitize.py`) drop
tool RESULTS, and the daemon cannot work without them: a run id first appears in
the output of `probe run start`, a metric in a script's stdout. So this module
parses the RAW line of each harness into one small event shape, and everything
it hands onward passes `secrets.redact` first -- the gateway call is a second
way off the machine, and it must not be the one the scanner does not see.

    Claude Code   {"type": "user"|"assistant", "message": {"content": ...}}
    Codex         {"type": "response_item", "payload": {"type": "message" |
                   "function_call" | "function_call_output" | "custom_tool_call" | ...}}
    pi            {"type": "message", "message": {"role": "user"|"assistant"|"toolResult"}}

Unknown shapes are skipped, never guessed at: an event the daemon misreads is
worse than one it did not see, because it would ground a write on it.
"""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tap import secrets

USER = "user"
ASSISTANT = "assistant"
TOOL_CALL = "tool_call"
TOOL_RESULT = "tool_result"

#: The daemon sees events WHOLE: a command is never cut, and a message or a
#: result only when one event alone is larger than EVENT_CEILING, which keeps
#: its head and tail and says how much is between them (the full text stays in
#: the transcript). What bounds a cycle is the WORKER's render budget, which
#: ends the cycle early rather than cutting what is in it.
EVENT_CEILING = 120_000
#: The longest single transcript line the worker will parse.
MAX_LINE_BYTES = 4 * 1024 * 1024
#: Kept for callers that still ask for the old per-event view (context only).
TEXT_CAP = 4000
COMMAND_CAP = 2000
RESULT_CAP = 3000

UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
#: `"run_id": "<uuid>"`, `/runs/<uuid>`, `run <uuid>` -- the entity a nearby id names.
_TYPED_ID_RE = re.compile(
    r"(?P<kind>run|project|experiment|artifact|paper)s?(?:_id)?[\"'/:=\s]{1,4}"
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)
#: `cd DIR` at the start of a shell command or after `&&`, `;`, `||`, `(` or a
#: newline: the folder the rest of the command ran in.
_CD_RE = re.compile(r"(?:^|&&|\|\||;|\(|\n)\s*cd\s+(\"[^\"]+\"|'[^']+'|[^\s;&|)]+)")
#: shlex operator tokens (the newline included: each line is its own command),
#: those that END a command, and a heredoc opener -- the plugin guard's
#: tokenizer (`hooks/tracking_guard.py`), vendored: the tap cannot import it.
_OPERATOR_CHARS = "();<>|&\n"
_SEPARATOR_CHARS = frozenset(";|&()\n")
_HEREDOC = re.compile(r"(?<!<)<<(-?)[ \t]*(['\"]?)\\?([A-Za-z0-9_][A-Za-z0-9_.-]*)\2")
_SHELLS = {"bash", "sh", "zsh", "dash"}
_ENV_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
#: Words that run the command after them: `uv run [opts] probe ...`, `time probe ...`.
_LAUNCHERS = {"uv", "uvx", "pipx", "poetry", "pdm", "npx", "time", "sudo", "nohup", "env", "exec", "command",
              "timeout", "nice", "xargs", "caffeinate", "stdbuf",
              # shell keywords a command follows: `do probe ...`, `then probe ...`
              "do", "then", "else", "elif", "!", "{"}
#: A heredoc opener is looked for on the line with its quoted text and arithmetic
#: blanked: `python -c 'print(1<<2)'` and `$((1<<2))` open nothing.
_QUOTED_OR_ARITH = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"|\$\(\([^)]*\)\)")
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit", "write", "edit"}
#: A Probe MCP tool, under any harness's spelling of its name.
_PROBE_MCP_RE = re.compile(r"probe[-_]research", re.IGNORECASE)
#: Root `probe` options whose VALUE is not a command word (session_marker's
#: ROOT_VALUE_OPTIONS; the tap cannot import it).
_ROOT_VALUE_OPTIONS = {"--base-url", "--spool-dir"}
#: `probe` commands whose OUTPUT names an entity the session just acted on: it
#: created it, or launched a run into it. Ids in a LISTING (`run list`, MCP
#: `browse`) or in a file the agent read are context, never write targets.
_ACTED_HEADS = {"exec", "snapshot"}
_ACTED_VERBS = {
    "run start", "run child", "run fork", "project create", "experiment create", "group create",
}
#: Shell commands that only READ: a path in their output is something the
#: session looked at, not something it produced.
_READER_COMMANDS = {
    "cat", "less", "more", "head", "tail", "grep", "rg", "ag", "ls", "find", "fd", "tree",
    "git", "sed", "awk", "wc", "file", "stat", "du", "diff", "jq", "yq", "echo", "printf",
    "which", "type", "env", "printenv", "probe",
}


@dataclass
class Event:
    offset: int
    role: str
    text: str = ""
    tool: str | None = None
    call_id: str | None = None
    tool_input: dict = field(default_factory=dict)
    is_error: bool = False


@dataclass
class Observation:
    """What one chunk of transcript taught the worker, beyond the events.

    `ids` are the entities the session ACTED on (see `_ACTED_VERBS`), the only
    ones the daemon may write to. `produced_text` is the output of the shell
    commands that did work (not `cat`/`ls`/...), where a file the session
    produced is named -- the artifact gate's evidence. `workdirs` are the
    folders those commands `cd`'d into, in order: a relative path in the
    output is relative to one of them, not to where the session started.
    """

    events: list[Event]
    ids: dict[str, tuple[str | None, int, str]]
    run_starts: set[str]
    run_ends: set[str]
    directed: list[tuple[int, str]]
    touched_files: set[str]
    redactions: int
    produced_text: str = ""
    workdirs: list[str] = field(default_factory=list)
    # Tool calls whose result has not arrived yet (call id -> {"offset", "tool",
    # "command"}), carried to the next chunk so a call read in one cycle and
    # answered in the next still grounds its run id and produced files.
    open_calls: dict[str, dict] = field(default_factory=dict)
    # Runs named in the OUTPUT of the session's own work commands (a sweep script
    # printing each run's URL): not acted on by a `probe` command, so not in
    # `ids`; the worker admits one only inside an experiment the session acted on.
    mentioned_runs: dict[str, int] = field(default_factory=dict)


def _cap(text: str, limit: int, event_id: str | None = None) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head - 30
    how = f'; {{"expand": "{event_id}"}} pages through them' if event_id else ""
    return f"{text[:head]}\n…[{len(text) - head - tail} chars cut{how}]…\n{text[-tail:]}"


def _block_texts(content: Any) -> list[str]:
    if isinstance(content, str):
        return [content]
    out: list[str] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                out.append(block["text"])
    return out


# ---------------------------------------------------------------------------
# One parser per harness. Each returns zero or more Events for one raw line.
# ---------------------------------------------------------------------------


def _claude(obj: dict, offset: int) -> list[Event]:
    kind = obj.get("type")
    message = obj.get("message")
    if kind not in ("user", "assistant") or not isinstance(message, dict):
        return []
    if obj.get("isMeta") or obj.get("isSidechain"):
        return []
    content = message.get("content")
    if kind == "user":
        if isinstance(content, str):
            return [Event(offset, USER, text=content)]
        events: list[Event] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                events.append(Event(offset, USER, text=block["text"]))
            elif block.get("type") == "tool_result":
                events.append(
                    Event(
                        offset,
                        TOOL_RESULT,
                        text="\n".join(_block_texts(block.get("content"))),
                        call_id=block.get("tool_use_id"),
                        is_error=bool(block.get("is_error")),
                    )
                )
        return events
    events = []
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            events.append(Event(offset, ASSISTANT, text=block["text"]))
        elif block.get("type") == "tool_use":
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            events.append(
                Event(offset, TOOL_CALL, tool=str(block.get("name") or ""), call_id=block.get("id"), tool_input=tool_input)
            )
    return events


_CODEX_STARTUP_PREFIXES = ("<environment_context>", "<user_instructions>", "# AGENTS.md", "<permissions")


def _codex(obj: dict, offset: int) -> list[Event]:
    if obj.get("type") != "response_item":
        return []
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return []
    inner = payload.get("type")
    if inner == "message":
        role = payload.get("role")
        if role not in ("user", "assistant"):
            return []
        texts = _block_texts(payload.get("content"))
        texts = [t for t in texts if not t.lstrip().startswith(_CODEX_STARTUP_PREFIXES)]
        return [Event(offset, USER if role == "user" else ASSISTANT, text=t) for t in texts if t.strip()]
    if inner == "function_call":
        raw = payload.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
        except ValueError:
            args = {"arguments": raw}
        if isinstance(args.get("command"), list):
            args = {**args, "command": " ".join(str(part) for part in args["command"])}
        return [Event(offset, TOOL_CALL, tool=str(payload.get("name") or ""), call_id=payload.get("call_id"), tool_input=args if isinstance(args, dict) else {})]
    if inner == "local_shell_call":
        action = payload.get("action") if isinstance(payload.get("action"), dict) else {}
        command = action.get("command")
        text = " ".join(command) if isinstance(command, list) else str(command or "")
        return [Event(offset, TOOL_CALL, tool="shell", call_id=payload.get("call_id") or payload.get("id"), tool_input={"command": text})]
    if inner == "custom_tool_call":
        return [Event(offset, TOOL_CALL, tool=str(payload.get("name") or "custom_tool"), call_id=payload.get("call_id"), tool_input={"input": payload.get("input") if isinstance(payload.get("input"), str) else ""})]
    if inner in ("function_call_output", "custom_tool_call_output"):
        output = payload.get("output")
        if isinstance(output, dict):
            output = output.get("content") or output.get("output") or json.dumps(output)
        elif isinstance(output, str):
            try:
                parsed = json.loads(output)
                if isinstance(parsed, dict) and isinstance(parsed.get("output"), str):
                    output = parsed["output"]
            except ValueError:
                pass
        return [Event(offset, TOOL_RESULT, text=str(output or ""), call_id=payload.get("call_id"))]
    return []


def _pi(obj: dict, offset: int) -> list[Event]:
    if obj.get("type") != "message":
        return []
    message = obj.get("message")
    if not isinstance(message, dict):
        return []
    role = message.get("role")
    content = message.get("content")
    if role == "user":
        return [Event(offset, USER, text=t) for t in _block_texts(content) if t.strip()]
    if role == "assistant":
        events: list[Event] = []
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                events.append(Event(offset, ASSISTANT, text=block["text"]))
            elif block.get("type") == "toolCall":
                args = block.get("arguments") if isinstance(block.get("arguments"), dict) else {}
                events.append(Event(offset, TOOL_CALL, tool=str(block.get("name") or ""), call_id=block.get("id"), tool_input=args))
        return events
    if role == "toolResult":
        return [
            Event(
                offset,
                TOOL_RESULT,
                text="\n".join(_block_texts(content)),
                tool=message.get("toolName") if isinstance(message.get("toolName"), str) else None,
                call_id=message.get("toolCallId") if isinstance(message.get("toolCallId"), str) else None,
                is_error=bool(message.get("isError")),
            )
        ]
    if role == "bashExecution":
        command = message.get("command") if isinstance(message.get("command"), str) else ""
        output = message.get("output") if isinstance(message.get("output"), str) else ""
        return [
            Event(offset, TOOL_CALL, tool="bash", call_id=None, tool_input={"command": command}),
            Event(offset, TOOL_RESULT, text=output),
        ]
    return []


PARSERS = {"claude_code": _claude, "codex": _codex, "pi": _pi}


def parse_lines(source: str, lines: list[tuple[int, bytes]]) -> list[Event]:
    """`lines` is `[(end_offset, raw_line)]`; malformed lines are skipped."""
    parser = PARSERS.get(source, _claude)
    events: list[Event] = []
    for offset, raw in lines:
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if isinstance(obj, dict):
            events.extend(parser(obj, offset))
    return events


def read_chunk(path: Path, start: int, *, max_bytes: int) -> tuple[list[tuple[int, bytes]], int]:
    """Complete lines from `start`, at most `max_bytes`; returns `(lines, new_offset)`.

    A transcript that shrank below `start` was rewritten; the worker starts over
    from 0 and its ledger's idempotency keys stop it re-writing what it wrote.
    """
    size = path.stat().st_size
    if size < start:
        start = 0
    if size <= start:
        return [], start
    with path.open("rb") as handle:
        handle.seek(start)
        buf = handle.read(max_bytes)
    out: list[tuple[int, bytes]] = []
    pos = 0
    while True:
        nl = buf.find(b"\n", pos)
        if nl < 0:
            break
        line = buf[pos:nl]
        pos = nl + 1
        if line.strip():
            out.append((start + pos, line))
    if not out and pos == 0 and len(buf) == max_bytes:
        # One line longer than the budget. Read it whole when it is not absurd
        # (a big tool result can still carry a run id); skip past it otherwise.
        nl = _find_newline(path, start + len(buf))
        if nl is None:
            return [], start
        if nl - start <= MAX_LINE_BYTES:
            with path.open("rb") as handle:
                handle.seek(start)
                line = handle.read(nl - start).rstrip(b"\n")
            return [(nl, line)], nl
        return [], nl
    return out, start + pos


def _find_newline(path: Path, from_offset: int) -> int | None:
    with path.open("rb") as handle:
        handle.seek(from_offset)
        consumed = from_offset
        while True:
            block = handle.read(1 << 20)
            if not block:
                return None
            nl = block.find(b"\n")
            if nl >= 0:
                return consumed + nl + 1
            consumed += len(block)


# ---------------------------------------------------------------------------
# What a chunk taught us beyond its text.
# ---------------------------------------------------------------------------


def _command_of(event: Event) -> str:
    for key in ("command", "cmd", "input"):
        value = event.tool_input.get(key)
        if isinstance(value, str):
            return value
    return ""


def probe_words(rest: str) -> list[str]:
    """The first two command words after `probe` (root option values skipped)."""
    words: list[str] = []
    skip = False
    for token in rest.split():
        if skip:
            skip = False
            continue
        if token == "--":
            if words[:1] == ["exec"]:
                break
            continue
        if token.startswith("-"):
            skip = not words and token in _ROOT_VALUE_OPTIONS
            continue
        words.append(token)
        if len(words) == 2:
            break
    return words


def _strip_heredocs(command: str) -> str:
    """The command without its heredoc BODIES (text on stdin, not commands)."""
    kept: list[str] = []
    pending: list[tuple[str, bool]] = []
    for line in command.split("\n"):
        if pending:
            word, tabs = pending[0]
            if (line.lstrip("\t") if tabs else line) == word:
                pending.pop(0)
            continue
        kept.append(line)
        pending.extend((m.group(3), bool(m.group(1))) for m in _HEREDOC.finditer(_QUOTED_OR_ARITH.sub(" ", line)))
    return "\n".join(kept)


def _shell_segments(command: str) -> list[list[str]]:
    """Each simple command's words, split on operators OUTSIDE quotes, so quoted
    text (`grep -E '^(wrote|probe run)'`) is one word, never a command. A segment
    left open by unbalanced quotes is dropped."""
    lexer = shlex.shlex(_strip_heredocs(command).replace("\\\n", " "), posix=True,
                        punctuation_chars=_OPERATOR_CHARS)
    lexer.whitespace_split = True
    lexer.whitespace = " \t\r"
    lexer.commenters = ""
    segments: list[list[str]] = []
    segment: list[str] = []
    try:
        for token in lexer:
            if set(token) <= set(_OPERATOR_CHARS) and set(token) & _SEPARATOR_CHARS:
                if segment:
                    segments.append(segment)
                segment = []
            else:
                segment.append(token)
    except ValueError:
        return segments
    if segment:
        segments.append(segment)
    return segments


def _probe_invocations(command: str, depth: int = 0) -> list[str]:
    """The text after `probe` for each probe invocation in a shell command: the
    first `probe` word of each simple command (after a launcher like `uv run`
    too), inside a `bash -lc '...'` wrapper too, never in quotes or a heredoc."""
    out: list[str] = []
    for words in _shell_segments(command):
        while words and _ENV_WORD.match(words[0]):
            words = words[1:]
        if not words:
            continue
        head = words[0].rsplit("/", 1)[-1]
        if depth < 2 and head in _SHELLS:
            # `bash [--norc ...] -lc '<command>'`: the command string is the shell's.
            for i, word in enumerate(words[1:], start=1):
                if re.fullmatch(r"-[A-Za-z]*c[A-Za-z]*", word):
                    if i + 1 < len(words):
                        out += _probe_invocations(words[i + 1], depth + 1)
                    break
                if not word.startswith("-"):
                    break
            continue
        # `probe` is the command itself, or what a launcher runs (`uv run --with
        # probe-research probe exec ...`, `time probe ...`); never an argument of
        # another command (`echo probe run end <id>`).
        at = 0 if head == "probe" else None
        if at is None and head in _LAUNCHERS:
            at = next((i for i, w in enumerate(words) if w == "probe" or w.endswith("/probe")), None)
        if at is not None:
            out.append(" ".join(words[at + 1 :]))
    return out


def _probe_segments(command: str) -> list[list[str]]:
    return [probe_words(rest) for rest in _probe_invocations(command)]


def command_of(event: Event | None) -> str:
    """The shell command a tool call ran ("" for anything else)."""
    return _command_of(event) if event is not None else ""


def is_work_command(command: str) -> bool:
    """A command that did work: a probe command that acted, or anything that is
    not a pure reader (`cat`, `ls`, `grep`...). Its output is evidence."""
    return bool(command) and (_acted(_probe_segments(command)) or not _is_reader(command))


def from_probe_tool(context: str | None) -> bool:
    """Was an id first seen in a call to a Probe MCP tool (a read), rather than
    in a `probe` command the session ran?"""
    # A tool's name has no spaces; a command line (even `uv run --with
    # probe-research ...`) does.
    return bool(context) and not any(c.isspace() for c in context) and bool(_PROBE_MCP_RE.search(context))


def _acted(segments: list[list[str]]) -> bool:
    for words in segments:
        if words[:1] and (words[0] in _ACTED_HEADS or " ".join(words[:2]) in _ACTED_VERBS):
            return True
    return False


_SHELL_WRAPPER_RE = re.compile(r"^\s*(?:ba|z|da)?sh\s+-l?c\s+(['\"])(?P<inner>.*)\1\s*$", re.S)
_SEGMENT_SPLIT_RE = re.compile(r"&&|\|\||;|\|")


def _is_reader(command: str) -> bool:
    """Only READ: every segment starts with a reader (or `cd`), through a
    `bash -lc '...'` wrapper (how Codex runs every command)."""
    wrapped = _SHELL_WRAPPER_RE.match(command)
    if wrapped:
        command = wrapped.group("inner")
    for segment in _SEGMENT_SPLIT_RE.split(command):
        words = segment.strip().split()
        # `env X=1 python train.py` and `X=1 python train.py` run python: skip
        # the environment to find the command.
        while words and ((words[0] == "env" and len(words) > 1) or _ENV_WORD.match(words[0])):
            words = words[1:]
        if not words:
            continue
        head = words[0].rsplit("/", 1)[-1]
        if head != "cd" and head not in _READER_COMMANDS:
            return False
    return True


OPEN_CALLS_KEPT = 200
OPEN_CALL_COMMAND_CHARS = 20_000


def observe(source: str, lines: list[tuple[int, bytes]], open_calls: dict[str, dict] | None = None) -> Observation:
    """What `lines` show. `open_calls` are the calls earlier chunks left
    unanswered (`Observation.open_calls` of the previous cycle)."""
    events = parse_lines(source, lines)
    calls: dict[str, Event] = {
        cid: Event(offset=int(row.get("offset") or 0), role=TOOL_CALL, tool=row.get("tool"), call_id=cid,
                   tool_input={"command": row.get("command") or ""})
        for cid, row in (open_calls or {}).items() if isinstance(row, dict)
    }
    calls.update({e.call_id: e for e in events if e.role == TOOL_CALL and e.call_id})
    ids: dict[str, tuple[str | None, int, str]] = {}
    run_starts: set[str] = set()
    run_ends: set[str] = set()
    directed: list[tuple[int, str]] = []
    touched: set[str] = set()
    produced: list[str] = []
    workdirs: list[str] = []
    mentioned: dict[str, int] = {}
    redactions = 0

    def ground(text: str, offset: int, context: str) -> None:
        for match in _TYPED_ID_RE.finditer(text):
            ids.setdefault(match.group("id"), (match.group("kind").lower(), offset, context))
        for found in UUID_RE.findall(text):
            ids.setdefault(found, (None, offset, context))

    for event in events:
        if event.role == TOOL_CALL:
            command = _command_of(event)
            for rest in _probe_invocations(command):
                if "--directed" in rest.split():
                    directed.append((event.offset, secrets.redact("probe " + rest.strip())[0]))
                if probe_words(rest)[:2] == ["run", "end"]:
                    run_ends.update(UUID_RE.findall(rest))
                # An id the agent NAMED in its own probe command is one it acted on.
                ground(rest, event.offset, command[:200])
            if event.tool and _PROBE_MCP_RE.search(event.tool):
                # ...and so is one it passed to a Probe tool, as opposed to one a
                # listing merely returned.
                ground(json.dumps(event.tool_input, default=str), event.offset, event.tool)
            for found in _CD_RE.findall(command):
                workdirs.append(found.strip("'\""))
            path = event.tool_input.get("file_path") or event.tool_input.get("path")
            if event.tool in _WRITE_TOOLS and isinstance(path, str):
                touched.add(path)
            if event.tool == "apply_patch" or "*** Begin Patch" in command:
                touched.update(re.findall(r"^\*\*\* (?:Add|Update) File: (.+)$", command, re.M))
        if event.role == TOOL_RESULT:
            call = calls.get(event.call_id or "")
            command = _command_of(call) if call else ""
            segments = _probe_segments(command)
            if _acted(segments):
                ground(event.text, event.offset, command[:200])
            if any(words[:2] == ["run", "start"] for words in segments):
                run_starts.update(UUID_RE.findall(event.text)[:1])
            if command and not segments and not _is_reader(command):
                # The OUTPUT only: a path in the command line is as likely an
                # input (`python eval.py --data customers.parquet`) as a result.
                produced.append(event.text)
                for match in _TYPED_ID_RE.finditer(event.text):
                    if match.group("kind").lower() == "run":
                        mentioned.setdefault(match.group("id").lower(), event.offset)
        # Redact LAST, and in place: everything downstream sees only this.
        clean, fired = secrets.redact(event.text)
        if fired:
            redactions += len(fired)
            event.text = clean
        if event.tool_input:
            redacted_input, fired = secrets.redact_event(event.tool_input)
            if fired:
                redactions += len(fired)
                event.tool_input = redacted_input if isinstance(redacted_input, dict) else {}
    answered = {e.call_id for e in events if e.role == TOOL_RESULT and e.call_id}
    still_open = {
        cid: {"offset": call.offset, "tool": call.tool,
              "command": secrets.redact(_command_of(call))[0][:OPEN_CALL_COMMAND_CHARS]}
        for cid, call in calls.items() if cid not in answered
    }
    still_open = dict(sorted(still_open.items(), key=lambda kv: kv[1]["offset"])[-OPEN_CALLS_KEPT:])
    return Observation(
        events, ids, run_starts, run_ends, directed, touched, redactions, "\n".join(produced), workdirs,
        still_open, mentioned,
    )


def render_event(event: Event, *, whole: bool = True, index: int = 0) -> str:
    """One event as the model sees it: `[role @offset]` and its text.

    `whole` (the daemon's view): every event whole up to EVENT_CEILING (a
    command almost always is; a 500 KB file handed to a Write tool is not), past it
    cut with the id that pages through the rest. Otherwise the short per-event caps
    (the earlier-context block)."""
    text_cap, command_cap, result_cap = (
        (EVENT_CEILING, EVENT_CEILING, EVENT_CEILING) if whole else (TEXT_CAP, COMMAND_CAP, RESULT_CAP)
    )
    eid = f"{event.offset}:{index}" if whole else None
    if event.role in (USER, ASSISTANT):
        text = event.text.strip()
        return f"[{event.role} @{event.offset}]\n{_cap(text, text_cap, eid)}" if text else ""
    if event.role == TOOL_CALL:
        command = _command_of(event)
        detail = _cap(command or json.dumps(event.tool_input, default=str), command_cap, eid)
        return f"[tool_call {event.tool} @{event.offset}]\n{detail}"
    if event.role == TOOL_RESULT:
        status = " error" if event.is_error else ""
        return f"[tool_result{status} @{event.offset}]\n{_cap(event.text.strip(), result_cap, eid)}"
    return ""


def render(events: list[Event], *, whole: bool = True) -> str:
    """The model's view of a chunk: one block per event (see `render_event`)."""
    return "\n\n".join(part for part in (render_event(e, whole=whole, index=i) for e, i in indexed(events)) if part)


def indexed(events: list[Event]) -> list[tuple[Event, int]]:
    """Each event with its index among the events of its JSONL line: one line can
    hold several (Codex O13), so an event's id is `<line end offset>:<index>`."""
    out: list[tuple[Event, int]] = []
    last, index = None, 0
    for event in events:
        index = index + 1 if event.offset == last else 0
        last = event.offset
        out.append((event, index))
    return out


# ---------------------------------------------------------------------------
# Retrieval: what the model may ask for when the view is not enough.
# ---------------------------------------------------------------------------

EXPAND_PAGE_CHARS = 48 * 1024
GREP_MAX_HITS = 50
GREP_HIT_CHARS = 300


def _line_ending_at(path: Path, end: int) -> bytes | None:
    """The JSONL line whose end offset (just past its newline) is `end`."""
    if end <= 0:
        return None
    with path.open("rb") as handle:
        handle.seek(end - 1)
        if handle.read(1) != b"\n":
            return None
        begin, pos = 0, end - 1  # search for the newline BEFORE the one ending the line
        while pos > 0:
            step = min(64 * 1024, pos)
            handle.seek(pos - step)
            nl = handle.read(step).rfind(b"\n")
            if nl >= 0:
                begin = pos - step + nl + 1
                break
            pos -= step
            if end - pos > MAX_LINE_BYTES:
                return None
        handle.seek(begin)
        return handle.read(end - 1 - begin)


def read_event(path: Path, source: str, event_id: str) -> Event | None:
    """One event by its id (`<line end offset>:<index>`), redacted, or None."""
    try:
        end_s, index_s = event_id.split(":", 1)
        end, index = int(end_s), int(index_s)
    except ValueError:
        return None
    raw = _line_ending_at(path, end)
    if raw is None:
        return None
    events = parse_lines(source, [(end, raw)])
    if not 0 <= index < len(events):
        return None
    event = events[index]
    event.text = secrets.redact(event.text)[0]
    if event.tool_input:
        redacted, _ = secrets.redact_event(event.tool_input)
        event.tool_input = redacted if isinstance(redacted, dict) else {}
    return event


def event_text(event: Event) -> str:
    """What an expansion pages through: the text, or a call's whole input."""
    if event.role == TOOL_CALL:
        return _command_of(event) or json.dumps(event.tool_input, default=str)
    return event.text


def grep(path: Path, source: str, needle: str, upto: int) -> list[tuple[str, str]]:
    """Case-insensitive plain-text search over every event up to `upto`:
    `[(event id, a <= GREP_HIT_CHARS window around the hit, redacted)]`."""
    needle = needle.strip().lower()
    if not needle:
        return []
    hits: list[tuple[str, str]] = []
    offset = 0
    while offset < upto and len(hits) < GREP_MAX_HITS:
        lines, new = read_chunk(path, offset, max_bytes=2 * 1024 * 1024)
        if new <= offset:
            break
        for event, index in indexed(parse_lines(source, [(o, raw) for o, raw in lines if o <= upto])):
            # Redact the WHOLE event before searching or cutting: a window cut from
            # raw text can start inside a secret, past the key name the redactor
            # anchors on, and a search over raw text answers whether a secret exists.
            text = secrets.redact(event_text(event))[0]
            at = text.lower().find(needle)
            if at < 0:
                continue
            lo = max(0, at - (GREP_HIT_CHARS - len(needle)) // 2)
            window = text[lo : lo + GREP_HIT_CHARS]
            hits.append((f"{event.offset}:{index}", window.replace("\n", " ")))
            if len(hits) >= GREP_MAX_HITS:
                break
        offset = new
    return hits
