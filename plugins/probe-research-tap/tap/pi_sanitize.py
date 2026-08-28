"""pi session JSONL -> Claude Code event shape.

pi (earendil-works/pi) writes one JSONL per session, entries linked by
`id`/`parentId` into a TREE. Each line is
`{"type": <variant>, "id", "parentId", "timestamp", ...}` where variant is one
of session, message, compaction, branch_summary, custom, custom_message,
label, model_change, thinking_level_change, session_info.

We translate into Claude Code's event shape — the same target codex_sanitize
hits — and stash everything pi-specific under a top-level `_pi_extras` key on
the translated event, so the engine's existing Claude Code parsing and unit
extraction work unchanged.

  session               -> CC `system` subtype=session_meta
  message.user          -> CC `user`
  message.assistant     -> CC `assistant` (text / thinking / toolCall blocks)
  message.toolResult    -> CC `user` w/ tool_result block
  message.bashExecution -> CC `assistant` tool_use + CC `user` tool_result
  compaction            -> CC `system` subtype=compaction
  branch_summary        -> CC `system` subtype=branch_summary (summary KEPT)
  custom_message        -> CC `user` (it DOES enter LLM context)
  custom                -> CC `system` subtype=custom:<customType>
  label / model_change / thinking_level_change / session_info
                        -> CC `system` w/ the fact in _pi_extras
  <anything else>       -> CC `system` subtype=unknown:<type>, NEVER dropped

WHY NOTHING IS DROPPED. codex_sanitize drops turn-boundary plumbing because
Codex's variants are a closed set we own the mapping for. pi's are not: any
extension can author a `custom` entry with an arbitrary `customType`, and a
fork's extensions will emit types this file has never seen. Dropping unknowns
would make the transcript quietly incomplete on exactly the forks we most
want to support, so unknowns pass through as system events instead.

TOOL ARGUMENTS. pi hands us `toolCall.arguments` as a full object. Shipping it
would break the policy sanitize.py sets deliberately: recognized keys get a
capped summary, unrecognized schemas get a bare tool name, argument VALUES
never leave the machine. `_summarize_arguments` enforces that here.

UNTRUSTED PRODUCER, DIFFERENT BOUNDARY. Claude Code and Codex are first-party
producers whose output shape we trust; codex_sanitize and sanitize.py can
copy a metadata field out with a bare `or ""` because that producer's JSON is
ours. pi explicitly targets forks and SDK embeds, so its JSON is not — every
metadata field copied out verbatim (`mimeType`, tool_use `id` and `name`,
`toolCallId`, `toolName`, the bash-execution synthetic call id, its `command`
text, and `customType` on both custom entry types) is type-checked with
`_safe_metadata_str` and length-capped before it leaves this module, even
though these are not `toolCall.arguments` and so not the argument-value
boundary above. This list is the class, not a sample of it — a new metadata
field copied out of an untrusted pi entry belongs on it too.

UNKNOWN CONTENT BLOCKS ARE KEPT AS A TYPE-ONLY PLACEHOLDER, NOT FORWARDED.
sanitize.py's precedent for a content block type it doesn't recognize is to
forward the block unchanged — correct there, because Claude Code's block
shapes are ours to trust. Doing the same here would let an untrusted fork's
arbitrary block (any keys, any values) ride straight through, which is an
exfiltration path this module exists to close. So an unrecognized block type
becomes `{"type": "unknown_block", "block_type": <name>}` — the fact that
something was here is kept (this file's "never drop" principle, which one
level up only promises that for top-level JSONL entries, applies to content
blocks too) but none of the unknown block's own content does.
"""

from __future__ import annotations

from typing import Any

SUPPORTED_SESSION_VERSIONS = frozenset({1, 2, 3})

_TREE_KEYS = ("id", "parentId")

# Dispatch table for non-"session" entry types, keyed by `event["type"]`.
# Populated at the bottom of each task's section as handlers are added.
_HANDLERS: dict[str, Any] = {}


