"""Sanitize Claude Code transcript events before shipping.

What we ship: the *conversation* — user prompts, assistant text + thinking,
plus a one-line marker for each tool call. Everything else is noise:
  - Anthropic API metadata (per-TTL cache breakdowns, service tier, request
    ids, big base64 signature blobs on thinking blocks). Since tap 0.9.11 an
    assistant event keeps the message id and the four token counts, as
    `inference_id` and `usage`.
  - CC-internal bookkeeping events:
      * `stop_hook_summary`, `turn_duration` (system subtypes)
      * `file-history-snapshot` (75% of payload weight; pure backup metadata)
      * `last-prompt`, `ai-title`, `permission-mode` (UI / mode plumbing)
  - Every top-level field not on `_KEEP_TOP_LEVEL`: the per-event copies of
    `cwd`, `gitBranch`, `sessionId` (already on the doc), CC plumbing, and
    `toolUseResult` (CC's second copy of every tool's OUTPUT).
  - Empty `thinking: ""` blocks (assistant turns where the model didn't
    surface any reasoning text — the empty block carries no content).
  - Full tool_use `input` args EXCEPT Bash's `command` (the full
    old_string/new_string of an Edit, the search/replace bodies, …)
  - Full tool_result `content` (file contents, command output, search
    results — usually the single largest chunk of any session payload)
  - Pasted image bytes, and any content block or attachment type this file
    does not name.
  - What a command the RESEARCHER ran printed (tap 0.9.13): a `!command`'s
    `<bash-stdout>`/`<bash-stderr>` and a local slash command's
    `<local-command-stdout>`/`<local-command-stderr>` keep their tags, and
    the text inside becomes OUTPUT_WITHHELD (`withhold_output`).
  - A loaded skill's body: the `isMeta` user turn Claude Code injects when a
    skill fires, starting "Base directory for this skill:" (tap 0.9.13).

ALLOW-LISTS, NOT DENY-LISTS (tap 0.9.10). The consent screen promises that file
contents, command output and search results are not sent
(`app/assistant/product.md`, held by `dashboard/src/lib/capture-disclosure.test.ts`).
Three deny-lists broke that promise by letting through whatever they did not
name: the top-level `toolUseResult` (stdout/stderr, `originalFile`, whole edit
bodies — 54% of the sanitized bytes of one measured 12.9 MB session), the
`file` / `edited_text_file` / `nested_memory` attachments, and every content
block type Claude Code added after this file was written. Claude Code keeps
adding fields (`wireToolInputs`, an attachment's `rendered` text, a
`prompt_snapshot` of the whole system prompt), so each level now names what
ships and drops the rest:
  - top-level keys      `_KEEP_TOP_LEVEL`
  - `message` keys      `_KEEP_MESSAGE`
  - content blocks      text, thinking, tool_use, tool_result, image
                        (a placeholder); any other type ships as
                        `{"type": <type>, "dropped": true}`
  - attachment types    `_KEEP_ATTACHMENTS`, each with its own key list

THE OUTPUT IS A CONTRACT: `probe-events-1.schema.json` beside this file
describes what all four sanitizers emit, and
`tests/test_probe_events_contract.py` holds them to it. An assistant event
also carries `inference_id` (Anthropic's message id) and `usage` (the four
token counts in USAGE_KEYS), lifted out of `message` rather than copied.

REDACTION IS NOT DONE HERE. `Journal.stage` and the legacy
`transcript.build_batch_body` run `secrets.redact_event` on this output, so a credential in a prompt or
a shell command is replaced before the event joins a batch. It lives there, not
here, because all three lanes (this one, Codex, pi) and all three producers
(the live daemon, the reconciler, the importer) converge on that one function.
Do not add a second redactor to this module.

We KEEP enough of each tool block to reconstruct what happened:
  - tool_use:    type, id, name, summary — the FULL command for Bash
                 (shell lines are a session's method section; capped at
                 _COMMAND_MAX_LEN), first line of path/pattern/etc for the rest
  - tool_result: type, tool_use_id, is_error (only when truthy), result_bytes

`sanitize_event(event)` returns:
  - None        → drop the event entirely (CC bookkeeping with no content)
  - dict        → trimmed copy with the noise fields removed
  - input as-is → if the input isn't a dict (defensive — non-JSON lines
                  shouldn't reach here, but if they do we don't mangle them)

CANONICAL: src/probe/tap_core/sanitize.py — the copy under
plugins/probe-research-tap/tap/ is vendored by `make sync-tap-core` and must
stay byte-identical (tests/test_tap_core_sync.py guards it). Edit the
canonical file, never the plugin copy.
"""

