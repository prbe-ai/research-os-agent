"""Tests for transcript event sanitization.

Two layers:
  - sanitize_event() unit tests: per-field drop behavior, content preservation
  - build_batch_body() integration: the body is None when every line gets
    sanitized away, otherwise contains only the kept events
"""

from __future__ import annotations

import json

from tap.outbox import build_batch_body
from tap.sanitize import sanitize_event


# ---------------------------------------------------------------------------
# sanitize_event — drop full events (CC bookkeeping)
# ---------------------------------------------------------------------------


def test_drops_stop_hook_summary() -> None:
    event = {
        "type": "system",
        "subtype": "stop_hook_summary",
        "hookCount": 1,
        "uuid": "x",
    }
    assert sanitize_event(event) is None


def test_drops_turn_duration() -> None:
    event = {
        "type": "system",
        "subtype": "turn_duration",
        "durationMs": 347448,
        "messageCount": 140,
        "uuid": "y",
    }
    assert sanitize_event(event) is None


def test_keeps_unknown_system_subtypes() -> None:
    """A `system` event we don't explicitly drop passes through (after
    top-level + message-level cleanup). Defensive: don't silently drop new
    system events CC adds in the future."""
    event = {"type": "system", "subtype": "user_warning", "uuid": "z"}
    out = sanitize_event(event)
    assert out is not None
    assert out["type"] == "system"
    assert out["subtype"] == "user_warning"


# ---------------------------------------------------------------------------
# sanitize_event — top-level field stripping
# ---------------------------------------------------------------------------


def test_drops_request_id_and_meta_flags() -> None:
    event = {
        "type": "assistant",
        "uuid": "u",
        "requestId": "req_xyz",
        "isSidechain": False,
        "isMeta": False,
        "diagnostics": None,
        "timestamp": "2026-04-29T19:31:18.640Z",
    }
    out = sanitize_event(event)
    assert "requestId" not in out
    assert "isSidechain" not in out
    assert "isMeta" not in out
    assert "diagnostics" not in out
    assert out["uuid"] == "u"
    assert out["timestamp"] == "2026-04-29T19:31:18.640Z"


def test_keeps_threading_metadata() -> None:
    """uuid + parentUuid + timestamp survive — they're the only threading
    info downstream actually uses. sessionId/cwd/gitBranch/userType are
    redundant per-event (already on the doc) and now get stripped."""
    event = {
        "type": "assistant",
        "uuid": "u",
        "parentUuid": "p",
        "sessionId": "s",
        "timestamp": "2026-04-29T19:31:18.640Z",
        "userType": "external",
        "cwd": "/x",
        "gitBranch": "main",
    }
    out = sanitize_event(event)
    assert out["uuid"] == "u"
    assert out["parentUuid"] == "p"
    assert out["timestamp"] == "2026-04-29T19:31:18.640Z"
    # These are dropped — already on the doc, redundant per-event.
    assert "sessionId" not in out
    assert "userType" not in out
    assert "cwd" not in out
    assert "gitBranch" not in out


def test_drops_file_history_snapshot_event() -> None:
    """file-history-snapshot events are CC's per-turn file backup index —
    pure bookkeeping, no conversational content, ~75% of typical session
    bytes. Drop entirely."""
    event = {
        "type": "file-history-snapshot",
        "messageId": "abc",
        "snapshot": {"trackedFileBackups": {"foo.py": {"version": 1}}},
        "uuid": "u",
    }
    assert sanitize_event(event) is None


def test_drops_last_prompt_event() -> None:
    """last-prompt is CC's lookahead of what's about to be sent — duplicates
    the user message that follows."""
    event = {
        "type": "last-prompt",
        "lastPrompt": "do the thing",
        "leafUuid": "x",
        "sessionId": "y",
    }
    assert sanitize_event(event) is None