def sanitize_event(event: Any) -> Any:
    """One pi JSONL entry -> one CC-shaped event. Never returns None."""
    if not isinstance(event, dict):
        return _system_event(subtype="unparsed", timestamp=None,
                             extras={"raw_type": type(event).__name__})

    entry_type = event.get("type")
    timestamp = event.get("timestamp")

    if entry_type == "session":
        return _translate_session_header(event, timestamp)

    handler = _HANDLERS.get(entry_type)
    if handler is None:
        return _system_event(
            subtype=f"unknown:{entry_type}",
            timestamp=timestamp,
            extras=_tree_extras(event) | {"raw_type": entry_type},
        )
    return handler(event, timestamp)


def _translate_session_header(event: dict, timestamp: Any) -> dict:
    version = event.get("version", 1)
    extras: dict[str, Any] = {
        "version": version,
        "cwd": event.get("cwd"),
        "session_uuid": event.get("id"),
    }
    parent = event.get("parentSession")
    if parent:
        extras["parent_session"] = parent
    if version not in SUPPORTED_SESSION_VERSIONS:
        # Flag rather than refuse: a newer pi still writes entries we can read,
        # and a silently mis-parsed session is worse than a marked one.
        extras["unsupported_version"] = True
    return _system_event(subtype="session_meta", timestamp=timestamp, extras=extras)


def _tree_extras(event: dict) -> dict[str, Any]:
    """The entry's position in the session tree, on every translated event.

    Kept on ALL events because the transcript ships the whole tree and the
    active branch is resolved downstream; without parentId the reader cannot
    tell an abandoned branch from the live one.
    """
    return {k: event[k] for k in _TREE_KEYS if event.get(k) is not None}


def _system_event(
    *, subtype: str, timestamp: Any,
    text: str | None = None, extras: dict | None = None,
) -> dict:
    out: dict[str, Any] = {"type": "system", "subtype": subtype, "timestamp": timestamp}
    if text:
        out["content"] = text
    if extras:
        out["_pi_extras"] = extras
    return out


_COMMAND_MAX_LEN = 4000
_TOOL_SUMMARY_MAX_LEN = 200
#: Cap for a single untrusted metadata field (mimeType, tool_use id,
#: toolCallId, toolName) — see the module docstring's "UNTRUSTED PRODUCER"
#: note. Not a content field, just generous enough for any real identifier.
_METADATA_MAX_LEN = 200


def _safe_metadata_str(value: Any, *, max_len: int = _METADATA_MAX_LEN) -> str:
    """A single untrusted metadata field, type-checked and length-capped.

    pi is not a trusted producer (see the module docstring): a fork can ship
    `{"mimeType": {"leaked": "..."}}` where a string is expected, and that
    nested value must not travel any further than this function. Anything
    that is not already a `str` becomes `""` rather than being coerced —
    coercing would still ship the attacker's structure, just as text.
    """
    if not isinstance(value, str):
        return ""
    return value[:max_len]


#: Argument keys worth summarizing, most informative first.
#: VERIFIED against pi 0.84.3's own typebox schemas in
#: node_modules/@earendil-works/pi-coding-agent/dist/core/tools/*.d.ts:
#:   bash  {command, timeout?}      read {path, offset?, limit?}
#:   write {path, content}          grep {pattern, path?, glob?, ...}
#:   edit  {path, edits[{oldText, newText}]}
#: pi uses `path` for every file tool — there is no filePath/file_path.
_TOOL_SUMMARY_KEYS = ("command", "path", "pattern")


def _translate_message(event: dict, timestamp: Any) -> dict:
    message = event.get("message")
    if not isinstance(message, dict):
        return _system_event(subtype="unknown:message", timestamp=timestamp,
                             extras=_tree_extras(event))

    role = message.get("role")
    handler = _MESSAGE_HANDLERS.get(role)
    if handler is None:
        return _system_event(
            subtype=f"unknown:message:{role}", timestamp=timestamp,
            extras=_tree_extras(event) | {"role": role},
        )
    return handler(event, message, timestamp)


