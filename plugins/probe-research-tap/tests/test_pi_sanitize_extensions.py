# tests/test_pi_sanitize_extensions.py
from tap.pi_sanitize import sanitize_event


def test_custom_message_enters_the_transcript_as_user_content():
    # custom_message DOES participate in LLM context, so it is conversation,
    # not bookkeeping.
    out = sanitize_event({
        "type": "custom_message", "id": "i9", "parentId": "h8",
        "timestamp": "2026-08-25T14:25:00.000Z",
        "customType": "my-extension", "content": "Injected context",
        "display": True,
    })
    assert out["type"] == "user"
    assert out["message"]["content"] == [{"type": "text", "text": "Injected context"}]
    assert out["_pi_extras"]["custom_type"] == "my-extension"


def test_custom_message_custom_type_rejects_non_strings():
    # customType is extension-chosen metadata, not content: a nested object
    # here must not ride into extras raw the way `content` is allowed to.
    out = sanitize_event({
        "type": "custom_message", "id": "i9", "parentId": "h8",
        "timestamp": "2026-08-25T14:25:00.000Z",
        "customType": {"leaked": "custom-type-value"}, "content": "hi",
    })
    assert "custom_type" not in out["_pi_extras"]
    assert "leaked" not in repr(out)


def test_custom_entry_custom_type_rejects_non_strings():
    # Same field, same guard, the `custom` (state) path: a non-string
    # customType must fall back to "unknown" rather than smuggling an
    # object into extras or the f-string-built subtype.
    out = sanitize_event({
        "type": "custom", "id": "h8", "parentId": "g7",
        "timestamp": "2026-08-25T14:20:00.000Z",
        "customType": {"leaked": "custom-type-value"}, "data": {"count": 1},
    })
    assert out["subtype"] == "custom:unknown"
    assert out["_pi_extras"]["custom_type"] == "unknown"
    assert "leaked" not in repr(out)


def test_custom_entry_is_state_not_conversation():
    out = sanitize_event({
        "type": "custom", "id": "h8", "parentId": "g7",
        "timestamp": "2026-08-25T14:20:00.000Z",
        "customType": "my-extension", "data": {"count": 42},
    })
    assert out["type"] == "system"
    assert out["subtype"] == "custom:my-extension"
    # Extension state is arbitrary and may be large or sensitive: record the
    # keys, never the values.
    assert out["_pi_extras"]["data_keys"] == ["count"]
    assert "42" not in repr(out["_pi_extras"])


def test_an_entry_type_from_a_fork_is_never_dropped():
    out = sanitize_event({
        "type": "some_fork_invention", "id": "z9", "parentId": "y8",
        "timestamp": "2026-08-25T14:40:00.000Z", "whatever": True,
    })
    assert out["type"] == "system"
    assert out["subtype"] == "unknown:some_fork_invention"
    assert out["_pi_extras"]["id"] == "z9"


def test_a_content_block_type_from_a_fork_is_never_dropped_but_never_forwarded():
    # The "never drop" principle above is about top-level entries. This is
    # the content-block equivalent, and it must NOT follow sanitize.py's
    # "forward unknown blocks unchanged" precedent — that precedent trusts
    # the producer, and pi's forks are exactly what we don't trust here.
    out = sanitize_event({
        "type": "message", "id": "m1", "parentId": "m0",
        "timestamp": "2026-08-25T14:41:00.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "forkOnlyBlockType", "anything": "goes",
                         "apiKey": "sk-should-never-appear"}],
            "provider": "p", "model": "m", "stopReason": "stop",
        },
    })
    blocks = out["message"]["content"]
    assert len(blocks) == 1
    assert blocks[0] == {"type": "unknown_block", "block_type": "forkOnlyBlockType"}
    assert "sk-should-never-appear" not in repr(out)
    assert "anything" not in repr(out)
    assert "goes" not in repr(out)


def test_a_fork_cannot_ship_a_nested_object_through_any_copied_field():
    # Every field this translator copies out of an entry, given an object
    # where a string, number or flag belongs. Before tap 0.9.10 each of these
    # shipped the object as-is (or its repr, through an f-string into the
    # rendered subtype), and an object `version` crashed the translator.
    leak = {"leaked": "nested-value"}
    entries = [
        {"type": "session", "version": leak, "id": leak, "cwd": leak, "parentSession": leak},
        {"type": leak, "id": leak, "parentId": leak, "timestamp": leak},
        {"type": "message", "id": "m1", "message": {"role": leak}},
        {"type": "message", "id": "m1", "message": {
            "role": "assistant", "provider": leak, "model": leak, "api": leak, "stopReason": leak,
            "content": [{"type": "text", "text": leak}, {"type": "thinking", "thinking": leak}],
        }},
        {"type": "message", "id": "m1", "message": {"role": "user", "content": [{"type": "text", "text": leak}]}},
        {"type": "message", "id": "m1", "message": {
            "role": "bashExecution", "command": "ls", "output": "ok",
            "exitCode": leak, "cancelled": leak, "truncated": leak,
        }},
        {"type": "compaction", "id": "c1", "summary": leak, "tokensBefore": leak, "firstKeptEntryId": leak},
        {"type": "branch_summary", "id": "b1", "summary": leak, "fromId": leak},
        {"type": "label", "id": "l1", "targetId": leak, "label": leak},
        {"type": "model_change", "id": "x1", "provider": leak, "modelId": leak},
        {"type": "thinking_level_change", "id": "t1", "thinkingLevel": leak},
        {"type": "session_info", "id": "s1", "name": leak},
        {"type": "message", "id": "m1", "message": {
            "role": "assistant", "content": [], "stopReason": "error", "errorMessage": leak,
        }},
    ]
    for entry in entries:
        out = sanitize_event(entry)
        assert "leaked" not in repr(out), entry


def test_a_custom_entry_ships_a_bounded_list_of_short_key_names():
    data = {f"k{i:03d}" + "x" * 100: i for i in range(80)}
    out = sanitize_event({"type": "custom", "id": "x1", "customType": "ext", "data": data})
    keys = out["_pi_extras"]["data_keys"]
    assert len(keys) == 50 and all(len(k) == 64 for k in keys)
    assert sanitize_event({"type": "custom", "id": "x1", "customType": "ext", "data": {"a": 1}}
                          )["_pi_extras"]["data_keys"] == ["a"]


def test_real_string_and_number_fields_still_ship():
    out = sanitize_event({
        "type": "message", "id": "m1", "parentId": "m0",
        "message": {"role": "bashExecution", "command": "ls", "output": "ok",
                    "exitCode": 2, "cancelled": False, "truncated": True},
    })
    assert out[0]["_pi_extras"] == {"id": "m1", "parentId": "m0", "exit_code": 2,
                                    "cancelled": False, "truncated": True}
    out = sanitize_event({"type": "label", "id": "l1", "targetId": "m1", "label": "good run"})
    assert out["_pi_extras"] == {"id": "l1", "target_id": "m1", "label": "good run"}
