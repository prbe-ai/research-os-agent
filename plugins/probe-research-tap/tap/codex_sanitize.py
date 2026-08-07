"""Translate Codex rollout JSONL lines into Anthropic-shape events.

Codex writes RolloutItems in the shape:
  {"type": <variant>, "payload": {...}, "timestamp": "..."}

where known conversation variants include session_meta, response_item,
compacted, turn_context, and event_msg. Newer clients also emit world_state,
which is internal configuration and is dropped.
We shim-translate each line into the Claude-Code transcript shape that the
existing prbe-knowledge connector understands — `{type, message, timestamp,
[…]}` — and stash allowlisted Codex-only metadata under a top-level
`_codex_extras` key. Unknown or internal payloads fail closed.

Wire format on each ingested line in the batch envelope:
  {"line_no": N, "raw": <translated event including _codex_extras>}

Translation rules:
  session_meta    → synthetic CC `system` event, subtype=session_start
                    extras: cli_version, originator, model_provider, source,
                            forked_from_id, agent_role, agent_nickname,
                            agent_path, dynamic_tools, memory_mode
  turn_context    → synthetic CC `system` event, subtype=turn_context
                    extras: full TurnContextItem (sandbox_policy, network,
                            personality, …)
  compacted       → synthetic CC `system` event, subtype=compaction
                    extras: replacement_history_count
  response_item.message     → CC `user`/`assistant`
                              with content[] of {type: text, text} blocks
                              extras: phase
                              drops: startup context / developer instruction frames
  response_item.reasoning   → CC `assistant` event with
                              {type: thinking, thinking: <flattened text>}
                              extras: structured reasoning_summary
                              drops:  encrypted_content
  response_item.function_call           → CC `assistant` w/ tool_use block
  response_item.function_call_output    → CC `user` w/ tool_result block
  response_item.local_shell_call        → CC `assistant` w/ synthetic tool_use
  response_item.custom_tool_call        → CC `assistant` w/ tool_use
  response_item.custom_tool_call_output → CC `user` w/ tool_result
  response_item.{tool_search_call,tool_search_output,
                 web_search_call,image_generation_call}
                                        → CC tool_use / tool_result analogues
  response_item.compaction              → synthetic CC `system` subtype=compaction
                                          drops: encrypted_content (large base64)
  event_msg.task_started, task_complete → drop (turn-boundary plumbing)
  event_msg.item_completed             → drop (UI mirror with raw tool output)
  event_msg.thread_settings_applied    → drop (runtime configuration)
  event_msg.token_count                 → drop (rate_limits + usage; pure noise)
  event_msg.user_message,agent_message  → drop (UI mirror of response_item.message,
                                          which is canonical)
  event_msg.<other>                     → drop (fail closed on future payloads)
  world_state / unknown top variants   → drop (may contain instructions/secrets)

`sanitize_event(event)` returns:
  - None  → drop the event entirely (turn-boundary, duplicates, pure metadata)
  - dict  → translated CC-shape event including `_codex_extras` if any
  - non-dict input → returned as-is (defensive)
"""

from __future__ import annotations

import json
from typing import Any

# Per-message phase from Codex's `MessagePhase` enum. Tracked in extras since
# CC has no equivalent.
_PHASES: frozenset[str] = frozenset({"commentary", "final_answer"})

# event_msg subtypes that we drop entirely. user_message/agent_message are
# duplicates of response_item.message; the rest are pure runtime metadata.
_DROP_EVENT_MSG_TYPES: frozenset[str] = frozenset({
    "task_started",
    "task_complete",
    "token_count",
    "user_message",
    "agent_message",
    "item_completed",
    "thread_settings_applied",
})

# SessionMetaLine fields that go into _codex_extras. id flows separately as
# session_id at the batch envelope level.
_SESSION_META_EXTRAS: tuple[str, ...] = (
    "cli_version", "originator", "model_provider", "source", "forked_from_id",
    "agent_role", "agent_nickname", "agent_path", "dynamic_tools",
    "memory_mode", "git", "timestamp",
)

# TurnContextItem fields that go into _codex_extras. cwd / model can change
# per turn — we keep them in extras to track drift.
_TURN_CONTEXT_EXTRAS: tuple[str, ...] = (
    "turn_id", "trace_id", "cwd", "current_date", "timezone",
    "approval_policy", "sandbox_policy", "permission_profile", "network",
    "file_system_sandbox_policy", "model", "personality",
    "realtime_active", "effort",
    "final_output_json_schema", "truncation_policy",
)

_STARTUP_CONTEXT_TAGS: tuple[str, ...] = (
    "permissions instructions",
    "skills_instructions",
    "plugins_instructions",
    "environment_context",
)

# Tool input summary keys ordered by "most identifying". Mirrors cc-tap.
_TOOL_SUMMARY_KEYS: tuple[str, ...] = (
    "command", "file_path", "pattern", "url", "query", "path", "description",
)

