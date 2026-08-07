from __future__ import annotations

from scripts.verify_codex_live import _codex_hit


def test_canary_requires_codex_source_and_marker() -> None:
    body = {
        "semantic": {
            "results": [
                {"source_system": "claude_code", "chunks": [{"content": "probe-cx-123"}]},
                {"source_system": "codex", "chunks": [{"content": "different"}]},
                {"source_system": "codex", "chunks": [{"content": "PROBE-CX-123"}]},
            ]
        }
    }
    assert _codex_hit(body, "probe-cx-123") == body["semantic"]["results"][2]
    assert _codex_hit(body, "missing") is None


def test_canary_does_not_accept_query_echoed_by_relevance_explanation() -> None:
    body = {
        "semantic": {
            "results": [
                {
                    "source_system": "codex",
                    "why_relevant": "This might relate to PROBE-CX-FAKE",
                    "chunks": [{"content": "an unrelated captured transcript"}],
                }
            ]
        }
    }
    assert _codex_hit(body, "probe-cx-fake") is None
