"""Kimi Code `wire.jsonl` records -> Claude Code event shape.

Kimi Code (2.1.x) writes one `wire.jsonl` per agent:
`sessions/<workDirKey>/session_<uuid>/agents/main/wire.jsonl` for the session
and `agents/agent-N/wire.jsonl` per subagent. Only the main wire is captured
(the registry row's `transcripts.pattern`); a subagent's own steps stay out,
like Claude Code's sidechains, and the main wire still holds the Agent tool's
call and result.

ONE OF TWO RECORD FAMILIES, SO NOTHING IS DOUBLED. Each wire carries the
conversation twice:

  * the engine records `context.append_message` (user-role messages, with
    their `origin`) and `context.append_loop_event` (`content.part` text and
    think, `tool.call`, `tool.result`, `step.*`), written as they happen;
  * the UI records (`"kind": "event"`): `agent.message.appended` holds whole
    messages, but an assistant's messages are only written when its TURN ends.

This module reads the engine family, which is also what Kimi's own context
recovery note tells its model to read ("The conversation is in
`context.append_message` (user prompts) and `context.append_loop_event`").
A turn cut short (Ctrl-C, a crash) keeps every part written so far, and every
user-role message carries its `origin`. The UI family is dropped whole.

Translation:

  metadata                          -> CC `system` subtype=session_meta
                                       extras: protocol_version, created_at
  context.append_message (role user)
    origin.kind user                -> CC `user` (a researcher prompt)
    origin.kind plugin_command      -> CC `user`: the slash line the researcher
                                       typed (`/plugin:cmd args`), never the
                                       expanded command body
    origin.kind shell_command input -> CC `user` `<bash-input>` text, as Claude
                                       Code records its own `!` shell mode
    origin.kind shell_command output-> CC `system` subtype=shell_output, the
                                       output's SIZE only
    origin.kind skill_activation    -> CC `system` subtype=skill_activation,
                                       the skill's name and trigger only
    origin.kind injection, hook_result, system_trigger, compaction_summary,
    task, cron_job, cron_missed, retry, other, <unknown>
                                    -> dropped: NOT researcher prompts (system
                                       reminders, hook output, stop-hook and
                                       subagent triggers, skill bodies)
  context.append_loop_event
    content.part text               -> CC `assistant` text block
    content.part think              -> CC `assistant` thinking block
                                       (empty dropped; `encrypted` dropped)
    tool.call                       -> CC `assistant` tool_use (summary + stats)
    tool.result                     -> CC `user` tool_result (size, is_error)
    step.* / <other>                -> dropped
  context.apply_compaction          -> CC `system` subtype=compaction (summary)
  context.clear                     -> CC `system` subtype=clear
  context.undo                      -> CC `system` subtype=undo (count)
  everything else                   -> dropped (fail closed): llm.request,
                                       usage, profile.bind (the system prompt),
                                       tools snapshots, permission and
                                       interaction records, subagent.*, the UI
                                       family, and any record type Kimi adds

TOOL ARGUMENTS follow sanitize.py's policy: a recognized key becomes a capped
summary (the full shell `command`), file-mutating tools get counts, never
content, and an unrecognized schema ships as a bare tool name. Tool OUTPUT
never ships, only its size.

A record that is not a JSON object is dropped, not passed through: unlike
Claude Code's, Kimi's wire carries records written for plugins and hooks.

REDACTION IS NOT DONE HERE (see sanitize.py): `Journal.stage` runs
`secrets.redact_event` on this output.

CANONICAL: src/probe/tap_core/kimi_sanitize.py — the copy under
plugins/probe-research-tap/tap/ is vendored by `make sync-tap-core` and must
stay byte-identical (tests/test_tap_core_sync.py guards it). Edit the
canonical file, never the plugin copy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .sanitize import COMMAND_MAX_LEN

#: Single definition in sanitize.py -- see COMMAND_MAX_LEN there.
_COMMAND_MAX_LEN = COMMAND_MAX_LEN
_TOOL_SUMMARY_MAX_LEN = 200
#: Cap for one metadata string copied out of a record (ids, names, kinds).
_METADATA_MAX_LEN = 200
#: A URL is a reference and costs nothing to keep; longer is not a URL.
_MAX_MEDIA_URL = 2048

#: Argument keys worth summarizing, most identifying first. Kimi Code 2.1.1's
#: tools: Bash {command}, Write {path, content, mode}, Edit {path, old_string,
#: new_string, replace_all}, Read {path}, Grep {pattern, path}, Glob {pattern},
#: FetchURL {url}, WebSearch {query}, Agent {description, prompt}.
_TOOL_SUMMARY_KEYS: tuple[str, ...] = (
    "command", "path", "file_path", "pattern", "url", "query", "description",
)

#: User-role origins that are the researcher's own words.
_PROMPT_ORIGINS = frozenset({"user"})

_HANDLERS: dict[str, Any] = {}


def sanitize_event(event: Any) -> Any:
    """One Kimi wire record -> a CC-shaped event, or None to drop it."""
    if not isinstance(event, dict):
        return None
    handler = _HANDLERS.get(event.get("type"))
    if handler is None:
        return None
    return handler(event)


# --- helpers ----------------------------------------------------------------


def _str(value: Any, max_len: int = _METADATA_MAX_LEN) -> str:
    """A metadata string, type-checked and capped; anything else is ""."""
    return value[:max_len] if isinstance(value, str) else ""


def _timestamp(value: Any) -> str | None:
    """Kimi's `time` (epoch milliseconds) as the ISO-8601 UTC string the
    Claude Code shape carries."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        moment = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _system_event(
    *, subtype: str, timestamp: Any, text: str | None = None, extras: dict | None = None,
) -> dict:
    out: dict[str, Any] = {"type": "system", "subtype": subtype, "timestamp": timestamp}
    if text:
        out["content"] = text
    if extras:
        out["_kimi_extras"] = extras
    return out


