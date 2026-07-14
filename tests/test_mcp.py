"""The MCP server is compact, structured, and read-only."""

from __future__ import annotations

import asyncio

from probe.mcp.server import create_server
from probe.mcp.service import ResearchReadService
from probe.mcp.source import ResearchOSSource


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
