# tests/test_pi_sanitize_tools.py
from tap.pi_sanitize import sanitize_event


def _entry(message):
    return {"type": "message", "id": "e1", "parentId": "e0",
            "timestamp": "2026-08-25T14:00:03.000Z", "message": message}


def test_tool_result_ships_a_size_not_a_body():
    out = sanitize_event(_entry({
        "role": "toolResult", "toolCallId": "call_1", "toolName": "bash",
        "content": [{"type": "text", "text": "x" * 5000}], "isError": False,
    }))
    assert out["type"] == "user"
    block = out["message"]["content"][0]
    assert block["type"] == "tool_result"
    assert block["tool_use_id"] == "call_1"
    assert block["result_bytes"] == 5000
    assert "content" not in block
    assert "is_error" not in block


def test_tool_result_metadata_rejects_non_strings():
    # pi is not a trusted producer: toolCallId/toolName are copied out with
    # an isinstance guard, unlike the equivalent trusted-producer fields in
    # sanitize.py. A nested object must not travel through as-is.
    out = sanitize_event(_entry({
        "role": "toolResult", "toolCallId": {"leaked": "id-value"},
        "toolName": {"leaked": "name-value"},
        "content": [{"type": "text", "text": "ok"}], "isError": False,
    }))
    block = out["message"]["content"][0]
    assert block["tool_use_id"] == ""
    assert "tool_name" not in out["_pi_extras"]
    assert "leaked" not in repr(out)


def test_failed_tool_result_is_marked():
    out = sanitize_event(_entry({
        "role": "toolResult", "toolCallId": "c", "toolName": "bash",
        "content": [{"type": "text", "text": "boom"}], "isError": True,
    }))
    assert out["message"]["content"][0]["is_error"] is True


def test_tool_call_name_rejects_non_strings():
    # Same untrusted-metadata class as tool_use id/toolCallId/toolName: a
    # fork can ship {"name": {"leaked": "..."}} where a string is expected.
    out = sanitize_event(_entry({
        "role": "assistant",
        "content": [{"type": "toolCall", "id": "c1",
                     "name": {"leaked": "tool-name-value"},
                     "arguments": {"command": "ls"}}],
        "provider": "p", "model": "m", "stopReason": "toolUse",
    }))
    block = out["message"]["content"][0]
    assert block["name"] == ""
    assert "leaked" not in repr(out)


def test_bash_execution_becomes_a_tool_use_pair():
    # role=bashExecution is pi's `!command` escape — a real shell run with no
    # matching toolCall. It carries command AND output on one entry.
    out = sanitize_event(_entry({
        "role": "bashExecution", "command": "ls -la", "output": "total 8\n",
        "exitCode": 0, "cancelled": False, "truncated": False,
    }))
    assert isinstance(out, list) and len(out) == 2
    use, result = out
    assert use["message"]["content"][0]["name"] == "bash"
    assert use["message"]["content"][0]["summary"] == "ls -la"
    assert result["message"]["content"][0]["result_bytes"] == len("total 8\n")
    assert use["_pi_extras"]["exit_code"] == 0


def test_excluded_bash_output_is_still_recorded_as_having_run():
    # `!!command` sets excludeFromContext: the model never saw the output, but
    # the user DID run it and the transcript should say so.
    out = sanitize_event(_entry({
        "role": "bashExecution", "command": "cat .env", "output": "SECRET=1",
        "exitCode": 0, "cancelled": False, "truncated": False,
        "excludeFromContext": True,
    }))
    use, _result = out
    assert use["_pi_extras"]["excluded_from_context"] is True


def test_bash_execution_command_rejects_non_strings():
    # command reaches this handler by a different path than the
    # `arguments.get("command")` branch in _summarize_arguments (which is
    # isinstance-checked): guard it the same way, since a non-string here
    # would otherwise crash the `[:_COMMAND_MAX_LEN]` slice or ship the raw
    # object through as "summary".
    out = sanitize_event(_entry({
        "role": "bashExecution", "command": {"leaked": "cmd-value"},
        "output": "ok", "exitCode": 0, "cancelled": False, "truncated": False,
    }))
    use, _result = out
    assert use["message"]["content"][0]["summary"] == ""
    assert "leaked" not in repr(out)


