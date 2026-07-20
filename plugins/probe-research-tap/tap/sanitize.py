"""Sanitize Claude Code transcript events before shipping.

What we ship: the *conversation* — user prompts, assistant text + thinking,
plus a one-line marker for each tool call. Everything else is noise:
  - Anthropic API metadata (token-usage tallies, cache stats, request ids,
    big base64 signature blobs on thinking blocks)
  - CC-internal bookkeeping events:
      * `stop_hook_summary`, `turn_duration` (system subtypes)
      * `file-history-snapshot` (75% of payload weight; pure backup metadata)
      * `last-prompt`, `ai-title`, `permission-mode` (UI / mode plumbing)
  - Top-level fields duplicated on every event: `cwd`, `gitBranch`,
    `sessionId` (already on the doc), plus pure CC plumbing
    (`promptId`, `entrypoint`, `userType`, `version`, `slug`).
  - Empty `thinking: ""` blocks (assistant turns where the model didn't
    surface any reasoning text — the empty block carries no content).
  - Full tool_use `input` args (the entire Bash script body, the full
    old_string/new_string of an Edit, the search/replace bodies, …)
  - Full tool_result `content` (file contents, command output, search
    results — usually the single largest chunk of any session payload)

We KEEP enough of each tool block to reconstruct what happened:
  - tool_use:    type, id, name, summary  (first line of command/path/etc)
  - tool_result: type, tool_use_id, is_error (only when truthy)

`sanitize_event(event)` returns:
  - None        → drop the event entirely (CC bookkeeping with no content)
  - dict        → trimmed copy with the noise fields removed
  - input as-is → if the input isn't a dict (defensive — non-JSON lines
                  shouldn't reach here, but if they do we don't mangle them)
"""

from __future__ import annotations

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

# Top-level fields to drop from every retained event. These are duplicated
# on every event but already present once at the document level (cwd,
# gitBranch, sessionId) or pure CC plumbing that never has retrieval value
# (promptId, entrypoint, userType, version, slug, sourceToolAssistantUUID).
_DROP_TOP_LEVEL: frozenset[str] = frozenset({
    "requestId",
    "isSidechain",
    "isMeta",
    "diagnostics",
    "promptId",
    "entrypoint",
    "userType",
    "version",
    "slug",
    "sessionId",
    "cwd",
    "gitBranch",
    "sourceToolAssistantUUID",
})

# Fields inside `message` that are pure API/runtime metadata, not content.
_DROP_MESSAGE: frozenset[str] = frozenset({
    "usage",
    "iterations",
    "cache_creation",
    "service_tier",
    "inference_geo",
    "speed",
    "stop_details",
    "stop_sequence",
    "diagnostics",
    "id",    # Anthropic's per-message API id; we already keep top-level uuid
    "type",  # Inner Anthropic shape ("message"); redundant with outer event type
})

# `system` events with these subtypes have no content — drop entirely.
# stop_hook_summary  = CC's per-hook timing/output; pure bookkeeping.
# turn_duration      = how long a turn took; pure bookkeeping.
_DROP_SYSTEM_SUBTYPES: frozenset[str] = frozenset({
    "stop_hook_summary",
    "turn_duration",
})

# `thinking` blocks carry both a `thinking` text field (content — keep) and
# a `signature` field (huge base64-encoded model state — drop).
_THINKING_DROP: frozenset[str] = frozenset({"signature"})

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


def sanitize_event(event: Any) -> Any:
    """Trim a transcript event to ship only the conversation, not metadata.

    Returns None for events that should be dropped entirely.
    """
    if not isinstance(event, dict):
        return event

    # Drop entire bookkeeping event types (file-history-snapshot, last-prompt,
    # ai-title, permission-mode). These never carry conversational content.
    if event.get("type") in _DROP_EVENT_TYPES:
        return None

    # Drop CC-internal system events with no content value.
    if event.get("type") == "system":
        sub = event.get("subtype")
        if sub in _DROP_SYSTEM_SUBTYPES:
            return None

    out = {k: v for k, v in event.items() if k not in _DROP_TOP_LEVEL}

    msg = out.get("message")
    if isinstance(msg, dict):
        msg_out = {k: v for k, v in msg.items() if k not in _DROP_MESSAGE}
        content = msg_out.get("content")
        if isinstance(content, list):
            sanitized_blocks = [_sanitize_block(b) for b in content]
            # Drop blocks that came back as None (empty thinking, etc).
            msg_out["content"] = [b for b in sanitized_blocks if b is not None]
        out["message"] = msg_out

    return out


def _summarize_tool_input(value: Any) -> str:
    """First line of the most informative input field, capped. Empty string
    if no recognized key exists."""
    if not isinstance(value, dict):
        return ""
    for key in _TOOL_SUMMARY_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            first_line = candidate.splitlines()[0]
            return first_line[:_TOOL_SUMMARY_MAX_LEN]
    return ""


def _sanitize_block(block: Any) -> Any:
    """Per-content-block sanitization.

    text       → unchanged (it's the conversation).
    thinking   → drop signature; if remaining `thinking` text is empty/
                 whitespace, return None so the caller drops the block.
    tool_use   → drop input, keep id+name + one-line summary.
    tool_result→ drop content, keep tool_use_id + is_error (when truthy).
    other      → unchanged (forward-compat for new block types).
    """
    if not isinstance(block, dict):
        return block

    btype = block.get("type")

    if btype == "thinking":
        thinking_text = block.get("thinking")
        if not isinstance(thinking_text, str) or not thinking_text.strip():
            # Empty thinking blocks add zero signal but inflate payload + chunks.
            return None
        return {k: v for k, v in block.items() if k not in _THINKING_DROP}

    if btype == "tool_use":
        out: dict[str, Any] = {
            "type": "tool_use",
            "id": block.get("id"),
            "name": block.get("name"),
        }
        summary = _summarize_tool_input(block.get("input"))
        if summary:
            out["summary"] = summary
        return out

    if btype == "tool_result":
        out = {"type": "tool_result", "tool_use_id": block.get("tool_use_id")}
        if block.get("is_error"):
            out["is_error"] = True
        return out

    return block