def _translate_user_message(event: dict, message: dict, timestamp: Any) -> dict:
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {"role": "user", "content": _blocks(message.get("content"))},
        "_pi_extras": _tree_extras(event),
    }


def _translate_assistant_message(event: dict, message: dict, timestamp: Any) -> dict:
    extras = _tree_extras(event)
    for src, dst in (("provider", "provider"), ("model", "model"),
                     ("api", "api"), ("stopReason", "stop_reason")):
        if message.get(src) is not None:
            extras[dst] = message[src]
    if message.get("errorMessage"):
        extras["error_message"] = str(message["errorMessage"])[:_TOOL_SUMMARY_MAX_LEN]
    return {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": _blocks(message.get("content"))},
        "_pi_extras": extras,
    }


def _blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if not isinstance(content, list):
        return []
    out = []
    for item in content:
        block = _translate_block(item)
        if block is not None:
            out.append(block)
    return out


def _translate_block(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if kind == "text":
        return {"type": "text", "text": item.get("text") or ""}
    if kind == "thinking":
        return {"type": "thinking", "thinking": item.get("thinking") or ""}
    if kind == "image":
        # Size, mime and nothing else. See the batch-cap test.
        data = item.get("data")
        return {
            "type": "image",
            "mimeType": _safe_metadata_str(item.get("mimeType")),
            "bytes": len(data) if isinstance(data, str) else 0,
        }
    if kind == "toolCall":
        return _translate_tool_call(item)
    # An unrecognized block type from an untrusted producer. Record that
    # something was here without forwarding it — see the module docstring's
    # "UNKNOWN CONTENT BLOCKS" note for why this differs from sanitize.py's
    # forward-unchanged precedent.
    return {"type": "unknown_block", "block_type": _safe_metadata_str(kind)}


def _translate_tool_call(item: dict) -> dict:
    block: dict[str, Any] = {
        "type": "tool_use",
        "id": _safe_metadata_str(item.get("id")),
        "name": _safe_metadata_str(item.get("name")),
    }
    summary = _summarize_arguments(item.get("arguments"))
    if summary:
        block["summary"] = summary
    stats = _edit_stats(block["name"], item.get("arguments"))
    if stats:
        block["edit_stats"] = stats
    return block


def _summarize_arguments(value: Any) -> str:
    """The most informative argument, capped. Empty when nothing is recognized.

    `command` keeps its FULL multi-line text; every other key is first-line
    only. A tool whose schema matches nothing here ships as a bare name — the
    same refusal sanitize.py documents, so an unrecognized MCP or extension
    tool cannot leak a random argument value.
    """
    if not isinstance(value, dict):
        return ""
    for key in _TOOL_SUMMARY_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            if key == "command":
                return candidate[:_COMMAND_MAX_LEN]
            return candidate.splitlines()[0][:_TOOL_SUMMARY_MAX_LEN]
    return ""


def _edit_stats(name: str, arguments: Any) -> dict | None:
    """Counts for a file-mutating pi tool. Never content.

    Same reasoning as sanitize.py's version for Claude Code: the engine's
    extraction LLM is asked for code_change.before/after, and without real
    numbers it reconstructs them from memory. These counts are free at capture
    time and ship no new bytes of the user's code.
    """
    if not isinstance(arguments, dict):
        return None
    # pi names this `path` on every file tool. Verified against its schemas.
    file_path = arguments.get("path")
    if not isinstance(file_path, str) or not file_path:
        return None

    def _measure(text: Any) -> tuple[int, int]:
        if not isinstance(text, str) or not text:
            return 0, 0
        return len(text), len(text.splitlines())

    if name == "edit":
        # TWO SHAPES IN THE WILD, and the translator must read both.
        #   current (pi 0.84.3): {path, edits: [{oldText, newText}, ...]}
        #   legacy  (published sessions, Jan 2026): {path, oldText, newText}
        # The schema changed between those versions. Handling only the current
        # one silently returns None for every historical session — a backfill
        # would report success and record no edit stats at all.
        edits = arguments.get("edits")
        if not isinstance(edits, list):
            if isinstance(arguments.get("oldText"), str) or isinstance(
                arguments.get("newText"), str
            ):
                edits = [{"oldText": arguments.get("oldText"),
                          "newText": arguments.get("newText")}]
            else:
                return None
        pairs = [e for e in edits if isinstance(e, dict)]
        removed = [_measure(e.get("oldText")) for e in pairs]
        added = [_measure(e.get("newText")) for e in pairs]
        return {
            "op": "edit",
            "edits": len(pairs),
            "removed_lines": sum(ln for _b, ln in removed),
            "added_lines": sum(ln for _b, ln in added),
            "removed_bytes": sum(b for b, _ln in removed),
            "added_bytes": sum(b for b, _ln in added),
        }
    if name == "write":
        by, ln = _measure(arguments.get("content"))
        return {"op": "write", "added_lines": ln, "added_bytes": by}
    return None


def _translate_tool_result(event: dict, message: dict, timestamp: Any) -> dict:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": _safe_metadata_str(message.get("toolCallId")),
    }
    if message.get("isError"):
        block["is_error"] = True
    size = _content_size(message.get("content"))
    if size is not None:
        block["result_bytes"] = size
    extras = _tree_extras(event)
    tool_name = _safe_metadata_str(message.get("toolName"))
    if tool_name:
        extras["tool_name"] = tool_name
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {"role": "user", "content": [block]},
        "_pi_extras": extras,
    }


