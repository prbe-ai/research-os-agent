"""The `corpora` -> `search_in` rename, pinned AT THE MCP TOOL-CALL BOUNDARY.

Every other search test in this suite calls ``ResearchReadService`` directly, so
none of them prove anything about how FastMCP binds arguments. That distinction
is the whole point here.

FastMCP builds its argument model from the function signature with no
``extra="forbid"``, so pydantic's default ``extra="ignore"`` applies: a tool that
simply DROPPED `corpora` would let a stale caller through with the parameter
silently discarded and an unfiltered search returned as success. Verified before
this landed -- a tool declaring only `search_in`, called with
`corpora=["transcripts"]`, ran with ``search_in=None`` and raised nothing.

So `corpora` stays BOUND in the signature purely to reject. These tests assert
the MCP-OBSERVABLE contract, not an internal exception: tool failures travel as
an ``isError`` result inside HTTP 200 (see test_mcp_threading.py's error
contract), never as a transport-level status.
"""

from __future__ import annotations

import asyncio
import json

import httpx
from mcp.server.transport_security import TransportSecuritySettings

from probe.mcp.server import create_server

_OPEN = TransportSecuritySettings(
    enable_dns_rebinding_protection=False, allowed_hosts=["*"], allowed_origins=["*"]
)
_HEADERS = {
    "Authorization": "Bearer probe_pat_test",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


class _RecordingService:
    """Captures what the tool body actually received, so a silently-dropped
    argument is visible rather than inferred from a green assertion."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search_knowledge(self, query: str, **kwargs: object) -> dict:
        self.calls.append({"query": query, **kwargs})
        return {"data": {"results": []}}


def _tool_call(name: str, arguments: dict, req_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def _call(svc: object, arguments: dict) -> dict:
    async def run() -> httpx.Response:
        mcp = create_server(svc, transport_security=_OPEN)
        mcp.settings.streamable_http_path = "/mcp"
        inner = mcp.streamable_http_app()
        async with inner.router.lifespan_context(inner):
            transport = httpx.ASGITransport(app=inner)
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as c:
                return await c.post(
                    "/mcp", json=_tool_call("search_knowledge", arguments), headers=_HEADERS
                )

    response = asyncio.run(run())
    assert response.status_code == 200, response.text
    body = response.text
    # streamable-http may frame the JSON-RPC body as SSE; take the data line.
    if body.lstrip().startswith("event:") or "\ndata: " in body:
        body = next(ln[len("data: ") :] for ln in body.splitlines() if ln.startswith("data: "))
    payload = json.loads(body)
    assert "result" in payload, payload
    return payload["result"]


def _error_text(result: dict) -> str:
    assert result.get("isError") is True, result
    return "".join(part.get("text", "") for part in result.get("content", []))


def test_the_old_name_is_rejected_at_the_boundary_not_silently_dropped() -> None:
    """The regression this whole shim exists for.

    A stale caller must NOT get a green, unfiltered search. It must get an error
    that names the replacement, because a search which silently answers a
    different question than it was asked is worse than one that errors.
    """
    svc = _RecordingService()
    result = _call(svc, {"query": "q", "corpora": ["transcripts"]})
    text = _error_text(result)
    assert "search_in" in text, text
    assert "corpora" in text, text
    # And crucially: the body never ran, so no unfiltered search was performed.
    assert svc.calls == []


def test_the_old_name_is_rejected_even_when_the_new_one_is_also_given() -> None:
    """A half-migrated caller naming BOTH vocabularies has not expressed one
    intent. Honouring either silently would reintroduce the very drop the guard
    exists to prevent, so the rule is `corpora is not None`, full stop -- not
    `search_in is None and corpora is not None`."""
    svc = _RecordingService()
    result = _call(svc, {"query": "q", "corpora": ["transcripts"], "search_in": ["documents"]})
    assert "search_in" in _error_text(result)
    assert svc.calls == []


def test_the_new_name_binds_through_fastmcp() -> None:
    """The positive control. Without this, the rejection tests above would still
    pass against a tool that had no working parameter at all."""
    svc = _RecordingService()
    result = _call(svc, {"query": "q", "search_in": ["documents"]})
    assert result.get("isError") is not True, result
    assert svc.calls[0]["search_in"] == ["documents"]
    # The dead name never reaches the service: it is bound and rejected on the
    # tool surface, so the internal API has no knowledge of it at all.
    assert "corpora" not in svc.calls[0]


def test_omitting_both_is_an_unfiltered_search_not_an_error() -> None:
    """`corpora=None` is the DEFAULT, so the guard must key on `is not None`
    rather than falsiness -- otherwise every ordinary call raises."""
    svc = _RecordingService()
    result = _call(svc, {"query": "q"})
    assert result.get("isError") is not True, result
    assert svc.calls[0]["search_in"] is None


def test_the_dead_parameter_is_advertised_as_deprecated() -> None:
    """It has to stay in the schema to be rejectable, so it must at least be
    marked -- otherwise a FRESH agent reads the schema, picks `corpora`, and the
    shim manufactures the failure it was added to catch."""
    import anyio

    tools = anyio.run(create_server(_RecordingService()).list_tools)
    schema = next(t for t in tools if t.name == "search_knowledge").inputSchema
    assert schema["properties"]["corpora"]["deprecated"] is True
    assert "search_in" in schema["properties"]["corpora"]["description"]
    assert "search_in" in schema["properties"]