from __future__ import annotations

import re
from typing import Any

# Top-level event types to drop entirely. These are CC-internal bookkeeping
# with no conversational content. file-history-snapshot dominates payload
# weight (~75% of typical session bytes); the others are smaller but pure
# UI/mode plumbing that contribute zero retrieval signal.
_DROP_EVENT_TYPES: frozenset[str] = frozenset({
    "file-history-snapshot",
    "last-prompt",
    "ai-title",
    "permission-mode",
})

# `attachment` events are harness plumbing that CC injects around the
# conversation: 12,217 events and 11.5 MB across ten measured sessions, none of
# which produce a single character of indexed text (the renderer has no case
# for them). Most are hook output, registry dumps and reminders; the rest carry
# FILE CONTENT -- `file` and `nested_memory` (a whole file or CLAUDE.md),
# `edited_text_file` (a snippet of the edited file), `instructions`, and
# `prompt_snapshot` (the whole system prompt). It used to be a deny-list "so a
# type we have not seen still ships", and that is exactly how the file-bearing
# ones shipped.
#
# ALLOW-LIST, and per type an allow-list of its keys, so a kept type that grows
# a content field later still ships only what is named here. Each entry was
# checked against real transcripts (Claude Code 2.x, 2026-10) for its key set.
# An attachment of any other type drops the whole event.
_KEEP_ATTACHMENTS: dict[str, tuple[str, ...]] = {
    # Which files were in context when a compaction ran: a path, never content.
    # Observed keys: type, filename, displayPath.
    "compact_file_reference": ("filename", "displayPath"),
}

# Top-level fields that ship. Everything else on an event is dropped: the
# per-event copies of cwd / gitBranch / sessionId (already on the doc), CC
# plumbing (promptId, entrypoint, userType, version, slug, requestId, ...),
# `toolUseResult`, and whatever Claude Code adds next.
#
# Each key is here because something reads it:
#   type, subtype       engine `transcript_render._render_event` dispatches on
#                       them; `compact_boundary` is counted and segmented on
#                       (`claude_code._count_compactions`, extraction)
#   message             the conversation (its own allow-list below)
#   content             engine renders a top-level string `content` for system
#                       events and unknown event types, and reads the first
#                       event's for the document preview (strings only)
#   isCompactSummary    engine labels it COMPACTION SUMMARY instead of USER
#   timestamp           engine's session-complete check reads it
#   uuid, parentUuid,   identity and order: the only link from one event to
#   logicalParentUuid   the one before it (logicalParentUuid carries that link
#                       across a compaction boundary)
#   attachment          the payload of a kept attachment (filtered above)
#
# `toolUseResult` is CC's SECOND copy of every tool's output (stdout/stderr,
# `originalFile`, whole edit bodies). The tool_result block below already
# carries its size; nothing in research-os or the engine reads the field.
#
# Each key maps to the JSON types it may hold (probe-events-1.schema.json): a
# value of any other type is dropped, so a key that is named still cannot carry
# an unnamed payload. Every real CC 2.x line checked (2026-10) conforms.
_NULL = type(None)
_KEEP_TOP_LEVEL: dict[str, tuple[type, ...]] = {
    "type": (str,),
    "subtype": (str,),
    "message": (dict,),
    "content": (str,),
    "isCompactSummary": (bool,),
    "timestamp": (str,),
    "uuid": (str,),
    "parentUuid": (str, _NULL),
    "logicalParentUuid": (str, _NULL),
    "attachment": (dict,),
}