def test_drops_ai_title_event() -> None:
    """ai-title is the CC-generated session title — not currently used
    downstream."""
    event = {
        "type": "ai-title",
        "aiTitle": "Convert agent to plugin",
        "sessionId": "y",
    }
    assert sanitize_event(event) is None


def test_drops_permission_mode_event() -> None:
    """permission-mode is operational state, not content."""
    event = {
        "type": "permission-mode",
        "permissionMode": "bypassPermissions",
        "sessionId": "y",
    }
    assert sanitize_event(event) is None


def test_drops_promptid_entrypoint_version_slug() -> None:
    """Top-level CC plumbing fields that appear on every event but never
    carry retrieval signal."""
    event = {
        "type": "user",
        "uuid": "u",
        "promptId": "abc",
        "entrypoint": "cli",
        "version": "2.1.123",
        "slug": "jazzy-drifting-bee",
        "sourceToolAssistantUUID": "z",
        "timestamp": "2026-04-29T19:31:18.640Z",
    }
    out = sanitize_event(event)
    for k in ("promptId", "entrypoint", "version", "slug", "sourceToolAssistantUUID"):
        assert k not in out, f"{k} should have been dropped"
    assert out["uuid"] == "u"
    assert out["timestamp"] == "2026-04-29T19:31:18.640Z"


def test_drops_inner_message_type() -> None:
    """`message.type = "message"` is the Anthropic API shape; redundant with
    the outer event type ("assistant" / "user")."""
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
        },
    }
    out = sanitize_event(event)
    assert "type" not in out["message"]
    assert out["message"]["role"] == "assistant"
    assert out["type"] == "assistant"  # outer type survives


def test_drops_empty_thinking_block() -> None:
    """Some assistant turns emit `{"type": "thinking", "thinking": ""}` with
    no text — pure overhead."""
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": ""},
                {"type": "text", "text": "answer"},
            ],
        },
    }
    out = sanitize_event(event)
    blocks = out["message"]["content"]
    assert len(blocks) == 1
    assert blocks[0] == {"type": "text", "text": "answer"}


def test_drops_whitespace_only_thinking_block() -> None:
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "   \n\n  "},
                {"type": "text", "text": "answer"},
            ],
        },
    }
    out = sanitize_event(event)
    blocks = out["message"]["content"]
    assert len(blocks) == 1
    assert blocks[0]["type"] == "text"


def test_keeps_non_empty_thinking_block_after_signature_drop() -> None:
    """Thinking with real text + signature: keep the text, drop the signature."""
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "Real reasoning.", "signature": "BIG"},
            ],
        },
    }
    out = sanitize_event(event)
    block = out["message"]["content"][0]
    assert block["thinking"] == "Real reasoning."
    assert "signature" not in block


# ---------------------------------------------------------------------------
# sanitize_event — message-level stripping
# ---------------------------------------------------------------------------


def test_drops_usage_iterations_cache_creation() -> None:
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "usage": {"input_tokens": 1, "output_tokens": 100},
            "iterations": [{"input_tokens": 1}],
            "cache_creation": {"ephemeral_5m_input_tokens": 0},
            "service_tier": "standard",
            "inference_geo": "",
            "speed": "standard",
            "id": "msg_anthropic_internal",
            "stop_reason": "end_turn",
            "stop_sequence": None,
            "stop_details": None,
        },
    }
    out = sanitize_event(event)
    msg = out["message"]
    assert "usage" not in msg
    assert "iterations" not in msg
    assert "cache_creation" not in msg
    assert "service_tier" not in msg
    assert "inference_geo" not in msg
    assert "speed" not in msg
    assert "id" not in msg, "Anthropic's per-message id is dropped (top-level uuid is enough)"
    assert "stop_sequence" not in msg
    assert "stop_details" not in msg
    # Stop_reason is content-relevant; keep it.
    assert msg["stop_reason"] == "end_turn"
    # Content survives intact.
    assert msg["content"] == [{"type": "text", "text": "hi"}]
    assert msg["role"] == "assistant"


