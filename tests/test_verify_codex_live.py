from __future__ import annotations

from types import SimpleNamespace

from scripts.verify_codex_live import _codex_hit, _resolve_read_token


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


def test_canary_prefers_explicit_then_environment_read_token(monkeypatch) -> None:
    monkeypatch.setenv("PROBE_MCP_TOKEN", "probe_pat_mcp")
    monkeypatch.setenv("PROBE_TOKEN", "probe_pat_write")
    assert _resolve_read_token("probe_pat_explicit") == "probe_pat_explicit"
    assert _resolve_read_token() == "probe_pat_mcp"


def test_canary_prefers_configured_mcp_token_over_write_token(monkeypatch) -> None:
    monkeypatch.delenv("PROBE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("PROBE_TOKEN", raising=False)
    monkeypatch.setattr(
        "probe.config.resolve",
        lambda: SimpleNamespace(mcp_token="probe_pat_read", token="probe_pat_write"),
    )
    assert _resolve_read_token() == "probe_pat_read"