# Fields inside `message` that ship. role + content are the conversation;
# stop_reason is rendered for non-default stops (`[stop: max_tokens]`); model
# says which model wrote the turn. Dropped: Anthropic's API metadata (type,
# stop_sequence, stop_details, service tier, ...) and CC's per-request
# bookkeeping (`container`, `context_management`, and `input_transformations`,
# a list of file paths with reasons). `id` and `usage` are lifted out of the
# message instead, as `inference_id` and `usage` (see sanitize_event).
_KEEP_MESSAGE: dict[str, tuple[type, ...]] = {
    "role": (str,),
    "content": (str, list),
    "model": (str,),
    "stop_reason": (str, _NULL),
}

#: The one token-usage shape every sanitizer emits, as `usage` on an assistant
#: event, whatever its harness calls the counts. Non-negative ints; a count the
#: harness did not report is absent, never 0. PUBLIC because pi_sanitize maps
#: its own names onto it.
USAGE_KEYS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)

#: `origin` on an event a researcher's own shell command produced (pi's `!`
#: bashExecution, Kimi's shell mode), so no reader takes it for model output.
#: The only value probe-events/1 defines. PUBLIC for pi_sanitize / kimi_sanitize.
ORIGIN_USER_SHELL = "user_shell"

# What a command the RESEARCHER ran printed, which Claude Code keeps as
# conversation text rather than as a tool result: a `!command` (bash mode) is a
# user turn `<bash-stdout>…</bash-stdout><bash-stderr>…</bash-stderr>`, and a
# local slash command (`/model`, `/cost`, `/context`, `/exit`'s "Bye!")
# prints `<local-command-stdout>…</local-command-stdout>` on a user turn or in
# a `system` `local_command` event's `content`. Across this devbox's Claude
# Code transcripts (2026-10-06): 444 user turns and 68 system events carried
# local-command output, 7 user turns a `!command`'s. The consent screen says
# command output is not sent (app/assistant/product.md); through tap 0.9.12
# it was. The tags stay, so a reader still sees that the command printed and
# how much; the text inside does not (`withhold_output`).
_OUTPUT_TAG_NAMES = ("bash-stdout", "bash-stderr", "local-command-stdout", "local-command-stderr")
_OUTPUT_TAG = re.compile(r"<(/?)(" + "|".join(_OUTPUT_TAG_NAMES) + r")>")

#: What replaces withheld command output, in every sanitizer that withholds
#: some (here, and codex_sanitize for Codex's `!command`): its size in
#: characters, comma-grouped. Empty output stays empty. PUBLIC for
#: codex_sanitize; one format, so a reader matches one string.
OUTPUT_WITHHELD = "[output not sent: {:,} chars]"

# When a skill fires, Claude Code injects its whole SKILL.md as an `isMeta`
# user turn starting with this line (313 of them on this devbox, 2026-10-06).
# It is a file's content, not something anyone said: the engine already drops
# any user text with this line when rendering (transcript_render
# `_INJECTED_DOCUMENT`), so it was uploaded and never read. Since tap 0.9.13
# it is not uploaded.
_SKILL_BODY_PREFIX = "Base directory for this skill:"

# `system` events with these subtypes have no content — drop entirely.
# stop_hook_summary  = CC's per-hook timing/output; pure bookkeeping.
# turn_duration      = how long a turn took; pure bookkeeping.
_DROP_SYSTEM_SUBTYPES: frozenset[str] = frozenset({
    "stop_hook_summary",
    "turn_duration",
})

# When summarizing a tool_use's `input`, pick the FIRST key from this list
# that holds a non-empty string. Order matches "most identifying" per tool:
#   command     — Bash (the actual shell line)
#   file_path   — Read / Edit / Write / NotebookEdit
#   pattern     — Grep / Glob (the search expression — more identifying than path)
#   url         — WebFetch
#   query       — WebSearch / search-style MCP tools
#   path        — generic fallback for tools that name it `path` (lower than
#                 pattern so Grep is summarized by what it searches for)
#   description — last-resort fallback for tools whose schema we don't know
_TOOL_SUMMARY_KEYS: tuple[str, ...] = (
    "command",
    "file_path",
    "pattern",
    "url",
    "query",
    "path",
    "description",
)

# Hard cap on the summary length so a runaway one-line value (e.g. a
# minified script jammed onto one line) can't bloat payloads on its own.
_TOOL_SUMMARY_MAX_LEN = 200

