"""The MCP server is compact, structured, and read-only."""

from __future__ import annotations

import asyncio

from ros.mcp.server import create_server
from ros.mcp.service import ResearchReadService
from ros.mcp.source import ResearchOSSource


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