# ---------------------------------------------------------------------------
# sanitize_event — content block sanitization
# ---------------------------------------------------------------------------


def test_drops_thinking_signature_keeps_thinking_text() -> None:
    """The big base64 `signature` blob on thinking blocks is the single
    largest field in real CC transcripts. Dropping it shrinks payloads
    dramatically; the actual `thinking` text is preserved."""
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {
            "role": "assistant",
            "content": [
                {
                    "type": "thinking",
                    "thinking": "Let me think about this carefully.",
                    "signature": "EqwgClkIDRgC..." * 200,  # huge base64 blob
                },
                {"type": "text", "text": "Here's my answer."},
            ],
        },
    }
    out = sanitize_event(event)
    blocks = out["message"]["content"]
    assert len(blocks) == 2
    # Thinking block: text kept, signature dropped.
    assert blocks[0]["type"] == "thinking"
    assert blocks[0]["thinking"] == "Let me think about this carefully."
    assert "signature" not in blocks[0]
    # Text block untouched.
    assert blocks[1] == {"type": "text", "text": "Here's my answer."}


def test_tool_use_keeps_first_line_of_command_drops_full_input() -> None:
    """tool_use ships {type, id, name, summary} only. Full `input` is dropped —
    Bash command bodies, Edit old/new strings, etc. are too noisy to ship."""
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "tool_1",
                "name": "Bash",
                "input": {
                    "command": "git log --oneline\ngit diff HEAD~1\nrm -rf tmp",
                    "description": "inspect and clean",
                    "timeout": 30,
                },
            }],
        },
    }
    block = sanitize_event(event)["message"]["content"][0]
    assert block == {
        "type": "tool_use",
        "id": "tool_1",
        "name": "Bash",
        "summary": "git log --oneline",
    }
    assert "input" not in block


def test_tool_use_falls_back_to_file_path_for_read() -> None:
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "t", "name": "Read",
            "input": {"file_path": "/tmp/notes.md", "limit": 100, "offset": 0},
        }]},
    }
    block = sanitize_event(event)["message"]["content"][0]
    assert block["summary"] == "/tmp/notes.md"


def test_tool_use_summary_for_edit_uses_file_path_not_old_string() -> None:
    """Edit has file_path + old_string + new_string. We summarize by path,
    not by leaking the diff content."""
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "t", "name": "Edit",
            "input": {
                "file_path": "/repo/src/app.py",
                "old_string": "def foo():\n    pass\n",
                "new_string": "def foo():\n    return 42\n",
            },
        }]},
    }
    block = sanitize_event(event)["message"]["content"][0]
    assert block["summary"] == "/repo/src/app.py"


def test_tool_use_summary_for_grep_uses_pattern() -> None:
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "t", "name": "Grep",
            "input": {"pattern": "TODO|FIXME", "path": "src/"},
        }]},
    }
    block = sanitize_event(event)["message"]["content"][0]
    # `pattern` wins over `path` per the priority list, but `path` would
    # otherwise have been a valid summary too.
    assert block["summary"] == "TODO|FIXME"


def test_tool_use_summary_caps_at_max_len() -> None:
    """A multi-thousand-char single-line command gets clipped."""
    long_cmd = "echo " + "a" * 1000
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "t", "name": "Bash",
            "input": {"command": long_cmd},
        }]},
    }
    block = sanitize_event(event)["message"]["content"][0]
    assert len(block["summary"]) == 200


def test_tool_use_unknown_schema_has_no_summary_key() -> None:
    """Tools whose schema has none of the known summary keys ship with no
    summary at all, rather than us guessing and leaking a random arg."""
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {"role": "assistant", "content": [{
            "type": "tool_use", "id": "t", "name": "MysteryMcpTool",
            "input": {"some_obscure_key": "with secret value"},
        }]},
    }
    block = sanitize_event(event)["message"]["content"][0]
    assert block == {"type": "tool_use", "id": "t", "name": "MysteryMcpTool"}
    assert "summary" not in block