# `command` is the exception to first-line-only (decided 2026-08-13, session
# digests review): a data-processing session's method IS its shell commands,
# and heredoc/inline-script bodies were exactly what first-line summaries
# dropped. The full command ships — outputs still never do — under its own,
# larger cap so a pathological one-liner can't bloat a batch.
#: PUBLIC because the Codex and pi sanitizers import it. It used to be
#: redeclared in each of them, with codex_sanitize.py carrying the comment
#: "Parity with cc-tap's sanitize.py" -- a decision written down three times is
#: a decision that drifts. One definition, three importers.
COMMAND_MAX_LEN = 4000
_COMMAND_MAX_LEN = COMMAND_MAX_LEN  # backwards-compatible local alias

#: A URL is a reference and costs nothing to keep; anything longer is not a
#: URL. Same bound as codex_sanitize.MAX_IMAGE_URL and kimi's media URLs.
_MAX_IMAGE_URL = 2048
#: Cap for a metadata string copied out of a block: a media type, the name of
#: a dropped block type.
_METADATA_MAX_LEN = 128


def sanitize_event(event: Any) -> Any:
    """Trim a transcript event to ship only the conversation, not metadata.

    Returns None for events that should be dropped entirely.
    """
    if not isinstance(event, dict):
        return event

    # An event that does not say what it is cannot be put on any allow-list
    # (probe-events/1 requires `type`). Every real CC line has one.
    if not isinstance(event.get("type"), str):
        return None

    # Drop entire bookkeeping event types (file-history-snapshot, last-prompt,
    # ai-title, permission-mode). These never carry conversational content.
    # A list or object here would make the set lookups below raise.
    ev_type = event.get("type")
    if isinstance(ev_type, str) and ev_type in _DROP_EVENT_TYPES:
        return None

    # Drop CC-internal system events with no content value.
    if ev_type == "system":
        sub = event.get("subtype")
        if isinstance(sub, str) and sub in _DROP_SYSTEM_SUBTYPES:
            return None

    # A loaded skill's body is a file, not a turn (see _SKILL_BODY_PREFIX).
    if ev_type == "user" and event.get("isMeta") is True and _is_skill_body(event.get("message")):
        return None

    out = {k: v for k, v in event.items() if isinstance(v, _KEEP_TOP_LEVEL.get(k, ()))}

    # An attachment ships only when its type is on _KEEP_ATTACHMENTS, and then
    # only that type's named keys. The `attachment` key itself is meaningful
    # on attachment events alone; anywhere else it is an unknown payload.
    if event.get("type") == "attachment":
        attachment = _sanitize_attachment(event.get("attachment"))
        if attachment is None:
            return None
        out["attachment"] = attachment
    else:
        out.pop("attachment", None)

    # Command output arrives on the user and system channels only; an
    # assistant's text is the model's own words and is left as written.
    withhold = out["type"] != "assistant"
    if withhold and "content" in out:
        out["content"] = withhold_output(out["content"])

    msg = out.get("message")
    if isinstance(msg, dict):
        msg_out = {k: v for k, v in msg.items() if isinstance(v, _KEEP_MESSAGE.get(k, ()))}
        content = msg_out.get("content")
        if isinstance(content, list):
            sanitized_blocks = [_sanitize_block(b) for b in content]
            # Drop blocks that came back as None (empty thinking, etc).
            msg_out["content"] = [b for b in sanitized_blocks if b is not None]
            if withhold:
                for block in msg_out["content"]:
                    if block.get("type") == "text":
                        block["text"] = withhold_output(block["text"])
        elif withhold and isinstance(content, str):
            msg_out["content"] = withhold_output(content)
        out["message"] = msg_out
        if out["type"] == "assistant":
            # One model call (Anthropic's message id) and what it cost. Claude
            # Code writes one line per content block of a call, every line
            # carrying the same id, and a usage snapshot that only grows: a
            # reader groups by `inference_id` and keeps the LAST usage.
            inference = msg.get("id")
            if isinstance(inference, str) and inference:
                out["inference_id"] = inference[:_METADATA_MAX_LEN]
            usage = usage_counts(msg.get("usage"), {k: k for k in USAGE_KEYS})
            if usage:
                out["usage"] = usage

    return out


