# tests/test_pi_sanitize_tree.py
from tap.pi_sanitize import sanitize_event


def test_compaction_keeps_its_summary_text():
    out = sanitize_event({
        "type": "compaction", "id": "f6", "parentId": "e5",
        "timestamp": "2026-08-25T14:10:00.000Z",
        "summary": "User discussed X, Y, Z", "tokensBefore": 50000,
    })
    assert out["subtype"] == "compaction"
    assert out["content"] == "User discussed X, Y, Z"
    assert out["_pi_extras"]["tokens_before"] == 50000


def test_branch_summary_keeps_the_abandoned_branch_prose():
    # This is the reason we ship the whole tree. A branch_summary is an
    # LLM-written account of an approach the user WALKED AWAY FROM, which is
    # the one thing a transcript of the surviving branch can never show.
    out = sanitize_event({
        "type": "branch_summary", "id": "g7", "parentId": "a1",
        "timestamp": "2026-08-25T14:15:00.000Z", "fromId": "f6",
        "summary": "Branch explored a Redis cache and abandoned it: cold-start "
                   "cost exceeded the query it replaced.",
    })
    assert out["subtype"] == "branch_summary"
    assert "abandoned it" in out["content"]
    assert out["_pi_extras"]["from_id"] == "f6"


def test_label_records_the_users_bookmark():
    out = sanitize_event({
        "type": "label", "id": "j0", "parentId": "i9",
        "timestamp": "2026-08-25T14:30:00.000Z",
        "targetId": "a1b2c3d4", "label": "checkpoint-1",
    })
    assert out["subtype"] == "label"
    assert out["_pi_extras"] == {"id": "j0", "parentId": "i9",
                                 "target_id": "a1b2c3d4", "label": "checkpoint-1"}


def test_model_change_is_recorded():
    out = sanitize_event({
        "type": "model_change", "id": "d4", "parentId": "c3",
        "timestamp": "2026-08-25T14:05:00.000Z",
        "provider": "openai", "modelId": "gpt-4o",
    })
    assert out["subtype"] == "model_change"
    assert out["_pi_extras"]["provider"] == "openai"
    assert out["_pi_extras"]["model_id"] == "gpt-4o"


def test_session_info_carries_the_display_name():
    out = sanitize_event({
        "type": "session_info", "id": "k1", "parentId": "j0",
        "timestamp": "2026-08-25T14:35:00.000Z", "name": "Refactor auth module",
    })
    assert out["_pi_extras"]["name"] == "Refactor auth module"