def _translate_bash_execution(event: dict, message: dict, timestamp: Any) -> list[dict]:
    """pi's `!command` escape -> a synthetic tool_use / tool_result PAIR.

    One pi entry carries both the command and its output, but Claude Code's
    shape puts a call on the assistant and its result on the user. Returning
    two events keeps the engine's existing pairing logic working; the outbox
    flattens the list.
    """
    # Same class as tool_use `id` in _translate_tool_call: this synthetic
    # call_id becomes the tool_use/tool_result pairing id below, so a
    # non-string entry `id` (e.g. a nested object) must not be interpolated
    # into it raw — f-string formatting would stringify the object's repr
    # straight into an output field.
    call_id = f"bash-{_safe_metadata_str(event.get('id'))}"
    extras = _tree_extras(event)
    for src, dst in (("exitCode", "exit_code"), ("cancelled", "cancelled"),
                     ("truncated", "truncated")):
        if message.get(src) is not None:
            extras[dst] = message[src]
    if message.get("excludeFromContext"):
        extras["excluded_from_context"] = True

    # Same untrusted-producer guard as the metadata fields above: a bare
    # `.get(...) or ""` here would let a non-string `command` (e.g. a nested
    # object) either ride through as a non-string "summary" or crash the
    # slice below outright. `_safe_metadata_str` types-checks it while still
    # keeping the full command text, not just an identifier-length prefix.
    command = _safe_metadata_str(message.get("command"), max_len=_COMMAND_MAX_LEN)
    use = {
        "type": "assistant",
        "timestamp": timestamp,
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": call_id, "name": "bash",
            "summary": command,
        }]},
        "_pi_extras": extras,
    }
    output = message.get("output")
    result_block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call_id}
    if isinstance(output, str):
        result_block["result_bytes"] = len(output)
    if message.get("exitCode"):
        result_block["is_error"] = True
    result = {
        "type": "user",
        "timestamp": timestamp,
        "message": {"role": "user", "content": [result_block]},
        "_pi_extras": dict(extras),
    }
    return [use, result]


def _content_size(content: Any) -> int | None:
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return None
    total = 0
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            total += len(item["text"])
    return total


_MESSAGE_HANDLERS: dict[str, Any] = {
    "user": _translate_user_message,
    "assistant": _translate_assistant_message,
    "toolResult": _translate_tool_result,
    "bashExecution": _translate_bash_execution,
}

_HANDLERS["message"] = _translate_message


