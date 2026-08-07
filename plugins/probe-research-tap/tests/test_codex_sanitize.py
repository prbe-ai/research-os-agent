"""Tests for Codex rollout sanitization.

The fixture `tests/fixtures/rollout-sample.jsonl` is a real captured Codex
session (gpt-5.5, "Say hello in five words. Do not call any tools.") from
2026-05-01. Tests assert the shim translation preserves the conversation and
stashes Codex-only fields under `_codex_extras`.

Three layers:
  - per-variant unit tests (synthetic payloads): drop rules, field mapping
  - fixture-driven integration test: the canonical assistant reply survives
    the round-trip into Anthropic shape
  - build_batch_body() integration: full envelope round-trips and is None
    when every line is dropped
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tap.codex_sanitize import sanitize_event
from tap.outbox import build_batch_body

FIXTURE = Path(__file__).parent / "fixtures" / "rollout-sample.jsonl"


@pytest.fixture(autouse=True)
def _codex_source(monkeypatch):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "codex")


# ---- per-variant unit tests ----------------------------------------------


def test_session_meta_becomes_system_event_with_extras():
    raw = {
        "type": "session_meta",
        "timestamp": "2026-05-01T00:23:01Z",
        "payload": {
            "id": "019d-...",
            "cli_version": "0.128.0",
            "originator": "codex_cli",
            "model_provider": "openai",
            "source": "startup",
            "base_instructions": {"text": "..."},
        },
    }
    out = sanitize_event(raw)
    assert out["type"] == "system"
    assert out["subtype"] == "session_start"
    assert out["timestamp"] == "2026-05-01T00:23:01Z"
    extras = out["_codex_extras"]
    assert extras["cli_version"] == "0.128.0"
    assert extras["originator"] == "codex_cli"
    assert extras["source"] == "startup"
    assert "base_instructions" not in extras
    # session_id flows separately at envelope level — should NOT be in extras.
    assert "id" not in extras


def test_turn_context_becomes_system_event_carrying_full_config():
    raw = {
        "type": "turn_context",
        "timestamp": "2026-05-01T00:23:02Z",
        "payload": {
            "turn_id": "t1",
            "cwd": "/repo",
            "model": "gpt-5.5",
            "approval_policy": "never",
            "sandbox_policy": "read-only",
            "personality": "default",
            "developer_instructions": "be concise",
            "user_instructions": "project instructions",
        },
    }
    out = sanitize_event(raw)
    assert out["type"] == "system"
    assert out["subtype"] == "turn_context"
    extras = out["_codex_extras"]
    assert extras["sandbox_policy"] == "read-only"
    assert extras["model"] == "gpt-5.5"
    assert "developer_instructions" not in extras
    assert "user_instructions" not in extras


def test_response_message_assistant_text_round_trips():
    raw = {
        "type": "response_item",
        "timestamp": "t",
        "payload": {
            "type": "message",
            "role": "assistant",
            "phase": "final_answer",
            "content": [{"type": "output_text", "text": "Hello, hope you are well."}],
        },
    }
    out = sanitize_event(raw)
    assert out["type"] == "assistant"
    assert out["message"]["role"] == "assistant"
    assert out["message"]["content"] == [
        {"type": "text", "text": "Hello, hope you are well."}
    ]
    assert out["_codex_extras"] == {"phase": "final_answer"}


def test_response_message_user_no_phase_no_extras():
    raw = {
        "type": "response_item",
        "timestamp": "t",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
        },
    }
    out = sanitize_event(raw)
    assert out["type"] == "user"
    assert out["message"]["content"] == [{"type": "text", "text": "hi"}]
    assert "_codex_extras" not in out


def test_developer_role_message_is_dropped():
    raw = {
        "type": "response_item",
        "timestamp": "t",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [{"type": "input_text", "text": "system instructions go here"}],
        },
    }
    assert sanitize_event(raw) is None


def test_user_startup_context_message_is_dropped():
    raw = {
        "type": "response_item",
        "timestamp": "t",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": "<environment_context>\n  <cwd>/repo</cwd>\n</environment_context>",
            }],
        },
    }
    assert sanitize_event(raw) is None


def test_real_user_message_with_context_words_survives():
    raw = {
        "type": "response_item",
        "timestamp": "t",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{
                "type": "input_text",
                "text": "why is <environment_context> showing up in search?",
            }],
        },
    }
    out = sanitize_event(raw)
    assert out["type"] == "user"
    assert out["message"]["content"][0]["text"].startswith("why is")


def test_function_call_becomes_tool_use_with_summary():
    raw = {
        "type": "response_item",
        "timestamp": "t",
        "payload": {
            "type": "function_call",
            "call_id": "call_1",
            "name": "shell",
            "arguments": json.dumps({"command": "ls -la /etc/passwd"}),
        },
    }
    out = sanitize_event(raw)
    assert out["type"] == "assistant"
    block = out["message"]["content"][0]
    assert block == {
        "type": "tool_use",
        "id": "call_1",
        "name": "shell",
        "summary": "ls -la /etc/passwd",
    }


def test_function_call_with_namespace_stashes_in_extras():
    raw = {
        "type": "response_item",
        "timestamp": "t",
        "payload": {
            "type": "function_call",
            "call_id": "c1",
            "name": "search",
            "namespace": "mcp_probe",
            "arguments": "{}",
        },
    }
    out = sanitize_event(raw)
    assert out["_codex_extras"] == {"namespace": "mcp_probe"}


def test_function_call_output_becomes_tool_result_with_error_flag():
    raw = {
        "type": "response_item",
        "timestamp": "t",
        "payload": {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": {"is_error": True, "content": "permission denied"},
        },
    }
    out = sanitize_event(raw)
    assert out["type"] == "user"
    assert out["message"]["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "call_1",
        "is_error": True,
    }


def test_local_shell_call_stashes_action_and_status():
    raw = {
        "type": "response_item",
        "timestamp": "t",
        "payload": {
            "type": "local_shell_call",
            "call_id": "ls1",
            "status": "completed",
            "action": {"command": ["bash", "-c", "echo hi"], "type": "exec"},
        },
    }
    out = sanitize_event(raw)
    block = out["message"]["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "local_shell"
    assert block["summary"] == "bash -c echo hi"
    extras = out["_codex_extras"]
    assert extras["status"] == "completed"
    assert extras["action"]["type"] == "exec"


def test_reasoning_keeps_text_and_stashes_summary_drops_encrypted():
    raw = {
        "type": "response_item",
        "timestamp": "t",
        "payload": {
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "thinking step"}],
            "summary": [{"type": "summary_text", "text": "did the thing"}],
            "encrypted_content": "AAAA" * 1000,
        },
    }
    out = sanitize_event(raw)
    block = out["message"]["content"][0]
    assert block == {"type": "thinking", "thinking": "thinking step"}
    assert out["_codex_extras"]["reasoning_summary"][0]["text"] == "did the thing"
    serialized = json.dumps(out)
    assert "AAAA" not in serialized  # encrypted_content stripped


def test_reasoning_dropped_when_empty():
    assert sanitize_event({
        "type": "response_item",
        "timestamp": "t",
        "payload": {"type": "reasoning", "content": [], "summary": []},
    }) is None


def test_event_msg_runtime_and_duplicate_records_are_dropped():
    for sub in ("token_count", "task_started", "task_complete",
                "user_message", "agent_message", "item_completed",
                "thread_settings_applied"):
        raw = {
            "type": "event_msg",
            "timestamp": "t",
            "payload": {"type": sub, "info": {"x": 1}},
        }
        assert sanitize_event(raw) is None


def test_unknown_event_msg_fails_closed():
    raw = {
        "type": "event_msg",
        "timestamp": "t",
        "payload": {"type": "future_variant", "data": {"k": "v"}},
    }
    assert sanitize_event(raw) is None


def test_compacted_emits_compaction_event_with_replacement_history_in_extras():
    raw = {
        "type": "compacted",
        "timestamp": "t",
        "payload": {
            "message": "compacted to 12 messages",
            "replacement_history": [{"type": "message"}, {"type": "message"}],
        },
    }
    out = sanitize_event(raw)
    assert out["type"] == "system"
    assert out["subtype"] == "compaction"
    assert out["content"] == "compacted to 12 messages"
    assert out["_codex_extras"] == {"replacement_history_count": 2}


def test_non_dict_input_returned_as_is():
    assert sanitize_event("not a dict") == "not a dict"
    assert sanitize_event(None) is None  # None is not a dict, returned as None
    assert sanitize_event(42) == 42


def test_unknown_top_type_fails_closed():
    raw = {"type": "future_top_variant", "timestamp": "t", "payload": {"k": "v"}}
    assert sanitize_event(raw) is None


def test_latest_world_state_shape_is_dropped_without_inspecting_contents():
    raw = {
        "type": "world_state",
        "timestamp": "t",
        "payload": {
            "full": True,
            "state": {
                "skills": "must not leave the machine as an unknown payload",
                "permissions": {"secret": "must not leave either"},
            },
        },
    }
    assert sanitize_event(raw) is None


# ---- fixture-driven integration test -------------------------------------


def _read_fixture() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def test_fixture_assistant_final_answer_survives():
    """The captured 5-word reply should round-trip end-to-end."""
    sanitized = [sanitize_event(line) for line in _read_fixture()]
    final_answers = [
        s for s in sanitized
        if isinstance(s, dict) and s.get("type") == "assistant"
        and s.get("_codex_extras", {}).get("phase") == "final_answer"
    ]
    assert len(final_answers) == 1
    blocks = final_answers[0]["message"]["content"]
    assert blocks == [{"type": "text", "text": "Hello, hope you are well."}]


def test_fixture_drops_event_msgs_and_startup_preamble():
    sanitized = [sanitize_event(line) for line in _read_fixture()]
    kept = [s for s in sanitized if s is not None]
    raw = _read_fixture()
    n_event_msg = sum(1 for r in raw if r["type"] == "event_msg")
    n_response = sum(1 for r in raw if r["type"] == "response_item")
    n_session_meta = sum(1 for r in raw if r["type"] == "session_meta")
    n_turn_context = sum(1 for r in raw if r["type"] == "turn_context")
    # All event_msgs in the fixture are in the drop list (task_started/complete,
    # user/agent_message, token_count) — confirmed by the fixture inspection.
    expected_kept = n_response + n_session_meta + n_turn_context
    # response_item.reasoning may be dropped if empty; in our fixture it has
    # encrypted_content but empty `content` array, so it's dropped.
    n_reasoning = sum(
        1 for r in raw
        if r["type"] == "response_item" and r["payload"].get("type") == "reasoning"
        and not r["payload"].get("content")
    )
    n_startup_messages = sum(
        1 for r in raw
        if r["type"] == "response_item"
        and r["payload"].get("type") == "message"
        and r["payload"].get("role") in ("developer", "user")
        and (
            r["payload"].get("role") == "developer"
            or all(
                isinstance(c, dict)
                and isinstance(c.get("text"), str)
                and c["text"].strip().startswith("<environment_context>")
                for c in (r["payload"].get("content") or [])
            )
        )
    )
    expected_kept -= n_reasoning
    expected_kept -= n_startup_messages
    assert len(kept) == expected_kept
    serialized = json.dumps(kept)
    assert "permissions instructions" not in serialized
    assert "skills_instructions" not in serialized
    assert "environment_context" not in serialized
    assert "base_instructions" not in serialized
    assert "developer_instructions" not in serialized
    # Sanity check: nothing from event_msg survived.
    assert n_event_msg > 0  # fixture sanity
    assert all(s.get("subtype", "").startswith(("session_start", "turn_context"))
               or s["type"] in ("user", "assistant", "system")
               for s in kept)


def test_fixture_turn_context_carries_session_config():
    sanitized = [sanitize_event(line) for line in _read_fixture()]
    tc = next(
        s for s in sanitized
        if isinstance(s, dict) and s.get("subtype") == "turn_context"
    )
    extras = tc["_codex_extras"]
    # These four are the load-bearing config fields we want for v0.2 surfaces.
    for required in ("model", "sandbox_policy", "approval_policy", "cwd"):
        assert required in extras


# ---- build_batch_body integration ----------------------------------------


def test_build_batch_body_drops_only_lines_that_sanitize_to_none():
    """When every line is a drop (e.g. a tick that only saw token_counts),
    build_batch_body returns None — same contract as cc-tap."""
    drop_only = [
        json.dumps({
            "type": "event_msg", "timestamp": "t",
            "payload": {"type": "token_count", "info": {}},
        }).encode(),
        json.dumps({
            "type": "event_msg", "timestamp": "t",
            "payload": {"type": "task_complete"},
        }).encode(),
    ]
    body = build_batch_body(
        device_id="d", session_id="s", batch_seq=0, cwd="/r",
        base_line_no=0, lines=drop_only,
    )
    assert body is None


def test_build_batch_body_drops_startup_preamble_messages():
    preamble_only = [
        json.dumps({
            "type": "response_item",
            "timestamp": "t",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{
                    "type": "input_text",
                    "text": "<permissions instructions>\nno tools\n</permissions instructions>",
                }],
            },
        }).encode(),
        json.dumps({
            "type": "response_item",
            "timestamp": "t",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{
                    "type": "input_text",
                    "text": "<environment_context>\n  <cwd>/repo</cwd>\n</environment_context>",
                }],
            },
        }).encode(),
    ]
    body = build_batch_body(
        device_id="d", session_id="s", batch_seq=0, cwd="/r",
        base_line_no=0, lines=preamble_only,
    )
    assert body is None


def test_build_batch_body_packs_kept_events_with_line_numbers():
    one_message = [json.dumps({
        "type": "response_item", "timestamp": "t",
        "payload": {"type": "message", "role": "user",
                    "content": [{"type": "input_text", "text": "hi"}]},
    }).encode()]
    body = build_batch_body(
        device_id="d", session_id="s", batch_seq=7, cwd="/r",
        base_line_no=42, lines=one_message,
    )
    assert body is not None
    obj = json.loads(body)
    assert obj["session_id"] == "s"
    assert obj["batch_seq"] == 7
    assert len(obj["events"]) == 1
    assert obj["events"][0]["line_no"] == 42
    assert obj["events"][0]["raw"]["message"]["content"][0]["text"] == "hi"
