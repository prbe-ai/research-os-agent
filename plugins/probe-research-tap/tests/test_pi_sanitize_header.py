# tests/test_pi_sanitize_header.py
from tap.pi_sanitize import sanitize_event


def test_session_header_becomes_a_system_event():
    out = sanitize_event({
        "type": "session", "version": 3, "id": "uuid-1",
        "timestamp": "2026-08-25T14:00:00.000Z", "cwd": "/repo",
    })
    assert out["type"] == "system"
    assert out["subtype"] == "session_meta"
    assert out["_pi_extras"]["version"] == 3
    assert out["_pi_extras"]["cwd"] == "/repo"


def test_fork_lineage_is_carried():
    # /fork and /clone write a NEW file pointing at the original. Without this
    # a forked session looks like an unrelated one starting mid-conversation.
    out = sanitize_event({
        "type": "session", "version": 3, "id": "uuid-2",
        "timestamp": "2026-08-25T14:00:00.000Z", "cwd": "/repo",
        "parentSession": "/home/u/.pi/agent/sessions/--repo--/older.jsonl",
    })
    assert out["_pi_extras"]["parent_session"].endswith("older.jsonl")


def test_unknown_future_version_is_flagged_not_silently_parsed():
    out = sanitize_event({
        "type": "session", "version": 99, "id": "u",
        "timestamp": "2026-08-25T14:00:00.000Z", "cwd": "/repo",
    })
    assert out["_pi_extras"]["unsupported_version"] is True


def test_v1_and_v2_are_supported():
    for version in (1, 2, 3):
        out = sanitize_event({
            "type": "session", "version": version, "id": "u",
            "timestamp": "2026-08-25T14:00:00.000Z", "cwd": "/repo",
        })
        assert "unsupported_version" not in out["_pi_extras"]