def _message_event(role: str, timestamp: Any, blocks: list, extras: dict | None = None) -> dict:
    out: dict[str, Any] = {
        "type": role,
        "timestamp": timestamp,
        "message": {"role": role, "content": blocks},
    }
    if extras:
        out["_kimi_extras"] = extras
    return out


def _media_placeholder(kind: str, holder: Any) -> dict:
    """An image/audio/video part that records WHAT was there, never the bytes.

    Same rule as codex_sanitize's pasted images: a remote `https://` reference
    is kept, an inline `data:` payload or Kimi's `blobref:` (media offloaded to
    `agents/<id>/blobs/`) becomes its type and length.
    """
    url = holder.get("url") if isinstance(holder, dict) else None
    url = url if isinstance(url, str) else ""
    if url.startswith(("http://", "https://")) and len(url) <= _MAX_MEDIA_URL:
        return {"type": kind, "source": {"type": "url", "url": url}}
    media = kind
    if url.startswith("data:"):
        head = url[5 : url.find(",")] if "," in url else url[5:]
        candidate = head.split(";")[0].strip()
        if candidate and len(candidate) <= 128:
            media = candidate
    return {"type": kind, "mimeType": media, "bytes": len(url)}


def _translate_part(part: Any) -> dict | None:
    """One Kimi ContentPart -> one CC content block (None drops it)."""
    if not isinstance(part, dict):
        return None
    kind = part.get("type")
    if kind == "text":
        text = part.get("text")
        return {"type": "text", "text": text} if isinstance(text, str) and text else None
    if kind == "think":
        think = part.get("think")
        if not isinstance(think, str) or not think.strip():
            return None
        return {"type": "thinking", "thinking": think}
    if kind == "image_url":
        return _media_placeholder("image", part.get("imageUrl"))
    if kind == "audio_url":
        return _media_placeholder("audio", part.get("audioUrl"))
    if kind == "video_url":
        return _media_placeholder("video", part.get("videoUrl"))
    # A part type this module has never seen: say something was here, ship
    # none of it.
    return {"type": "unknown_block", "block_type": _str(kind)}


