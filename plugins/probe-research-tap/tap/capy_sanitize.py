"""Capy session mirror JSONL -> Claude Code event shape.

Capy (capy.ai) runs its agent on its own servers, so no transcript ever exists
on this machine. `probe capy sync` (cli/capy_discovery.py) reads a thread
through Capy's public API and writes it to a local MIRROR file, one per thread
and one per subagent, which this module translates exactly like the other
harnesses' transcripts. The mirror's lines are Probe's own format:

  {"type": "capy_session", "version": 1, "id": <session uuid>, "thread_id",
   "subagent_id", "subagent_path", "project_id", "cwd", "created_at"}
      the first line of every mirror: who this file is (session_identity reads
      it). -> CC `system` subtype=session_meta.
  {"type": "capy_message", "id", "source": "user"|"assistant"|"tool", "text",
   "tool", "calls", "edit_of", "model", "attachments", "created_at"}
      one Capy transcript entry, in Capy's own order:
        user      -> CC `user` (an edit of an earlier prompt is labelled)
        assistant -> CC `assistant` text, `inference_id` = the message id
        tool      -> CC `assistant` with one `tool_use` block
  <anything else> -> CC `system` subtype=unknown:<type>, never dropped

WHAT CAPY GIVES US IS ALREADY A SUMMARY. Capy's public transcript carries the
researcher's prompts, the agent's replies and a ONE-LINE summary per tool
call ("Running the test suite"): never tool arguments, tool output or the
model's reasoning. So a tool entry ships as a `tool_use` block whose `summary`
is that line (first line, capped like every other sanitizer's), and there is
no `tool_result` to send.

EDITS. Capy lets the researcher edit an earlier prompt: the edit arrives as a
NEW message naming the one it replaces (`editOf`). The mirror is append-only,
so the edit is a new user turn whose text starts with EDIT_LABEL; the engine
keeps both, and a reader can tell which came later.

ATTACHMENTS. Names, media types and sizes only: an image (or audio, video)
becomes the shared media block, any other file a one-line text marker. Capy's
file ids and upload paths never leave the machine, and the bytes are never
fetched.

UNTRUSTED PRODUCER. The mirror is written by Probe, but every value in it came
from Capy's API. Like pi_sanitize, every metadata field copied out is
type-checked and length-capped; conversation text must be a string or ships
empty.
"""

from __future__ import annotations

import math
from typing import Any

#: The mirror line types this module reads (cli/capy_discovery.py writes them).
HEADER_TYPE = "capy_session"
MESSAGE_TYPE = "capy_message"
#: Mirror format versions this module reads.
SUPPORTED_VERSIONS = frozenset({1})

#: The label an edited prompt opens with.
EDIT_LABEL = "[edited prompt, replaces an earlier one] "

_METADATA_MAX_LEN = 200
_TIMESTAMP_MAX_LEN = 64
_TOOL_SUMMARY_MAX_LEN = 200
_MEDIA_KINDS = ("image", "audio", "video")


def _opt_str(value: Any, max_len: int = _METADATA_MAX_LEN) -> str | None:
    if isinstance(value, str) and value:
        return value[:max_len]
    return None


def _text(value: Any) -> str:
    """Conversation text: uncapped like every sanitizer's, but a string or nothing."""
    return value if isinstance(value, str) else ""


def _size(value: Any) -> int:
    """A byte count: a finite non-negative number, else 0 (Capy's API may
    send "Infinity" or "NaN" as strings; those are not counts)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(value):
        return 0
    return max(0, int(value))


def _system_event(*, subtype: str, timestamp: Any) -> dict:
    return {"type": "system", "subtype": subtype, "timestamp": timestamp}


def sanitize_event(event: Any) -> Any:
    """One mirror line -> one CC-shaped event. Never returns None."""
    if not isinstance(event, dict):
        return _system_event(subtype="unparsed", timestamp=None)
    kind = event.get("type")
    timestamp = _opt_str(event.get("created_at"), _TIMESTAMP_MAX_LEN)
    if kind == HEADER_TYPE:
        return _translate_header(event, timestamp)
    if kind == MESSAGE_TYPE:
        source = event.get("source")
        handler = _MESSAGE_HANDLERS.get(source) if isinstance(source, str) else None
        if handler is not None:
            return handler(event, timestamp)
        return _system_event(
            subtype=f"unknown:message:{_opt_str(source) or ''}", timestamp=timestamp
        )
    return _system_event(subtype=f"unknown:{_opt_str(kind) or ''}", timestamp=timestamp)


def _translate_header(event: dict, timestamp: Any) -> dict:
    subtype = "session_meta"
    version = event.get("version")
    # An unhashable version (a list, an object) cannot be looked up: not an int is unsupported.
    if isinstance(version, bool) or not isinstance(version, int) or version not in SUPPORTED_VERSIONS:
        # Flag rather than refuse: a newer mirror still has lines we can read.
        subtype = "session_meta:unsupported_version"
    return _system_event(subtype=subtype, timestamp=timestamp)


def _attachment_blocks(value: Any) -> list[dict]:
    if not isinstance(value, list):
        return []
    blocks: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        media_type = _opt_str(item.get("media_type")) or "application/octet-stream"
        size = _size(item.get("size_bytes"))
        if media_type.split("/", 1)[0] in _MEDIA_KINDS:
            blocks.append(
                {"type": media_type.split("/", 1)[0], "mimeType": media_type, "bytes": size}
            )
            continue
        name = _opt_str(item.get("name")) or "file"
        blocks.append({"type": "text", "text": f"[attached file: {name}, {media_type}, {size} bytes]"})
    return blocks


def _translate_user(event: dict, timestamp: Any) -> dict:
    text = _text(event.get("text"))
    if _opt_str(event.get("edit_of")):
        text = EDIT_LABEL + text
    content: list[dict] = [{"type": "text", "text": text}] if text else []
    content.extend(_attachment_blocks(event.get("attachments")))
    out: dict[str, Any] = {
        "type": "user",
        "timestamp": timestamp,
        "message": {"role": "user", "content": content},
    }
    if message_id := _opt_str(event.get("id")):
        out["uuid"] = message_id
    return out


def _translate_assistant(event: dict, timestamp: Any) -> dict:
    content: list[dict] = [{"type": "text", "text": _text(event.get("text"))}]
    content.extend(_attachment_blocks(event.get("attachments")))
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if model := _opt_str(event.get("model")):
        message["model"] = model
    out: dict[str, Any] = {"type": "assistant", "timestamp": timestamp, "message": message}
    # Capy delivers one reply per message, so the message id names the inference.
    if message_id := _opt_str(event.get("id")):
        out["inference_id"] = message_id
    return out


def _tool_name(event: dict) -> str | None:
    name = _opt_str(event.get("tool"))
    if name:
        return name
    calls = event.get("calls")
    if isinstance(calls, list):
        for call in calls:
            if isinstance(call, dict) and (name := _opt_str(call.get("tool"))):
                return name
    return None


def _translate_tool(event: dict, timestamp: Any) -> dict:
    block: dict[str, Any] = {
        "type": "tool_use",
        "id": _opt_str(event.get("id")),
        "name": _tool_name(event),
    }
    lines = _text(event.get("text")).strip().splitlines()
    if lines and lines[0].strip():
        block["summary"] = lines[0].strip()[:_TOOL_SUMMARY_MAX_LEN]
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": [block]},
    }


_MESSAGE_HANDLERS = {
    "user": _translate_user,
    "assistant": _translate_assistant,
    "tool": _translate_tool,
}