def usage_counts(source: Any, names: dict[str, str]) -> dict[str, int] | None:
    """`source`'s token counts under the USAGE_KEYS names, or None if it has
    none. `names` maps each of ours to the harness's own key. Only the four
    counts: service tier, per-TTL cache breakdowns, iterations, cost and
    whatever else a provider adds stay on the machine."""
    if not isinstance(source, dict):
        return None
    out: dict[str, int] = {}
    for ours in USAGE_KEYS:
        value = source.get(names.get(ours, ""))
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[ours] = value
    return out or None


def output_withheld(size: int) -> str:
    """OUTPUT_WITHHELD for `size` characters, or "" when there were none."""
    return OUTPUT_WITHHELD.format(size) if size > 0 else ""


_TASK_NOTIFICATION = re.compile(r"<task-notification>(.*?)(?:</task-notification>|\Z)", re.DOTALL)
_TASK_EVENT = re.compile(r"<event>(.*?)(?:</event>|\Z)", re.DOTALL)


def _withhold_task_events(text: str) -> str:
    """Inside a `<task-notification>`, each `<event>` body (a Monitor's output
    lines, a watcher's report) becomes its size; the rest of the notice stays.
    Only inside a notification: an `<event>` a researcher types is their own."""
    if "<task-notification>" not in text:
        return text

    def notification(block: re.Match[str]) -> str:
        inner = _TASK_EVENT.sub(
            lambda e: f"<event>{output_withheld(len(e.group(1)))}</event>", block.group(1)
        )
        closed = block.group(0).endswith("</task-notification>")
        return f"<task-notification>{inner}" + ("</task-notification>" if closed else "")

    return _TASK_NOTIFICATION.sub(notification, text)


def withhold_output(text: str) -> str:
    """`text` with the inside of every command-output tag replaced by its size.

    `<bash-stdout>(1,234 characters)</bash-stdout><bash-stderr></bash-stderr>` becomes
    `<bash-stdout>[output not sent: 1,234 chars]</bash-stdout><bash-stderr></bash-stderr>`.

    FAIL-CLOSED, because output can contain the tags themselves (a `!cat` of a
    transcript, a grep of this repo), and pairing tags naively would then ship
    the output after the first fake close tag. So everything from the FIRST
    output open tag to the LAST output close tag (to the end of the text when
    an open tag is never closed) ships as tags and size markers only: each
    open tag starts a span that only its own close tag ends, and any text
    between spans, which only a faked tag puts there, is counted into the span
    before it and never shipped. Real Claude Code lines have none. One linear
    pass over the tags: a pasted log full of them costs no more than its length.
    """
    text = _withhold_task_events(text)
    tags = list(_OUTPUT_TAG.finditer(text))
    opens = [m for m in tags if not m.group(1)]
    if not opens:
        # A lone close tag is a researcher writing about the tag, not output
        # (Claude Code never splits an output turn across blocks).
        return text
    start = opens[0].start()
    closes = [m for m in tags if m.group(1)]
    end = closes[-1].end() if closes and closes[-1].start() > opens[-1].start() else len(text)
    spans: list[list[Any]] = []  # [tag name, characters withheld]
    current: str | None = None  # the tag whose span is open
    cursor = start  # first character not yet counted
    for m in tags:
        if m.start() < start or m.end() > end:
            continue
        closing, name = m.group(1), m.group(2)
        if current is None and not closing:
            if spans:
                spans[-1][1] += m.start() - cursor
            spans.append([name, 0])
            current, cursor = name, m.end()
        elif closing and name == current:
            spans[-1][1] += m.start() - cursor
            current, cursor = None, m.end()
    spans[-1][1] += end - cursor
    withheld = "".join(f"<{name}>{output_withheld(size)}</{name}>" for name, size in spans)
    return text[:start] + withheld + text[end:]


