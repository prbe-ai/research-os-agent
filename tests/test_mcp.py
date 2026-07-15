"""The MCP server is compact, structured, and read-only."""

from __future__ import annotations

import asyncio
import json

import pytest

from probe.mcp.server import create_server
from probe.mcp.service import ResearchReadService
from probe.mcp.source import ResearchOSSource
from probe.sdk import errors


def _search_response(
    *,
    state: str = "ok",
    exact: list[dict] | None = None,
    semantic: list[dict] | None = None,
    exact_error: str | None = None,
    semantic_error: str | None = None,
    exact_cursor: str | None = None,
    semantic_cursor: str | None = None,
) -> dict:
    """A CONTRACT.md-shaped POST /v1/search response."""
    return {
        "query": "q",
        "state": state,
        "exact": {"results": exact or [], "cursor": exact_cursor, "error": exact_error},
        "semantic": {"results": semantic or [], "cursor": semantic_cursor, "error": semantic_error},
    }


def test_context_and_search_use_current_api_fallback(client, app):
    project = client.ensure_project("folding")
    run = client.run(
        project="folding",
        experiment="dockq-path",
        hypothesis="relative paths fix scoring",
        name="eval-1",
    )
    service = ResearchReadService(ResearchOSSource(client))

    context = service.research_context("DockQ scoring paths", project_ref="folding")
    assert context["scope"]["customer_id"] == "lab-42"
    assert context["capabilities"]["semantic_search"] is False
    assert context["data"]["project"]["id"] == project["id"]

    results = service.research_search("DockQ paths")
    assert results["data"]["results"][0]["id"] == app.runs[run.id]["experiment_id"]
    assert results["completeness"]["state"] == "partial"
    assert "semantic_search" in results["completeness"]["missing"]


# -- research_search over POST /v1/search (workspaces+kb fold-in) ------------
def test_search_maps_corpora_and_merges_channels(client, app):
    app.search_response = _search_response(
        exact=[
            {
                "entity_type": "experiment", "id": "e-1", "name": "adam sweep",
                "slug": "adam-sweep", "workspace_id": "ws-1", "project_id": "p-1",
                "experiment_id": None, "run_id": None, "score": 0.91,
            },
            {
                "entity_type": "artifact", "id": "a-1", "name": "adam.csv",
                "slug": None, "workspace_id": None, "project_id": None,
                "experiment_id": "e-1", "run_id": "r-1", "score": 0.55,
            },
        ],
        semantic=[
            {
                "doc_id": "file:f-1", "title": "sweep notes", "snippet": "adam beta2 ...",
                "score": 0.83, "source_system": "workspace", "source_url": None,
                "ref": {"kind": "file", "id": "f-1"},
            },
        ],
        exact_cursor="exact-c1",
    )
    service = ResearchReadService(ResearchOSSource(client))
    out = service.research_search("adam sweep", corpora=["documents"], workspace_id="ws-1")

    # corpora vocabulary maps onto backend corpus values (documents -> github+files,
    # experiments always searched); the workspace lens and limits ride along.
    body = app.search_requests[-1]
    assert body["query"] == "adam sweep"
    assert body["corpus"] == ["experiments", "files", "github"]
    assert body["workspace_id"] == "ws-1"
    assert body["top_k"] == 8 and body["exact_limit"] == 8

    # merged result list keeps the tool contract with per-channel provenance
    results = out["data"]["results"]
    assert [r["why_matched"]["channel"] for r in results] == ["exact", "semantic", "exact"]
    assert results[0]["entity_type"] == "experiment"
    assert results[0]["resource"] == "research://experiments/e-1/card"
    assert results[0]["why_matched"] == {"mode": "exact", "channel": "exact", "score": 0.91}
    assert results[1]["entity_type"] == "file" and results[1]["id"] == "f-1"
    assert results[1]["card"]["snippet"] == "adam beta2 ..."
    assert results[2]["entity_type"] == "artifact" and results[2]["card"]["run_id"] == "r-1"

    assert out["completeness"] == {"state": "complete", "missing": []}
    assert out["capabilities"]["unified_search"] is True
    assert out["capabilities"]["semantic_search"] is True
    assert out["capabilities"]["kb_documents"] is True

    # per-channel backend cursors ride one opaque tool cursor, round-trippable
    assert json.loads(out["next_cursor"]) == {"exact": "exact-c1"}
    service.research_search("adam sweep", cursor=out["next_cursor"])
    assert app.search_requests[-1]["exact_cursor"] == "exact-c1"
    assert "semantic_cursor" not in app.search_requests[-1]


def test_search_transcripts_corpus_reported_as_missing_kb(client, app):
    app.search_response = _search_response()
    service = ResearchReadService(ResearchOSSource(client))
    out = service.research_search("q", corpora=["transcripts", "assets"])
    assert app.search_requests[-1]["corpus"] == ["experiments", "files"]
    assert out["completeness"] == {"state": "partial", "missing": ["kb_corpora"]}
    assert out["data"]["unsupported_corpora"] == ["transcripts"]