_TOOL_SUMMARY_MAX_LEN = 200


def sanitize_event(event: Any) -> Any:
    """Translate a single Codex rollout JSONL object to a CC-shape event."""
    if not isinstance(event, dict):
        return event

    rollout_type = event.get("type")
    raw_payload = event.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    timestamp = event.get("timestamp")

    if rollout_type == "session_meta":
        return _translate_session_meta(payload, timestamp)
    if rollout_type == "turn_context":
        return _translate_turn_context(payload, timestamp)
    if rollout_type == "compacted":
        return _translate_compacted(payload, timestamp)
    if rollout_type == "response_item":
        return _translate_response_item(payload, timestamp)
    if rollout_type == "event_msg":
        return _translate_event_msg(payload, timestamp)
    if rollout_type == "world_state":
        return None

    # Unknown future variants fail closed. Forwarding an unrecognized payload
    # is unsafe: Codex has introduced records containing full instructions,
    # environment state, and raw tool output in minor CLI updates.
    return None


# --- per-variant translators -----------------------------------------------


def _translate_session_meta(payload: dict, timestamp: Any) -> dict:
    extras = {k: payload[k] for k in _SESSION_META_EXTRAS if k in payload}
    return _system_event(subtype="session_start", timestamp=timestamp, extras=extras)


def _translate_turn_context(payload: dict, timestamp: Any) -> dict:
    extras = {k: payload[k] for k in _TURN_CONTEXT_EXTRAS if k in payload}
    return _system_event(subtype="turn_context", timestamp=timestamp, extras=extras)


def _translate_compacted(payload: dict, timestamp: Any) -> dict:
    out = _system_event(
        subtype="compaction",
        timestamp=timestamp,
        text=payload.get("message"),
    )
    rh = payload.get("replacement_history")
    if isinstance(rh, list):
        out["_codex_extras"] = {"replacement_history_count": len(rh)}
    return out


def _translate_event_msg(payload: dict, timestamp: Any) -> Any:
    sub = payload.get("type")
    if sub in _DROP_EVENT_MSG_TYPES:
        return None
    return None


def _translate_response_item(payload: dict, timestamp: Any) -> Any:
    inner = payload.get("type")
    if inner == "message":
        return _translate_message(payload, timestamp)
    if inner == "reasoning":
        return _translate_reasoning(payload, timestamp)
    if inner == "function_call":
        return _translate_function_call(payload, timestamp)
    if inner == "function_call_output":
        return _translate_function_call_output(payload, timestamp)
    if inner == "local_shell_call":
        return _translate_local_shell_call(payload, timestamp)
    if inner == "custom_tool_call":
        return _translate_custom_tool_call(payload, timestamp)
    if inner == "custom_tool_call_output":
        return _translate_custom_tool_call_output(payload, timestamp)
    if inner in ("tool_search_call", "web_search_call", "image_generation_call"):
        return _translate_synthetic_tool_use(payload, timestamp, name=inner)
    if inner == "tool_search_output":
        return _translate_synthetic_tool_result(payload, timestamp)
    if inner == "compaction":
        return _system_event(
            subtype="compaction",
            timestamp=timestamp,
            extras={"had_encrypted_content": "encrypted_content" in payload},
        )
    return None


def _translate_message(payload: dict, timestamp: Any) -> dict | None:
    role = payload.get("role") or "user"
    if role == "developer":
        return None
    if role == "user" and _is_startup_context_message(payload):
        return None

    cc_role = "assistant" if role == "assistant" else "user"
    blocks = [_translate_content_item(c) for c in payload.get("content") or []]
    blocks = [b for b in blocks if b is not None]
    if not blocks:
        return None

    extras: dict[str, Any] = {}
    phase = payload.get("phase")
    if phase in _PHASES:
        extras["phase"] = phase

    out: dict[str, Any] = {
        "type": cc_role,
        "timestamp": timestamp,
        "message": {"role": cc_role, "content": blocks},
    }
    if extras:
        out["_codex_extras"] = extras
    return out


def _is_startup_context_message(payload: dict) -> bool:
    texts = _content_texts(payload.get("content"))
    return bool(texts) and all(_is_startup_context_text(t) for t in texts)


def _content_texts(content: Any) -> list[str]:
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in ("input_text", "output_text"):
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            texts.append(text)
    return texts


def _is_startup_context_text(text: str) -> bool:
    stripped = text.strip().lower()
    return any(
        stripped.startswith(f"<{tag}>") and stripped.endswith(f"</{tag}>")
        for tag in _STARTUP_CONTEXT_TAGS
    )


