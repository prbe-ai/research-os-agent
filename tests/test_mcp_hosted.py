"""Hosted MCP transport: stateless sessions and edge token validation.

The hosted service runs multiple replicas behind a load balancer with no session
affinity, so anything held in one pod's memory is unreachable from the next request.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from mcp.server.transport_security import TransportSecuritySettings

from probe.mcp.server import create_server, with_auth_and_health

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


# -- edge token verification -------------------------------------------------
async def _inner_ok(scope, receive, send) -> None:
    """Stands in for the MCP app: reaching it means the token passed the wrapper."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b'{"reached":true}'})


def _call(app, headers: list | None = None) -> dict:
    out: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode(): v.decode() for k, v in msg["headers"]}
        elif msg["type"] == "http.response.body":
            out["body"] = msg.get("body", b"")

    scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": headers or []}
    asyncio.run(app(scope, receive, send))
    return out


@pytest.fixture
def hosted_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROBE_MCP_OAUTH", "1")
    monkeypatch.setenv("PROBE_MCP_RESOURCE_URL", "https://mcp.test")
    monkeypatch.setenv("PROBE_MCP_AUTH_SERVER", "https://api.test")


def _bearer(token: str) -> list:
    return [(b"authorization", f"Bearer {token}".encode())]


def test_invalid_token_gets_401_not_a_200_tool_error(hosted_env) -> None:
    """A stale token must fail at the edge.

    Otherwise its tools load and every call fails inside an HTTP 200 — and the 401
    is what makes a client re-run its credential helper and retry.
    """

    async def rejects(token: str) -> bool:
        return True

    app = with_auth_and_health(_inner_ok, mcp_path="/mcp", token_rejected=rejects)
    res = _call(app, _bearer("probe_pat_revoked"))
    assert res["status"] == 401
    assert res["headers"]["www-authenticate"].startswith("Bearer ")
    assert b"reached" not in res["body"]


def test_valid_token_reaches_the_mcp_app(hosted_env) -> None:
    async def accepts(token: str) -> bool:
        return False

    app = with_auth_and_health(_inner_ok, mcp_path="/mcp", token_rejected=accepts)
    assert _call(app, _bearer("probe_pat_good"))["status"] == 200


def test_both_token_prefixes_are_accepted(hosted_env) -> None:
    """The prefix only discriminates; auth is a sha256 lookup. Legacy ros_pat_ lives."""
    seen: list[str] = []

    async def accepts(token: str) -> bool:
        seen.append(token)
        return False

    app = with_auth_and_health(_inner_ok, mcp_path="/mcp", token_rejected=accepts)
    for token in ("ros_pat_legacy", "probe_pat_current"):
        assert _call(app, _bearer(token))["status"] == 200
    assert seen == ["ros_pat_legacy", "probe_pat_current"]


# -- the real verifier ------------------------------------------------------
def _verifier_against(monkeypatch, *, status: int | None = None, exc: Exception | None = None):
    """Point _upstream_rejects at a mock transport instead of the live API."""
    import probe.mcp.server as server

    server._verify_cache.clear()

    def handle(request: httpx.Request) -> httpx.Response:
        if exc is not None:
            raise exc
        return httpx.Response(status, json={})

    real = httpx.AsyncClient

    class Mocked(real):
        def __init__(self, **kwargs):
            kwargs.pop("base_url", None)
            super().__init__(transport=httpx.MockTransport(handle), base_url="http://api.test", **kwargs)

    monkeypatch.setattr(server.httpx, "AsyncClient", Mocked)
    return server


@pytest.mark.parametrize(
    ("status", "exc", "rejected", "why"),
    [
        (200, None, False, "valid"),
        (401, None, True, "revoked"),
        (403, None, True, "forbidden"),
        (500, None, False, "server error must not disconnect everyone"),
        (None, httpx.ConnectError("down"), False, "API down must not disconnect everyone"),
        (None, httpx.ReadTimeout("slow"), False, "timeout must not disconnect everyone"),
    ],
)
def test_upstream_rejects_only_on_a_definitive_refusal(monkeypatch, status, exc, rejected, why) -> None:
    """Fail closed on 401/403; fail open on everything else. `why` documents each case."""
    server = _verifier_against(monkeypatch, status=status, exc=exc)
    assert asyncio.run(server._upstream_rejects("probe_pat_x")) is rejected, why


def test_a_rejection_is_cached_but_an_acceptance_is_not(monkeypatch) -> None:
    """Caching an accept would keep letting a just-revoked token through, suppressing
    the 401 that tells the client to re-run its helper and heal."""
    import probe.mcp.server as server

    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        # 401 for the dead token, 200 for the live one.
        bad = request.headers["Authorization"].endswith("dead")
        return httpx.Response(401 if bad else 200, json={})

    real = httpx.AsyncClient

    class Mocked(real):
        def __init__(self, **kwargs):
            kwargs.pop("base_url", None)
            super().__init__(transport=httpx.MockTransport(handle), base_url="http://api.test", **kwargs)

    monkeypatch.setattr(server.httpx, "AsyncClient", Mocked)
    server._verify_cache.clear()

    assert asyncio.run(server._upstream_rejects("probe_pat_dead")) is True
    assert asyncio.run(server._upstream_rejects("probe_pat_dead")) is True
    assert len(calls) == 1  # second rejection served from cache

    calls.clear()
    assert asyncio.run(server._upstream_rejects("probe_pat_live")) is False
    assert asyncio.run(server._upstream_rejects("probe_pat_live")) is False
    assert len(calls) == 2  # acceptance re-checked every time


def test_rotating_past_a_cached_rejection_is_not_delayed(monkeypatch) -> None:
    """A cached rejection must not bleed onto the replacement token.

    Rotation is the moment this matters: the revoked token is cached as dead, and the
    new one has to be believed immediately or the heal stalls for the whole TTL.
    """
    import probe.mcp.server as server

    def handle(request: httpx.Request) -> httpx.Response:
        revoked = request.headers["Authorization"].endswith("old")
        return httpx.Response(401 if revoked else 200, json={})

    real = httpx.AsyncClient

    class Mocked(real):
        def __init__(self, **kwargs):
            kwargs.pop("base_url", None)
            super().__init__(transport=httpx.MockTransport(handle), base_url="http://api.test", **kwargs)

    monkeypatch.setattr(server.httpx, "AsyncClient", Mocked)
    server._verify_cache.clear()

    assert asyncio.run(server._upstream_rejects("probe_pat_old")) is True
    assert len(server._verify_cache) == 1  # the dead one is remembered
    assert asyncio.run(server._upstream_rejects("probe_pat_new")) is False  # and not inherited


def test_verification_disabled_wires_no_verifier(hosted_env) -> None:
    """PROBE_MCP_VERIFY_TOKEN=0 (set for the whole suite in conftest) skips the check.

    Nothing is injected here, so reaching the inner app proves no upstream call was
    attempted — the escape hatch self-hosters use to avoid the extra round-trip.
    """
    app = with_auth_and_health(_inner_ok, mcp_path="/mcp")
    assert _call(app, _bearer("probe_pat_unchecked"))["status"] == 200