def test_search_falls_back_to_keyword_on_pre_search_backend(client, app):
    # app.search_response stays None -> the fake 404s POST /v1/search like a
    # backend that predates the endpoint; the old keyword behavior must survive.
    client.ensure_project("folding")
    client.run(
        project="folding",
        experiment="dockq-path",
        hypothesis="relative paths fix scoring",
        name="eval-1",
    )
    service = ResearchReadService(ResearchOSSource(client))
    out = service.research_search("DockQ paths", corpora=["documents"])
    assert app.search_requests, "should try POST /v1/search first"
    assert out["data"]["results"][0]["why_matched"]["mode"] == "keyword_fallback"
    assert out["completeness"]["state"] == "partial"
    assert set(out["completeness"]["missing"]) == {"kb_corpora", "semantic_search"}
    assert out["capabilities"]["unified_search"] is False
    assert out["capabilities"]["semantic_search"] is False


def test_search_partial_passthrough_when_engine_down(client, app):
    app.search_response = _search_response(
        state="partial",
        exact=[
            {
                "entity_type": "project", "id": "p-1", "name": "folding", "slug": "folding",
                "workspace_id": "ws-1", "project_id": None, "experiment_id": None,
                "run_id": None, "score": 1.0,
            },
        ],
        semantic_error="engine_timeout",
    )
    service = ResearchReadService(ResearchOSSource(client))
    out = service.research_search("folding")
    assert out["completeness"] == {"state": "partial", "missing": ["semantic_search"]}
    assert out["data"]["channels"]["semantic"]["error"] == "engine_timeout"
    assert [r["entity_type"] for r in out["data"]["results"]] == ["project"]
    # the endpoint exists but the semantic engine is down right now
    assert out["capabilities"]["unified_search"] is True
    assert out["capabilities"]["semantic_search"] is False
    assert out["capabilities"]["kb_documents"] is False


def test_capabilities_probe_discovers_search_once(client, app):
    app.search_response = _search_response()
    service = ResearchReadService(ResearchOSSource(client))
    context = service.research_context("anything")
    assert context["capabilities"]["unified_search"] is True
    assert context["capabilities"]["semantic_search"] is True
    assert "semantic_search" not in context["completeness"]["missing"]
    assert len(app.search_requests) == 1  # one cached capability probe
    service.research_context("again")
    assert len(app.search_requests) == 1


def test_search_unknown_workspace_is_not_found_not_fallback(client, app):
    # The contract 404s an unknown/foreign workspace_id (oracle-safe); that must
    # surface as NotFound, not silently degrade to the keyword fallback.
    app.search_response = _search_response()
    app.search_404_workspace_ids.add("ws-missing")
    service = ResearchReadService(ResearchOSSource(client))
    with pytest.raises(errors.NotFoundError):
        service.research_search("q", workspace_id="ws-missing")
    # disambiguated via one extra probe without the workspace lens
    assert len(app.search_requests) == 2


def test_asset_resolve_returns_no_match_when_not_found(client):
    # The registry exists now (fold #5); resolving an absent asset is an honest
    # no_match over the real registry, not a missing-capability.
    service = ResearchReadService(ResearchOSSource(client))
    result = service.research_resolve("dockq-scorer", kind="script")
    assert result["data"]["state"] == "no_match"


def test_server_exposes_only_the_six_read_tools(client):
    service = ResearchReadService(ResearchOSSource(client))
    server = create_server(service)
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert names == {
        "research_context",
        "research_search",
        "research_get",
        "research_compare",
        "research_resolve",
        "research_trace_file",
    }
    assert not any(name.startswith(("create", "update", "promote", "upload")) for name in names)


# -- hosted HTTP mode: per-request auth + health -----------------------------
def test_http_auth_middleware_propagates_token_and_health():
    from probe.mcp.server import _token_var, with_auth_and_health

    captured: dict = {}

    async def fake_inner(scope, receive, send):
        captured["token"] = _token_var.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = with_auth_and_health(fake_inner)

    async def drive(path, headers):
        scope = {"type": "http", "path": path, "headers": headers}
        sent: list = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(m):
            sent.append(m)

        await app(scope, receive, send)
        return sent

    # the caller's bearer token reaches the inner app (i.e. the tool)
    asyncio.run(drive("/mcp", [(b"authorization", b"Bearer tok-123")]))
    assert captured["token"] == "tok-123"
    # and is cleared after the request (no leak across tenants)
    assert _token_var.get() is None
    # health endpoint answers without touching the inner app
    sent = asyncio.run(drive("/healthz", []))
    assert sent[0]["status"] == 200


def test_service_resolves_from_request_token():
    from probe.mcp.server import _clients, _service_from_token, _token_var

    _clients.clear()
    reset = _token_var.set("read-only-tok")
    try:
        _service_from_token()
        assert "read-only-tok" in _clients
        assert _clients["read-only-tok"].settings.token == "read-only-tok"
    finally:
        _token_var.reset(reset)
        _clients.clear()


def test_http_app_builds():
    from probe.mcp.server import http_app

    app = http_app()  # FastMCP streamable-http + auth/health wrapper
    assert callable(app)
