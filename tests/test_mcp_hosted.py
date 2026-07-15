"""Hosted MCP transport: stateless sessions and edge token validation.

The hosted service runs multiple replicas behind a load balancer with no session
affinity, so anything held in one pod's memory is unreachable from the next request.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from mcp.server.transport_security import TransportSecuritySettings

from probe.mcp.server import create_server

_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
_HEADERS = {
    "Authorization": "Bearer probe_pat_test",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_OPEN = TransportSecuritySettings(
    enable_dns_rebinding_protection=False, allowed_hosts=["*"], allowed_origins=["*"]
)


def test_hosted_server_is_stateless() -> None:
    """A session would live in one pod's memory and 404 from every other replica."""
    assert create_server(transport_security=_OPEN).settings.stateless_http is True


def test_tools_list_works_without_a_session_id(service) -> None:
    """The multi-replica path: a follow-up request carries no session the pod knows.

    Statefully this is `400 Bad Request: Missing session ID` (and `Session not found`
    when the id came from a sibling pod) — the live failure this guards against.
    """

    async def run() -> tuple[int, str]:
        mcp = create_server(service, transport_security=_OPEN)
        mcp.settings.streamable_http_path = "/mcp"
        inner = mcp.streamable_http_app()
        async with inner.router.lifespan_context(inner):
            transport = httpx.ASGITransport(app=inner)
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as c:
                first = await c.post("/mcp", json=_INIT, headers=_HEADERS)
                assert first.status_code == 200
                # Stateless issues no session id at all; there is nothing to lose.
                assert first.headers.get("mcp-session-id") is None
                second = await c.post("/mcp", json=_LIST, headers=_HEADERS)
                return second.status_code, second.text

    status, body = asyncio.run(run())
    assert status == 200
    assert '"tools"' in body
    assert "Missing session ID" not in body


@pytest.fixture
def service(client):
    from probe.mcp.service import ResearchReadService
    from probe.mcp.source import ResearchOSSource

    return ResearchReadService(ResearchOSSource(client))