def _blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return []
    return [b for b in (_translate_part(p) for p in content) if b is not None]


def _text_of(content: Any) -> str:
    if not isinstance(content, list):
        return content if isinstance(content, str) else ""
    return "\n".join(
        p["text"] for p in content
        if isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str)
    )


def _summarize_arguments(value: Any) -> str:
    """The most informative argument, capped. Empty when nothing is recognized:
    an unrecognized schema ships as a bare tool name (sanitize.py's policy)."""
    if not isinstance(value, dict):
        return ""
    for key in _TOOL_SUMMARY_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            if key == "command":
                return candidate[:_COMMAND_MAX_LEN]
            return candidate.splitlines()[0][:_TOOL_SUMMARY_MAX_LEN]
    return ""


def _measure(text: Any) -> tuple[int, int]:
    if not isinstance(text, str) or not text:
        return 0, 0
    return len(text), len(text.splitlines())


def _edit_stats(name: str, arguments: Any) -> dict | None:
    """Counts for Kimi's file-mutating tools (Write, Edit). Never content.

    Same reasoning as sanitize.py's `_edit_stats`: the engine is asked what
    changed, and these numbers are free at capture time.
    """
    if not isinstance(arguments, dict):
        return None
    path = arguments.get("path") or arguments.get("file_path")
    if not isinstance(path, str) or not path:
        return None
    if name == "Edit":
        old_b, old_l = _measure(arguments.get("old_string"))
        new_b, new_l = _measure(arguments.get("new_string"))
        stats: dict[str, Any] = {
            "op": "edit",
            "removed_lines": old_l,
            "added_lines": new_l,
            "removed_bytes": old_b,
            "added_bytes": new_b,
        }
        if arguments.get("replace_all") is True:
            stats["replace_all"] = True
        return stats
    if name == "Write":
        by, ln = _measure(arguments.get("content"))
        stats = {"op": "write", "added_lines": ln, "added_bytes": by}
        mode = arguments.get("mode")
        if isinstance(mode, str) and mode:
            stats["mode"] = _str(mode, 32)
        return stats
    return None


def _output_size(output: Any) -> int | None:
    """Character count of a tool result's output, content never included."""
    if isinstance(output, str):
        return len(output)
    if isinstance(output, list):
        return len(_text_of(output))
    return None


# --- record handlers ----------------------------------------------------------


def _translate_metadata(event: dict) -> dict:
    extras: dict[str, Any] = {}
    version = event.get("protocol_version")
    if isinstance(version, str):
        extras["protocol_version"] = _str(version, 32)
    created = event.get("created_at")
    if isinstance(created, (int, float)) and not isinstance(created, bool):
        extras["created_at"] = created
    return _system_event(
        subtype="session_meta", timestamp=_timestamp(created), extras=extras,
    )


def _translate_append_message(event: dict) -> dict | None:
    message = event.get("message")
    if not isinstance(message, dict):
        return None
    timestamp = _timestamp(event.get("time"))
    role = message.get("role")
    if role == "assistant":
        # Not written by 2.1.1 (assistant text arrives as loop events), but a
        # whole assistant message here is still the model's words.
        blocks = _blocks(message.get("content"))
        return _message_event("assistant", timestamp, blocks) if blocks else None
    if role != "user":
        return None
    origin = message.get("origin") if isinstance(message.get("origin"), dict) else {}
    kind = origin.get("kind")
    if kind in _PROMPT_ORIGINS:
        blocks = _blocks(message.get("content"))
        if not blocks:
            return None
        extras = {"prompt_id": _str(message.get("id"))} if message.get("id") else None
        return _message_event("user", timestamp, blocks, extras)
    if kind == "plugin_command":
        plugin, command = _str(origin.get("pluginId")), _str(origin.get("commandName"))
        if not (plugin and command):
            return None
        line = f"/{plugin}:{command}"
        args = origin.get("commandArgs")
        if isinstance(args, str) and args.strip():
            line += " " + args[:_COMMAND_MAX_LEN]
        return _message_event(
            "user", timestamp, [{"type": "text", "text": line}], {"origin": "plugin_command"},
        )
    if kind == "shell_command":
        if origin.get("phase") == "input":
            text = _text_of(message.get("content"))[:_COMMAND_MAX_LEN]
            if not text:
                return None
            return _message_event(
                "user", timestamp, [{"type": "text", "text": text}], {"origin": "shell_command"},
            )
        extras: dict[str, Any] = {"result_bytes": len(_text_of(message.get("content")))}
        if origin.get("isError") is True:
            extras["is_error"] = True
        return _system_event(subtype="shell_output", timestamp=timestamp, extras=extras)
    if kind == "skill_activation":
        extras = {
            key: _str(origin.get(src))
            for src, key in (("skillName", "skill_name"), ("trigger", "trigger"))
            if isinstance(origin.get(src), str) and origin.get(src)
        }
        return _system_event(subtype="skill_activation", timestamp=timestamp, extras=extras)
    # injection (system reminders), hook_result (hook output), system_trigger
    # (stop-hook continuations, a subagent's prompt), compaction_summary,
    # task/cron prompts, retries and anything newer: not the researcher's words.
    return None