def _is_skill_body(message: Any) -> bool:
    """True when a user message's text is a skill's injected SKILL.md."""
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    if isinstance(content, str):
        texts: list[Any] = [content]
    elif isinstance(content, list):
        texts = [b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"]
    else:
        return False
    return any(isinstance(t, str) and t.lstrip().startswith(_SKILL_BODY_PREFIX) for t in texts)


def _sanitize_attachment(attachment: Any) -> dict[str, Any] | None:
    """A kept attachment's named keys, or None to drop the event."""
    if not isinstance(attachment, dict):
        return None
    kind = attachment.get("type")
    keys = _KEEP_ATTACHMENTS.get(kind) if isinstance(kind, str) else None
    if keys is None:
        return None
    out: dict[str, Any] = {"type": kind}
    for key in keys:
        value = attachment.get(key)
        if isinstance(value, str) and value:
            out[key] = value[:_TOOL_SUMMARY_MAX_LEN]
    return out


def _summarize_tool_input(value: Any) -> str:
    """The most informative input field, capped. Empty string if no
    recognized key exists.

    `command` keeps its FULL text (multi-line — provenance for the digest
    pipeline); every other key is first-line-only as before."""
    if not isinstance(value, dict):
        return ""
    for key in _TOOL_SUMMARY_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            if key == "command":
                return candidate[:_COMMAND_MAX_LEN]
            first_line = candidate.splitlines()[0]
            return first_line[:_TOOL_SUMMARY_MAX_LEN]
    return ""


def _edit_stats(name: str, value: dict) -> dict[str, Any] | None:
    """Deterministic shape-of-the-change facts for a file-mutating tool.

    WHY THIS EXISTS. `command` already ships in full because a session's shell
    commands are its method section. Edit and Write are the same claim and got
    the opposite treatment: measured over a real 1,660-call session, Edit keeps
    7.3% of its input and Write keeps 2.2% — the file path and nothing else.
    The engine's extraction LLM is then asked for `code_change.before` and
    `after`, which it can only produce by RECALLING content the tap deleted.
    Reconstructed diffs are the worst of both worlds: they cost tokens, they
    cannot be trusted, and the real numbers were free at capture time.

    So compute the change instead of describing it. These are COUNTS, never
    content — no new bytes of the user's code leave the machine, which keeps
    this a compaction of what we already shipped rather than a widening of it.
    """
    file_path = value.get("file_path") or value.get("notebook_path")
    if not isinstance(file_path, str) or not file_path:
        return None

    def _measure(text: Any) -> tuple[int, int]:
        if not isinstance(text, str) or not text:
            return 0, 0
        return len(text), len(text.splitlines())

    if name in ("Edit", "NotebookEdit"):
        old_b, old_l = _measure(value.get("old_string") or value.get("new_source"))
        new_b, new_l = _measure(value.get("new_string") or value.get("new_source"))
        stats: dict[str, Any] = {
            "op": "edit",
            "removed_lines": old_l,
            "added_lines": new_l,
            "removed_bytes": old_b,
            "added_bytes": new_b,
        }
        if value.get("replace_all"):
            stats["replace_all"] = True
        return stats

    if name == "Write":
        by, ln = _measure(value.get("content"))
        return {"op": "write", "added_lines": ln, "added_bytes": by}

    if name == "MultiEdit":
        edits = value.get("edits")
        if isinstance(edits, list):
            old_b = sum(_measure(e.get("old_string"))[0] for e in edits if isinstance(e, dict))
            new_b = sum(_measure(e.get("new_string"))[0] for e in edits if isinstance(e, dict))
            old_l = sum(_measure(e.get("old_string"))[1] for e in edits if isinstance(e, dict))
            new_l = sum(_measure(e.get("new_string"))[1] for e in edits if isinstance(e, dict))
            return {
                "op": "edit",
                "edits": len(edits),
                "removed_lines": old_l,
                "added_lines": new_l,
                "removed_bytes": old_b,
                "added_bytes": new_b,
            }
    return None


# NOT ADDED, deliberately: a fallback digest of small scalar args for tools with
# no recognized summary key (3.1% of Claude Code calls, 257 lines of one real
# Codex session, and every MCP tool structurally). It would ship actual argument
# VALUES, and this file already made the opposite call — see
# test_tool_use_unknown_schema_has_no_summary_key: "ship with no summary at all,
# rather than us guessing and leaking a random arg". Those calls stay a bare tool
# name until someone decides to relax that policy on purpose.


def _result_size(content: Any) -> int | None:
    """Character count of a tool result, or None when there is nothing to size.

    Walks the block shapes Anthropic actually sends: a bare string, or a list
    of content blocks whose text parts carry the payload.
    """
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, str):
                total += len(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    total += len(text)
        return total
    return None


def _sanitize_block(block: Any) -> Any:
    """Per-content-block sanitization.

    text       → type + text (it's the conversation).
    thinking   → type + thinking; the signature (huge base64 model state)
                 goes, and an empty/whitespace block returns None so the
                 caller drops it.
    tool_use   → drop input, keep id+name + summary + compacted change stats.
    tool_result→ drop content, keep tool_use_id + is_error + size of the result.
    image      → media type and encoded size, never the bytes (a remote
                 http(s) URL is kept: a pointer, not content).
    other      → `{"type": <type>, "dropped": true}`. It used to be forwarded
                 unchanged "for forward-compat", which is how a new block type
                 carrying a document or a search result would have shipped.
                 The renderer has no case for an unknown block, so nothing
                 that was indexed is lost; the marker records that it was here.
    non-dict   → None (dropped; nothing renders a non-dict block).
    """
    if not isinstance(block, dict):
        return None

    btype = block.get("type")

    if btype == "text":
        text = block.get("text")
        return {"type": "text", "text": text if isinstance(text, str) else ""}

    if btype == "thinking":
        thinking_text = block.get("thinking")
        if not isinstance(thinking_text, str) or not thinking_text.strip():
            # Empty thinking blocks add zero signal but inflate payload + chunks.
            return None
        return {"type": "thinking", "thinking": thinking_text}

    if btype == "tool_use":
        name = block.get("name") or "tool"
        inp = block.get("input")
        out: dict[str, Any] = {
            "type": "tool_use",
            "id": _id_or_none(block.get("id")),
            "name": _id_or_none(block.get("name")),
        }
        summary = _summarize_tool_input(inp)
        if summary:
            out["summary"] = summary
        if isinstance(inp, dict):
            stats = _edit_stats(name, inp)
            if stats:
                out["stats"] = stats
        return out

    if btype == "tool_result":
        out = {"type": "tool_result", "tool_use_id": _id_or_none(block.get("tool_use_id"))}
        if block.get("is_error"):
            out["is_error"] = True
        # SIZE, not content. "ok" alone cannot distinguish a grep that found
        # nothing from one that found four hundred matches, and that difference
        # is most of what a result means once the body is gone. A count is not
        # a payload, so this stays a compaction rather than a new egress path.
        size = _result_size(block.get("content"))
        if size is not None:
            out["result_bytes"] = size
        return out

    if btype == "image":
        return _image_placeholder(block.get("source"))

    name = btype[:_METADATA_MAX_LEN] if isinstance(btype, str) and btype else "unknown"
    return {"type": name, "dropped": True}


def _id_or_none(value: Any) -> str | None:
    """An id or a tool name, as probe-events/1 types it: a capped string, or
    None when Claude Code wrote none (or wrote something else)."""
    return value[:_METADATA_MAX_LEN] if isinstance(value, str) else None


def _image_placeholder(source: Any) -> dict[str, Any]:
    """A pasted image as WHAT was there, never the bytes.

    The same shape codex_sanitize, pi_sanitize and kimi_sanitize already give
    a pasted image (`{type, mimeType, bytes}`, or a remote URL kept as a
    pointer). Claude Code was the one sanitizer still forwarding the base64.
    """
    if isinstance(source, dict):
        url = source.get("url")
        if (
            source.get("type") == "url"
            and isinstance(url, str)
            and url.startswith(("http://", "https://"))
            and len(url) <= _MAX_IMAGE_URL
        ):
            return {"type": "image", "source": {"type": "url", "url": url}}
        media = source.get("media_type")
        data = source.get("data")
    else:
        media = data = None
    return {
        "type": "image",
        "mimeType": media[:_METADATA_MAX_LEN] if isinstance(media, str) and media else "image",
        "bytes": len(data) if isinstance(data, str) else 0,
    }