def test_bash_execution_call_id_rejects_non_string_entry_id():
    # The synthetic call_id is built from the entry's own `id` and becomes
    # the tool_use `id` / tool_result `tool_use_id` pairing key — same class
    # as tool_use id elsewhere, just reached via f-string interpolation
    # instead of a dict literal. NOTE: the entry's `id` also rides through
    # unguarded into `_pi_extras` via `_tree_extras` (a separate, documented,
    # load-bearing tree-navigation field this fix deliberately does not
    # touch) — so this test checks only the synthetic pairing id, not that
    # the whole output is free of the leaked value.
    out = sanitize_event({
        "type": "message", "id": {"leaked": "entry-id-value"}, "parentId": "e0",
        "timestamp": "2026-08-25T14:00:03.000Z",
        "message": {
            "role": "bashExecution", "command": "ls", "output": "ok",
            "exitCode": 0, "cancelled": False, "truncated": False,
        },
    })
    use, result = out
    assert use["message"]["content"][0]["id"] == "bash-"
    assert result["message"]["content"][0]["tool_use_id"] == "bash-"


def test_edit_tool_call_ships_counts_not_content():
    # pi's real edit schema: {path, edits: [{oldText, newText}]}. Always an
    # array, even for one change.
    out = sanitize_event(_entry({
        "role": "assistant",
        "content": [{"type": "toolCall", "id": "c", "name": "edit", "arguments": {
            "path": "/repo/a.py",
            "edits": [{"oldText": "one\ntwo\n", "newText": "one\ntwo\nthree\n"}],
        }}],
        "provider": "p", "model": "m", "stopReason": "toolUse",
    }))
    stats = out["message"]["content"][0]["edit_stats"]
    assert stats == {"op": "edit", "edits": 1, "removed_lines": 2, "added_lines": 3,
                     "removed_bytes": 8, "added_bytes": 14}
    assert "three" not in repr(out)


def test_legacy_flat_edit_shape_still_measures():
    # Sessions published before the edits[] schema landed use flat
    # oldText/newText. Backfill of historical sessions depends on this.
    out = sanitize_event(_entry({
        "role": "assistant",
        "content": [{"type": "toolCall", "id": "c", "name": "edit", "arguments": {
            "path": "/repo/a.py", "oldText": "one\ntwo\n",
            "newText": "one\ntwo\nthree\n",
        }}],
        "provider": "p", "model": "m", "stopReason": "toolUse",
    }))
    stats = out["message"]["content"][0]["edit_stats"]
    assert stats == {"op": "edit", "edits": 1, "removed_lines": 2, "added_lines": 3,
                     "removed_bytes": 8, "added_bytes": 14}


def test_multiple_disjoint_edits_are_summed():
    out = sanitize_event(_entry({
        "role": "assistant",
        "content": [{"type": "toolCall", "id": "c", "name": "edit", "arguments": {
            "path": "/repo/a.py",
            "edits": [
                {"oldText": "a\n", "newText": "aa\n"},
                {"oldText": "b\n", "newText": "bb\n"},
            ],
        }}],
        "provider": "p", "model": "m", "stopReason": "toolUse",
    }))
    assert out["message"]["content"][0]["edit_stats"]["edits"] == 2


def test_write_tool_call_ships_counts_not_content():
    out = sanitize_event(_entry({
        "role": "assistant",
        "content": [{"type": "toolCall", "id": "c", "name": "write", "arguments": {
            "path": "/repo/new.py", "content": "line1\nline2\n",
        }}],
        "provider": "p", "model": "m", "stopReason": "toolUse",
    }))
    assert out["message"]["content"][0]["edit_stats"] == {
        "op": "write", "added_lines": 2, "added_bytes": 12}
    assert "line1" not in repr(out)