def _translate_loop_event(event: dict) -> dict | None:
    loop = event.get("event")
    if not isinstance(loop, dict):
        return None
    timestamp = _timestamp(event.get("time"))
    kind = loop.get("type")
    extras: dict[str, Any] = {}
    turn_id = loop.get("turnId")
    if isinstance(turn_id, (str, int)) and not isinstance(turn_id, bool):
        extras["turn_id"] = _str(str(turn_id), 64)
    step = loop.get("step")
    if isinstance(step, int) and not isinstance(step, bool):
        extras["step"] = step
    if kind == "content.part":
        block = _translate_part(loop.get("part"))
        return _message_event("assistant", timestamp, [block], extras) if block else None
    if kind == "tool.call":
        name = _str(loop.get("name"))
        args = loop.get("args")
        block: dict[str, Any] = {
            "type": "tool_use",
            "id": _str(loop.get("toolCallId")),
            "name": name,
        }
        summary = _summarize_arguments(args)
        if summary:
            block["summary"] = summary
        stats = _edit_stats(name, args)
        if stats:
            block["stats"] = stats
        return _message_event("assistant", timestamp, [block], extras)
    if kind == "tool.result":
        result = loop.get("result") if isinstance(loop.get("result"), dict) else {}
        block = {"type": "tool_result", "tool_use_id": _str(loop.get("toolCallId"))}
        if result.get("isError") is True:
            block["is_error"] = True
        size = _output_size(result.get("output"))
        if size is not None:
            block["result_bytes"] = size
        return _message_event("user", timestamp, [block], extras)
    return None


def _translate_compaction(event: dict) -> dict:
    extras: dict[str, Any] = {}
    for src, dst in (
        ("compactedCount", "compacted_count"),
        ("tokensBefore", "tokens_before"),
        ("tokensAfter", "tokens_after"),
        ("keptUserMessageCount", "kept_user_message_count"),
        ("droppedCount", "dropped_count"),
    ):
        value = event.get(src)
        if isinstance(value, int) and not isinstance(value, bool):
            extras[dst] = value
    summary = event.get("summary")
    return _system_event(
        subtype="compaction",
        timestamp=_timestamp(event.get("time")),
        text=summary if isinstance(summary, str) else None,
        extras=extras,
    )


def _translate_clear(event: dict) -> dict:
    return _system_event(subtype="clear", timestamp=_timestamp(event.get("time")))


def _translate_undo(event: dict) -> dict:
    count = event.get("count")
    extras = {"count": count} if isinstance(count, int) and not isinstance(count, bool) else None
    return _system_event(subtype="undo", timestamp=_timestamp(event.get("time")), extras=extras)


_HANDLERS.update({
    "metadata": _translate_metadata,
    "context.append_message": _translate_append_message,
    "context.append_loop_event": _translate_loop_event,
    "context.apply_compaction": _translate_compaction,
    "context.clear": _translate_clear,
    "context.undo": _translate_undo,
})
