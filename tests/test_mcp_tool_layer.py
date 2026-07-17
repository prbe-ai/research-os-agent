"""Round-trip through the REAL MCP tool layer, not the service underneath it.

Every other test calls `ResearchReadService.research_get(...)` directly. That skips
FastMCP entirely — and FastMCP is not a passthrough. Its `pre_parse_json` runs
json.loads on every string argument and, when the result is not a scalar, REPLACES
the argument with the parsed object. So a `json.dumps({...})` cursor arrives at the
tool as a dict and is rejected against `cursor: str | None`.

That is how research_search shipped paginating perfectly in-process and 422-ing over
the wire, for as long as it has existed. The service-level tests could never see it;
only a call through `mcp.call_tool` can. The cursor is the contract an agent actually
has to use, so this file exercises the layer the agent talks to.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from probe.mcp.server import create_server
from probe.mcp.service import ResearchReadService, _pack_cursor
from probe.mcp.source import ResearchOSSource


def _server(client):
    return create_server(ResearchReadService(ResearchOSSource(client)))


def _call(server, tool: str, args: dict):
    """Invoke a tool the way a real MCP client does, and unwrap the payload."""
    result = asyncio.run(server.call_tool(tool, args))
    # FastMCP returns (content, structured) in this version; older ones return content.
    payload = result[1] if isinstance(result, tuple) else result
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    if isinstance(payload, list):  # content blocks
        return json.loads(payload[0].text)
    return payload


def _run_with_spans(client, app, count: int = 40):
    run = client.run(project="folding", experiment="e", hypothesis="h", name="r")
    app.spans[run.id] = [
        {"id": f"span-{i}", "run_id": run.id, "span_type": "rollout", "name": f"n-{i}",
         "step_index": i, "status": "ok", "parent_span_id": None,
         "attributes": {"reward": i * 0.1}, "summary": {},
         "started_at": "2026-07-16T00:00:00Z", "ended_at": "2026-07-16T00:00:01Z",
         "customer_id": "lab-42", "created_at": "2026-07-16T00:00:00Z"}
        for i in range(count)
    ]
    return run.id


def test_research_get_cursor_round_trips_through_the_tool_layer(client, app):
    """The bug this file exists for: a JSON-object cursor is coerced to a dict by
    FastMCP before validation, so passing back a next_cursor the tool JUST issued
    fails with "Input should be a valid string". Caught only by smoke-testing the
    hosted MCP — 264 service-level tests were green."""
    rid = _run_with_spans(client, app)
    server = _server(client)

    page1 = _call(server, "research_get",
                  {"ref": f"run:{rid}", "view": "trajectory", "token_budget": 400})
    cursor = page1["next_cursor"]
    assert cursor is not None and isinstance(cursor, str)

    page2 = _call(server, "research_get",
                  {"ref": f"run:{rid}", "view": "trajectory", "token_budget": 400,
                   "cursor": cursor})
    first = [s["id"] for s in page1["data"]["spans"]]
    second = [s["id"] for s in page2["data"]["spans"]]
    assert second, "the cursor the tool just issued returned nothing through the tool layer"
    assert not set(first) & set(second)


def test_a_cursor_is_opaque_and_survives_fastmcp_pre_parsing(client, app):
    """The token must not be JSON: FastMCP replaces any string arg that json.loads
    to a non-scalar with the parsed object. This pins WHY the cursor is packed —
    revert _pack_cursor to json.dumps and this fails."""
    from mcp.server.fastmcp.utilities.func_metadata import func_metadata

    def research_get(ref: str, cursor: str | None = None) -> dict: ...

    token = _pack_cursor({"offset": 2, "view": "trajectory"})
    with pytest.raises(json.JSONDecodeError):
        json.loads(token)

    survived = func_metadata(research_get).pre_parse_json({"ref": "run:x", "cursor": token})
    assert isinstance(survived["cursor"], str), "FastMCP coerced the cursor away from str"


def test_full_trajectory_walk_through_the_tool_layer(client, app):
    """End to end as an agent drives it: every span, no skips, no duplicates."""
    rid = _run_with_spans(client, app, count=40)
    server = _server(client)

    seen: list[str] = []
    cursor, pages = None, 0
    while pages < 50:
        args = {"ref": f"run:{rid}", "view": "trajectory", "token_budget": 400}
        if cursor:
            args["cursor"] = cursor
        page = _call(server, "research_get", args)
        seen.extend(s["id"] for s in page["data"]["spans"])
        cursor = page["next_cursor"]
        pages += 1
        if not cursor:
            break

    assert pages > 1, "budget did not force pagination; the walk proves nothing"
    assert seen == [f"span-{i}" for i in range(40)]


def test_research_search_cursor_round_trips_through_the_tool_layer(client, app):
    """research_search had the SAME defect — same json.dumps({...}) format, same
    coercion. It was never reachable from a service-level test."""
    client.run(project="folding", experiment="dockq", hypothesis="h", name="r")
    app.search_responses = [
        {"query": "q", "state": "ok",
         "exact": {"results": [{"entity_type": "experiment", "id": "e1", "name": "one"}],
                   "cursor": "backend-exact-2", "error": None},
         "semantic": {"results": [], "cursor": None, "error": None}},
        {"query": "q", "state": "ok",
         "exact": {"results": [{"entity_type": "experiment", "id": "e2", "name": "two"}],
                   "cursor": None, "error": None},
         "semantic": {"results": [], "cursor": None, "error": None}},
    ]
    server = _server(client)

    page1 = _call(server, "research_search", {"query": "dockq", "limit": 2})
    cursor = page1["next_cursor"]
    assert isinstance(cursor, str) and cursor

    page2 = _call(server, "research_search", {"query": "dockq", "limit": 2, "cursor": cursor})
    assert [r["id"] for r in page2["data"]["results"]] == ["e2"]
    # the packed token still carries the backend's own per-channel cursor
    assert app.search_requests[-1]["exact_cursor"] == "backend-exact-2"


def test_a_legacy_raw_json_cursor_is_still_honored(client, app):
    """Tokens minted before cursors were packed live in agent transcripts; refusing
    them would turn a stale cursor into an error instead of a page."""
    rid = _run_with_spans(client, app)
    legacy = json.dumps({"offset": 2, "view": "trajectory"}, sort_keys=True)
    service = ResearchReadService(ResearchOSSource(client))

    result = service.research_get(f"run:{rid}", view="trajectory", cursor=legacy)
    assert [s["id"] for s in result["data"]["spans"]][:1] == ["span-2"]
