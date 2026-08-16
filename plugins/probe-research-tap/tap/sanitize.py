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
  - Full tool_use `input` args EXCEPT Bash's `command` (the full
    old_string/new_string of an Edit, the search/replace bodies, …)
  - Full tool_result `content` (file contents, command output, search
    results — usually the single largest chunk of any session payload)

We KEEP enough of each tool block to reconstruct what happened:
  - tool_use:    type, id, name, summary — the FULL command for Bash
                 (shell lines are a session's method section; capped at
                 _COMMAND_MAX_LEN), first line of path/pattern/etc for the rest
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

# `attachment` events are harness plumbing that CC injects around the
# conversation, and they were shipping in full: 12,217 events and 11.5 MB across
# ten measured sessions, none of which produce a single character of indexed
# text (the renderer has no case for them). Same class as file-history-snapshot,
# and missed for the same reason — it does not look like a payload until you
# weigh it.
#
# Denylist rather than allowlist, so an attachment type we have not seen still
# ships. Share of those 11.5 MB in brackets.
_DROP_ATTACHMENT_TYPES: frozenset[str] = frozenset({
    "hook_success",             # 54.7% — hook stdout/stderr/exitCode per fire
    "hook_non_blocking_error",  # 10.9%
    "hook_cancelled",
    "hook_system_message",
    "hook_additional_context",  #  1.4% — the same injected preamble every session
    "task_reminder",            #  6.2%
    "queued_command",           #  4.0%
    "deferred_tools_delta",     #  4.0% — tool-registry dumps
    "skill_listing",            #  3.4% — identical across every session
    "invoked_skills",           #  2.4%
    "agent_listing_delta",      #  0.8%
    "mcp_instructions_delta",   #  0.7%
    "command_permissions",
    "date_change",
    "ultra_effort_enter",
    "plan_mode_exit",
    "dynamic_skill",
})

# KEPT on purpose: `edited_text_file` and `file` carry real file content, and
# `nested_memory` / `compact_file_reference` carry project context a session
# genuinely referred to. Together they are ~11% of attachment bytes.

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

# `command` is the exception to first-line-only (decided 2026-08-13, session
# digests review): a data-processing session's method IS its shell commands,
# and heredoc/inline-script bodies were exactly what first-line summaries
# dropped. The full command ships — outputs still never do — under its own,
# larger cap so a pathological one-liner can't bloat a batch.
_COMMAND_MAX_LEN = 4000


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

    # Drop harness-plumbing attachments (hook output, tool/skill registry
    # dumps, reminders). See _DROP_ATTACHMENT_TYPES.
    if event.get("type") == "attachment":
        attachment = event.get("attachment")
        if isinstance(attachment, dict):
            if attachment.get("type") in _DROP_ATTACHMENT_TYPES:
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

    text       → unchanged (it's the conversation).
    thinking   → drop signature; if remaining `thinking` text is empty/
                 whitespace, return None so the caller drops the block.
    tool_use   → drop input, keep id+name + summary + compacted change stats.
    tool_result→ drop content, keep tool_use_id + is_error + size of the result.
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
        name = block.get("name") or "tool"
        inp = block.get("input")
        out: dict[str, Any] = {
            "type": "tool_use",
            "id": block.get("id"),
            "name": block.get("name"),
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
        out = {"type": "tool_result", "tool_use_id": block.get("tool_use_id")}
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

    return block