def _translate_compaction(event: dict, timestamp: Any) -> dict:
    extras = _tree_extras(event)
    if event.get("tokensBefore") is not None:
        extras["tokens_before"] = event["tokensBefore"]
    if event.get("firstKeptEntryId"):
        extras["first_kept_entry_id"] = event["firstKeptEntryId"]
    if isinstance(event.get("retainedTail"), list):
        # The retained messages themselves are already in the file as their own
        # entries; record only that this compaction was self-contained.
        extras["retained_tail_count"] = len(event["retainedTail"])
    if event.get("fromHook"):
        extras["from_extension"] = True
    return _system_event(subtype="compaction", timestamp=timestamp,
                         text=event.get("summary"), extras=extras)


def _translate_branch_summary(event: dict, timestamp: Any) -> dict:
    extras = _tree_extras(event)
    if event.get("fromId"):
        extras["from_id"] = event["fromId"]
    if event.get("fromHook"):
        extras["from_extension"] = True
    return _system_event(subtype="branch_summary", timestamp=timestamp,
                         text=event.get("summary"), extras=extras)


def _translate_label(event: dict, timestamp: Any) -> dict:
    extras = _tree_extras(event)
    if event.get("targetId"):
        extras["target_id"] = event["targetId"]
    # A cleared label is `label: undefined` — absent, not empty.
    if event.get("label") is not None:
        extras["label"] = event["label"]
    return _system_event(subtype="label", timestamp=timestamp, extras=extras)


def _translate_model_change(event: dict, timestamp: Any) -> dict:
    extras = _tree_extras(event)
    for src, dst in (("provider", "provider"), ("modelId", "model_id")):
        if event.get(src):
            extras[dst] = event[src]
    return _system_event(subtype="model_change", timestamp=timestamp, extras=extras)


def _translate_thinking_level_change(event: dict, timestamp: Any) -> dict:
    extras = _tree_extras(event)
    if event.get("thinkingLevel"):
        extras["thinking_level"] = event["thinkingLevel"]
    return _system_event(subtype="thinking_level_change", timestamp=timestamp,
                         extras=extras)


def _translate_session_info(event: dict, timestamp: Any) -> dict:
    extras = _tree_extras(event)
    if event.get("name"):
        extras["name"] = event["name"]
    return _system_event(subtype="session_info", timestamp=timestamp, extras=extras)


_HANDLERS.update({
    "compaction": _translate_compaction,
    "branch_summary": _translate_branch_summary,
    "label": _translate_label,
    "model_change": _translate_model_change,
    "thinking_level_change": _translate_thinking_level_change,
    "session_info": _translate_session_info,
})


def _translate_custom_message(event: dict, timestamp: Any) -> dict:
    extras = _tree_extras(event)
    # Same untrusted-metadata class as tool_use id/toolName: customType is an
    # extension-chosen label, not content, so a nested object here must not
    # ride through into extras raw.
    custom_type = _safe_metadata_str(event.get("customType"))
    if custom_type:
        extras["custom_type"] = custom_type
    if event.get("display") is not None:
        extras["display"] = bool(event["display"])
    return {
        "type": "user",
        "timestamp": timestamp,
        "message": {"role": "user", "content": _blocks(event.get("content"))},
        "_pi_extras": extras,
    }


def _translate_custom(event: dict, timestamp: Any) -> dict:
    """Extension state. Keys only.

    `data` is whatever an extension chose to persist — arbitrary, possibly
    large, possibly sensitive, and never shown to the model. Recording the key
    names says an extension was active and what it tracked without shipping
    values this translator cannot reason about.
    """
    extras = _tree_extras(event)
    # Same guard as _translate_custom_message: customType is metadata, not
    # content, and a nested object must not reach either extras or the
    # f-string-built subtype below.
    custom_type = _safe_metadata_str(event.get("customType")) or "unknown"
    extras["custom_type"] = custom_type
    data = event.get("data")
    if isinstance(data, dict):
        extras["data_keys"] = sorted(data)
    return _system_event(subtype=f"custom:{custom_type}", timestamp=timestamp,
                         extras=extras)


_HANDLERS.update({
    "custom_message": _translate_custom_message,
    "custom": _translate_custom,
})