def test_tool_result_drops_content_keeps_only_threading() -> None:
    """tool_result `content` (file reads, command output, search results) is
    the single biggest payload chunk in real sessions. We drop it entirely."""
    big_output = "\n".join(f"line {i}" for i in range(1000))
    event = {
        "type": "user",
        "uuid": "u",
        "message": {"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "tool_1",
            "content": big_output,
            "is_error": False,
        }]},
    }
    block = sanitize_event(event)["message"]["content"][0]
    assert block == {"type": "tool_result", "tool_use_id": "tool_1"}
    assert "content" not in block
    assert "is_error" not in block, "is_error=False is the default; don't ship it"


def test_tool_result_keeps_is_error_when_truthy() -> None:
    event = {
        "type": "user",
        "uuid": "u",
        "message": {"role": "user", "content": [{
            "type": "tool_result",
            "tool_use_id": "tool_1",
            "content": "Error: file not found",
            "is_error": True,
        }]},
    }
    block = sanitize_event(event)["message"]["content"][0]
    assert block == {
        "type": "tool_result",
        "tool_use_id": "tool_1",
        "is_error": True,
    }
    assert "content" not in block


def test_user_event_with_text_content_kept_intact() -> None:
    event = {
        "type": "user",
        "uuid": "u",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "What's the weather?"}],
        },
    }
    out = sanitize_event(event)
    assert out["type"] == "user"
    assert out["message"]["content"][0]["text"] == "What's the weather?"


# ---------------------------------------------------------------------------
# sanitize_event — defensive on weird inputs
# ---------------------------------------------------------------------------


def test_non_dict_event_passes_through() -> None:
    """If a malformed line ends up here as a string (lenient JSON fallback),
    don't try to sanitize it — pass through so the caller sees raw input."""
    assert sanitize_event("not a dict") == "not a dict"
    assert sanitize_event(42) == 42
    assert sanitize_event(None) is None  # but this collides with "drop entire event" — see note below
    assert sanitize_event([1, 2, 3]) == [1, 2, 3]


def test_message_without_content_list_passes_through() -> None:
    """If `message` exists but has no list `content`, leave content alone."""
    event = {
        "type": "assistant",
        "uuid": "u",
        "message": {"role": "assistant", "content": "plain string content"},
    }
    out = sanitize_event(event)
    assert out["message"]["content"] == "plain string content"


# ---------------------------------------------------------------------------
# build_batch_body integration
# ---------------------------------------------------------------------------


def _line(d: dict) -> bytes:
    return json.dumps(d).encode("utf-8")


def test_build_batch_body_returns_none_when_all_dropped() -> None:
    """Tick that only saw bookkeeping events → no payload to ship."""
    body = build_batch_body(
        device_id="dev",
        session_id="sess",
        batch_seq=0,
        cwd="/x",
        base_line_no=0,
        lines=[
            _line({"type": "system", "subtype": "stop_hook_summary", "uuid": "1"}),
            _line({"type": "system", "subtype": "turn_duration", "uuid": "2"}),
        ],
    )
    assert body is None


def test_build_batch_body_keeps_content_drops_bookkeeping() -> None:
    body = build_batch_body(
        device_id="dev",
        session_id="sess",
        batch_seq=3,
        cwd="/x",
        base_line_no=10,
        lines=[
            _line({"type": "user", "uuid": "u1", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}),
            _line({"type": "system", "subtype": "stop_hook_summary", "uuid": "u2"}),
            _line({"type": "assistant", "uuid": "u3", "message": {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}}),
        ],
    )
    assert body is not None
    parsed = json.loads(body)
    assert parsed["session_id"] == "sess"
    assert parsed["batch_seq"] == 3
    # Only the user + assistant events survive. Bookkeeping line is gone.
    events = parsed["events"]
    assert len(events) == 2
    assert events[0]["line_no"] == 10
    assert events[0]["raw"]["type"] == "user"
    assert events[1]["line_no"] == 12
    assert events[1]["raw"]["type"] == "assistant"


