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