def _translate_content_item(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    t = item.get("type")
    if t in ("input_text", "output_text"):
        text = item.get("text")
        if not isinstance(text, str) or not text:
            return None
        return {"type": "text", "text": text}
    if t == "input_image":
        url = item.get("image_url")
        if not isinstance(url, str) or not url:
            return None
        # Anthropic image block has source.url; detail is Codex-only and lost.
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _translate_reasoning(payload: dict, timestamp: Any) -> dict | None:
    text = _flatten_reasoning_content(payload.get("content"))
    summary = payload.get("summary")
    if not text.strip() and not summary:
        # Empty reasoning blocks add no signal.
        return None

    out: dict[str, Any] = {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": text}],
        },
    }
    if summary:
        out["_codex_extras"] = {"reasoning_summary": summary}
    return out


def _flatten_reasoning_content(content: Any) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict):
            text = item.get("text") or item.get("reasoning")
            if isinstance(text, str):
                parts.append(text)
    return "\n\n".join(parts)


def _translate_function_call(payload: dict, timestamp: Any) -> dict:
    call_id = payload.get("call_id") or ""
    name = payload.get("name") or ""
    summary = _summarize_args(payload.get("arguments"))
    block: dict[str, Any] = {"type": "tool_use", "id": call_id, "name": name}
    if summary:
        block["summary"] = summary
    out: dict[str, Any] = {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": [block]},
    }
    namespace = payload.get("namespace")
    if namespace:
        out["_codex_extras"] = {"namespace": namespace}
    return out


def _translate_function_call_output(payload: dict, timestamp: Any) -> dict:
    call_id = payload.get("call_id") or ""
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call_id}
    if _output_is_error(payload.get("output")):
        block["is_error"] = True
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {"role": "user", "content": [block]},
    }


def _translate_local_shell_call(payload: dict, timestamp: Any) -> dict:
    call_id = payload.get("call_id") or payload.get("id") or ""
    action = payload.get("action") or {}
    command_summary = ""
    if isinstance(action, dict):
        cmd = action.get("command")
        if isinstance(cmd, list) and cmd:
            command_summary = " ".join(str(t) for t in cmd)[:_TOOL_SUMMARY_MAX_LEN]
        elif isinstance(cmd, str):
            command_summary = cmd.splitlines()[0][:_TOOL_SUMMARY_MAX_LEN]
    block: dict[str, Any] = {"type": "tool_use", "id": call_id, "name": "local_shell"}
    if command_summary:
        block["summary"] = command_summary
    out: dict[str, Any] = {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": [block]},
    }
    extras: dict[str, Any] = {}
    if isinstance(action, dict):
        extras["action"] = action
    status = payload.get("status")
    if status is not None:
        extras["status"] = status
    if extras:
        out["_codex_extras"] = extras
    return out


def _translate_custom_tool_call(payload: dict, timestamp: Any) -> dict:
    call_id = payload.get("call_id") or ""
    name = payload.get("name") or "custom_tool"
    block: dict[str, Any] = {"type": "tool_use", "id": call_id, "name": name}
    summary = _summarize_args(payload.get("input"))
    if summary:
        block["summary"] = summary
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": [block]},
    }


def _translate_custom_tool_call_output(payload: dict, timestamp: Any) -> dict:
    call_id = payload.get("call_id") or ""
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call_id}
    if _output_is_error(payload.get("output")):
        block["is_error"] = True
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {"role": "user", "content": [block]},
    }


def _translate_synthetic_tool_use(payload: dict, timestamp: Any, *, name: str) -> dict:
    call_id = payload.get("call_id") or payload.get("id") or ""
    block: dict[str, Any] = {"type": "tool_use", "id": call_id, "name": name}
    summary = _summarize_args(payload.get("action") or payload.get("arguments"))
    if summary:
        block["summary"] = summary
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": [block]},
    }


def _translate_synthetic_tool_result(payload: dict, timestamp: Any) -> dict:
    call_id = payload.get("call_id") or ""
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call_id}
    if payload.get("status") and payload["status"] != "completed":
        block["is_error"] = True
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {"role": "user", "content": [block]},
    }


# --- helpers ---------------------------------------------------------------


def _system_event(
    *, subtype: str, timestamp: Any,
    text: str | None = None, extras: dict | None = None,
) -> dict:
    out: dict[str, Any] = {
        "type": "system",
        "subtype": subtype,
        "timestamp": timestamp,
    }
    if text:
        out["content"] = text
    if extras:
        out["_codex_extras"] = extras
    return out


def _summarize_args(value: Any) -> str:
    """First-line summary of a tool input, capped at 200 chars."""
    if isinstance(value, str):
        # function_call.arguments is a raw JSON string per Codex's protocol.
        # Try to parse and pull a known key; fall back to the first line.
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                value = parsed
        except (ValueError, TypeError):
            return value.splitlines()[0][:_TOOL_SUMMARY_MAX_LEN]
    if isinstance(value, dict):
        for key in _TOOL_SUMMARY_KEYS:
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate.splitlines()[0][:_TOOL_SUMMARY_MAX_LEN]
    return ""


def _output_is_error(output: Any) -> bool:
    """Codex's FunctionCallOutputPayload may be a string or a dict with is_error."""
    if isinstance(output, dict):
        return bool(output.get("is_error"))
    return False
