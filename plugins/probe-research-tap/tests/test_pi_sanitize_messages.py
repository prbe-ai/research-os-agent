# tests/test_pi_sanitize_messages.py
from tap.pi_sanitize import sanitize_event


def _entry(message, **kw):
    return {"type": "message", "id": "a1b2c3d4", "parentId": "prev1234",
            "timestamp": "2026-08-25T14:00:01.000Z", "message": message, **kw}


def test_string_user_content_becomes_a_text_block():
    out = sanitize_event(_entry({"role": "user", "content": "Hello"}))
    assert out["type"] == "user"
    assert out["message"]["content"] == [{"type": "text", "text": "Hello"}]
    assert out["_pi_extras"]["id"] == "a1b2c3d4"
    assert out["_pi_extras"]["parentId"] == "prev1234"


def test_assistant_text_and_thinking_blocks():
    out = sanitize_event(_entry({
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "considering"},
            {"type": "text", "text": "Hi!"},
        ],
        "provider": "anthropic", "model": "claude-sonnet-4-5", "stopReason": "stop",
    }))
    assert out["type"] == "assistant"
    kinds = [b["type"] for b in out["message"]["content"]]
    assert kinds == ["thinking", "text"]
    assert out["_pi_extras"]["provider"] == "anthropic"
    assert out["_pi_extras"]["model"] == "claude-sonnet-4-5"
    assert out["_pi_extras"]["stop_reason"] == "stop"


def test_image_blocks_ship_as_a_placeholder_never_base64():
    # pi inlines base64 image data. A real screenshot is megabytes and the
    # gateway caps a batch at 2MB, where an oversized body is classified
    # POISON and DROPPED — the whole tick is lost, not just the image.
    out = sanitize_event(_entry({
        "role": "user",
        "content": [{"type": "image", "data": "iVBORw0KGgo" * 5000,
                     "mimeType": "image/png"}],
    }))
    block = out["message"]["content"][0]
    assert block["type"] == "image"
    assert "data" not in block
    assert block["mimeType"] == "image/png"
    assert block["bytes"] > 0


def test_tool_call_ships_a_name_and_never_argument_values():
    out = sanitize_event(_entry({
        "role": "assistant",
        "content": [{"type": "toolCall", "id": "call_1", "name": "mystery_tool",
                     "arguments": {"secret_key": "sk-live-abc123"}}],
        "provider": "anthropic", "model": "m", "stopReason": "toolUse",
    }))
    block = out["message"]["content"][0]
    assert block == {"type": "tool_use", "id": "call_1", "name": "mystery_tool"}
    assert "sk-live-abc123" not in repr(out)


def test_image_mime_type_rejects_a_non_string():
    # pi is not a trusted producer: a fork can ship a nested object where a
    # string is expected. {"mimeType": {"leaked": "..."}} must not pass
    # through literally — it becomes "" instead of shipping the dict.
    out = sanitize_event(_entry({
        "role": "user",
        "content": [{"type": "image", "data": "abc",
                     "mimeType": {"leaked": "nested-value"}}],
    }))
    block = out["message"]["content"][0]
    assert block["mimeType"] == ""
    assert "leaked" not in repr(out)


def test_tool_call_id_rejects_a_non_string():
    out = sanitize_event(_entry({
        "role": "assistant",
        "content": [{"type": "toolCall", "id": {"leaked": "nested-value"},
                     "name": "mystery_tool", "arguments": {}}],
        "provider": "p", "model": "m", "stopReason": "toolUse",
    }))
    block = out["message"]["content"][0]
    assert block["id"] == ""
    assert "leaked" not in repr(out)


def test_metadata_fields_are_length_capped():
    out = sanitize_event(_entry({
        "role": "user",
        "content": [{"type": "image", "data": "abc",
                     "mimeType": "x" * 5000}],
    }))
    block = out["message"]["content"][0]
    assert len(block["mimeType"]) <= 200


def test_unknown_content_block_ships_as_a_type_only_placeholder():
    # A fork's custom block type must not vanish (this file's "never drop"
    # principle applies to content blocks too, not just top-level entries) —
    # but it also must not forward arbitrary content from an untrusted
    # producer. The fix: keep the type name, drop everything else.
    out = sanitize_event(_entry({
        "role": "assistant",
        "content": [{"type": "forkWidget", "widgetPayload": "sensitive-data",
                     "nested": {"secret": "sk-live-abc123"}}],
        "provider": "p", "model": "m", "stopReason": "stop",
    }))
    block = out["message"]["content"][0]
    assert block == {"type": "unknown_block", "block_type": "forkWidget"}
    assert "sensitive-data" not in repr(out)
    assert "sk-live-abc123" not in repr(out)


def test_bash_tool_call_keeps_its_full_command():
    # `command` is the one argument shipped in full: a session's shell commands
    # are its method section. Same rule as sanitize.py's _TOOL_SUMMARY_KEYS.
    out = sanitize_event(_entry({
        "role": "assistant",
        "content": [{"type": "toolCall", "id": "c2", "name": "bash",
                     "arguments": {"command": "pytest -q\nmake check"}}],
        "provider": "p", "model": "m", "stopReason": "toolUse",
    }))
    assert out["message"]["content"][0]["summary"] == "pytest -q\nmake check"