def test_build_batch_body_strips_thinking_signature_and_usage() -> None:
    """End-to-end: a real-shaped assistant message with all the noise fields
    ships only the content."""
    big_signature = "Eqwg" + "A" * 5000  # mock the giant base64
    body = build_batch_body(
        device_id="dev",
        session_id="sess",
        batch_seq=0,
        cwd="/x",
        base_line_no=0,
        lines=[
            _line({
                "type": "assistant",
                "uuid": "u",
                "requestId": "req_drop_me",
                "message": {
                    "role": "assistant",
                    "id": "msg_drop_me",
                    "content": [
                        {"type": "thinking", "thinking": "reasoning text", "signature": big_signature},
                        {"type": "text", "text": "answer"},
                    ],
                    "usage": {"input_tokens": 1, "output_tokens": 100, "cache_read_input_tokens": 99999},
                    "iterations": [{"input_tokens": 1}],
                    "cache_creation": {"ephemeral_5m_input_tokens": 0},
                    "service_tier": "standard",
                    "stop_reason": "end_turn",
                },
            }),
        ],
    )
    assert body is not None
    parsed = json.loads(body)
    assert "requestId" not in parsed["events"][0]["raw"]
    msg = parsed["events"][0]["raw"]["message"]
    assert "usage" not in msg
    assert "iterations" not in msg
    assert "cache_creation" not in msg
    assert "service_tier" not in msg
    assert "id" not in msg
    assert msg["stop_reason"] == "end_turn"
    blocks = msg["content"]
    assert blocks[0]["thinking"] == "reasoning text"
    assert "signature" not in blocks[0]
    assert blocks[1] == {"type": "text", "text": "answer"}
    # Sanity: payload is much smaller than what it would have been with the signature.
    assert len(body) < 1000, f"payload should be under 1KB after stripping, got {len(body)}"


def test_build_batch_body_drops_tool_io_aggressively() -> None:
    """Realistic shape: an assistant tool_use + the user-side tool_result
    that follows. Verify the full Bash script body and the multi-KB output
    are both gone from the wire payload."""
    big_command = "for i in $(seq 1 100); do echo hello world $i; done\nls -la /tmp"
    big_output = "hello world\n" * 5000  # ~60KB of output
    body = build_batch_body(
        device_id="dev",
        session_id="sess",
        batch_seq=0,
        cwd="/x",
        base_line_no=0,
        lines=[
            _line({
                "type": "assistant",
                "uuid": "a1",
                "message": {"role": "assistant", "content": [{
                    "type": "tool_use", "id": "tool_x", "name": "Bash",
                    "input": {"command": big_command, "description": "bulk run"},
                }]},
            }),
            _line({
                "type": "user",
                "uuid": "u1",
                "message": {"role": "user", "content": [{
                    "type": "tool_result", "tool_use_id": "tool_x",
                    "content": big_output, "is_error": False,
                }]},
            }),
        ],
    )
    assert body is not None
    parsed = json.loads(body)
    # tool_use kept name + first line summary, dropped command body
    use_block = parsed["events"][0]["raw"]["message"]["content"][0]
    assert use_block["name"] == "Bash"
    assert use_block["summary"] == "for i in $(seq 1 100); do echo hello world $i; done"
    assert "input" not in use_block
    # tool_result has only threading info
    res_block = parsed["events"][1]["raw"]["message"]["content"][0]
    assert res_block == {"type": "tool_result", "tool_use_id": "tool_x"}
    # The 60KB output reduced to a few hundred bytes total.
    assert len(body) < 1000, f"expected aggressive shrink, got {len(body)} bytes"
    assert big_output not in body.decode("utf-8")
    assert big_command not in body.decode("utf-8")


if __name__ == "__main__":
    import sys
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
