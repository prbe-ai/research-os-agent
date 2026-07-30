"""Threaded tool execution: the event loop must never block on a tool call.

2026-07-30 incident: FastMCP dispatches sync tools INLINE on the event loop
(mcp func_metadata calls ``fn(...)`` directly), so one slow backend round-trip
starved ``/healthz`` and the kubelet liveness-killed the pod mid-rollout —
callers saw nginx 502s. ``server._tool`` moves tool bodies onto worker threads
and bounds admission. These tests pin that contract:

- ``/healthz`` answers while a tool call is mid-flight          (offload)
- a saturated worker pool sheds with a retryable error          (load shed)
- concurrent callers with different bearers never cross         (isolation)
- tool schemas are byte-identical to the pre-offload baseline   (wraps contract)
- an error raised on the worker thread keeps today's shape      (error contract)
- LRU eviction never closes a client another call is using      (lease guard)
"""

from __future__ import annotations

import asyncio
import itertools
import json
import threading
import time
from pathlib import Path

import anyio
import httpx
import pytest
from mcp.server.transport_security import TransportSecuritySettings

import probe.mcp.server as server_mod
from probe.mcp.server import create_server, with_auth_and_health

_OPEN = TransportSecuritySettings(
    enable_dns_rebinding_protection=False, allowed_hosts=["*"], allowed_origins=["*"]
)
_HEADERS = {
    "Authorization": "Bearer probe_pat_test",
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}
_SCHEMA_BASELINE = Path(__file__).parent / "fixtures" / "mcp_tool_schemas.json"


def _tool_call(name: str, arguments: dict, req_id: int = 1) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


def _result(response: httpx.Response) -> dict:
    body = response.json()
    assert "result" in body, body
    return body["result"]


class BlockingService:
    """A service whose ``browse_research`` parks on an event until released.

    ``entered`` proves the body is executing (on its worker thread) — the
    tests use it to sequence "while a tool call is mid-flight" assertions
    without sleeps."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def browse_research(self, **_kw: object) -> dict:
        self.calls += 1
        self.entered.set()
        assert self.release.wait(timeout=30), "test never released the blocked tool"
        return {"ok": True}


def _fresh_loop_state() -> None:
    # anyio limiters bind to the loop they first await on; each test runs its
    # own asyncio.run loop, and a dead loop's id() can be reused by a new one.
    server_mod._limiters_by_loop.clear()


# -- offload: the regression test ---------------------------------------------
def test_healthz_answers_while_a_tool_call_blocks() -> None:
    """THE 2026-07-30 regression: with inline dispatch this deadlocks (the loop
    is inside the tool body, so /healthz can never be served and the 2s
    wait_for trips). With the offload, healthz answers in milliseconds."""
    svc = BlockingService()

    async def run() -> tuple[int, float, httpx.Response]:
        _fresh_loop_state()
        mcp = create_server(svc, transport_security=_OPEN)
        mcp.settings.streamable_http_path = "/mcp"
        inner = mcp.streamable_http_app()
        app = with_auth_and_health(inner, mcp_path="/mcp")
        async with inner.router.lifespan_context(inner):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as c:
                slow = asyncio.create_task(
                    c.post("/mcp", json=_tool_call("browse_research", {}), headers=_HEADERS)
                )
                await asyncio.to_thread(svc.entered.wait, 10)
                start = time.monotonic()
                health = await asyncio.wait_for(c.get("/healthz"), timeout=2.0)
                elapsed = time.monotonic() - start
                svc.release.set()
                return health.status_code, elapsed, await slow

    status, elapsed, tool_response = asyncio.run(run())
    assert status == 200
    assert elapsed < 1.0, f"/healthz took {elapsed:.2f}s while a tool call was in flight"
    assert tool_response.status_code == 200
    assert _result(tool_response).get("isError") is not True


# -- load shed -----------------------------------------------------------------
def test_saturated_pool_sheds_with_a_retryable_overloaded_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queueing past the admission timeout must fail fast with a retryable
    "overloaded" tool error — not ride toward the ingress's 300s timeout —
    and the shed call must never reach the service."""
    svc = BlockingService()
    monkeypatch.setattr(server_mod, "_TOOL_CAPACITY", 1)
    monkeypatch.setattr(server_mod, "_QUEUE_TIMEOUT_S", 0.5)
    monkeypatch.setattr(server_mod, "_QUEUE_WARN_S", 0.05)

    async def run() -> tuple[httpx.Response, float, httpx.Response, httpx.Response]:
        _fresh_loop_state()
        mcp = create_server(svc, transport_security=_OPEN)
        mcp.settings.streamable_http_path = "/mcp"
        inner = mcp.streamable_http_app()
        async with inner.router.lifespan_context(inner):
            transport = httpx.ASGITransport(app=inner)
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as c:
                slow = asyncio.create_task(
                    c.post("/mcp", json=_tool_call("browse_research", {}, 1), headers=_HEADERS)
                )
                await asyncio.to_thread(svc.entered.wait, 10)
                start = time.monotonic()
                shed = await c.post(
                    "/mcp", json=_tool_call("browse_research", {}, 2), headers=_HEADERS
                )
                shed_elapsed = time.monotonic() - start
                svc.release.set()
                first = await slow
                retry = await c.post(
                    "/mcp", json=_tool_call("browse_research", {}, 3), headers=_HEADERS
                )
                return shed, shed_elapsed, first, retry

    shed, shed_elapsed, first, retry = asyncio.run(run())

    result = _result(shed)
    assert result["isError"] is True
    text = result["content"][0]["text"]
    assert "overloaded" in text and "retry" in text, text
    assert 0.3 < shed_elapsed < 5.0, f"shed after {shed_elapsed:.2f}s, wanted ~0.5s"
    # The blocked call and the post-release retry both succeed; the shed call
    # never reached the service.
    assert _result(first).get("isError") is not True
    assert _result(retry).get("isError") is not True
    assert svc.calls == 2


# -- cross-tenant isolation ----------------------------------------------------
def test_concurrent_callers_with_different_tokens_never_cross(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two simultaneous requests with different bearers must each resolve their
    OWN client on their own worker thread — the contextvar token must survive
    the thread hop. The barrier forces true overlap: the test cannot pass by
    serializing the calls."""
    barrier = threading.Barrier(2)

    class FakeClient:
        def __init__(self, *, settings, fail_open, surface=None):
            self.token = settings.token

        def close(self) -> None:
            pass

    class FakeSource:
        def __init__(self, client):
            self.client = client

        def close(self) -> None:
            self.client.close()

    class FakeService:
        def __init__(self, source):
            self.source = source

        def browse_research(self, **_kw: object) -> dict:
            barrier.wait(timeout=10)
            return {"token_seen": self.source.client.token}

    monkeypatch.setattr(server_mod, "Client", FakeClient)
    monkeypatch.setattr(server_mod, "ResearchOSSource", FakeSource)
    monkeypatch.setattr(server_mod, "ResearchReadService", FakeService)
    server_mod._clients.clear()
    server_mod._sources.clear()

    async def run() -> tuple[httpx.Response, httpx.Response]:
        _fresh_loop_state()
        mcp = create_server(transport_security=_OPEN)
        mcp.settings.streamable_http_path = "/mcp"
        inner = mcp.streamable_http_app()
        app = with_auth_and_health(inner, mcp_path="/mcp")
        async with inner.router.lifespan_context(inner):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as c:

                def headers(token: str) -> dict:
                    return {**_HEADERS, "Authorization": f"Bearer {token}"}

                return await asyncio.gather(
                    c.post(
                        "/mcp",
                        json=_tool_call("browse_research", {}, 1),
                        headers=headers("probe_pat_alpha"),
                    ),
                    c.post(
                        "/mcp",
                        json=_tool_call("browse_research", {}, 2),
                        headers=headers("probe_pat_beta"),
                    ),
                )

    try:
        res_alpha, res_beta = asyncio.run(run())
    finally:
        server_mod._clients.clear()
        server_mod._sources.clear()

    assert "probe_pat_alpha" in res_alpha.text and "probe_pat_beta" not in res_alpha.text
    assert "probe_pat_beta" in res_beta.text and "probe_pat_alpha" not in res_beta.text


# -- schema contract -----------------------------------------------------------
def test_tool_schemas_match_the_pre_offload_baseline() -> None:
    """The _tool wrapper relies on functools.wraps for FastMCP's schema
    generation; this pins every tool's schema byte-for-byte against the
    baseline captured from the pre-offload code (decbed3). A mismatch means
    agents see wrong/empty tool parameters while every probe stays green."""
    mcp = create_server()
    tools = anyio.run(mcp.list_tools)
    current = sorted(
        (t.model_dump(mode="json", exclude_none=True) for t in tools),
        key=lambda t: t["name"],
    )
    expected = json.loads(_SCHEMA_BASELINE.read_text())
    assert current == expected


# -- error contract ------------------------------------------------------------
def test_an_error_raised_on_the_worker_thread_keeps_its_shape() -> None:
    """A backend failure inside the threaded body must surface exactly as it
    did with inline dispatch: an isError tool result carrying the message,
    inside an HTTP 200 — never a 500, never a swallowed error."""

    class ExplodingService:
        def browse_research(self, **_kw: object) -> dict:
            raise RuntimeError("boom-sentinel")

    async def run() -> httpx.Response:
        _fresh_loop_state()
        mcp = create_server(ExplodingService(), transport_security=_OPEN)
        mcp.settings.streamable_http_path = "/mcp"
        inner = mcp.streamable_http_app()
        async with inner.router.lifespan_context(inner):
            transport = httpx.ASGITransport(app=inner)
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as c:
                return await c.post(
                    "/mcp", json=_tool_call("browse_research", {}), headers=_HEADERS
                )

    response = asyncio.run(run())
    assert response.status_code == 200
    result = _result(response)
    assert result["isError"] is True
    assert "boom-sentinel" in result["content"][0]["text"]


# -- lease guard ---------------------------------------------------------------
def _install_fake_factory(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """Point the per-token factory at counting fakes; returns the close log."""
    closed: list[tuple[str, int]] = []
    serial = itertools.count(1)

    class FakeClient:
        def __init__(self, *, settings, fail_open, surface=None):
            self.token = settings.token
            self.serial = next(serial)

        def close(self) -> None:
            closed.append((self.token, self.serial))

    class FakeSource:
        def __init__(self, client):
            self.client = client

        def close(self) -> None:
            self.client.close()

    monkeypatch.setattr(server_mod, "Client", FakeClient)
    monkeypatch.setattr(server_mod, "ResearchOSSource", FakeSource)
    server_mod._clients.clear()
    server_mod._sources.clear()
    server_mod._in_flight.clear()
    server_mod._parked.clear()
    return closed


def _resolve(token: str) -> None:
    reset = server_mod._token_var.set(token)
    try:
        server_mod._service_from_token()
    finally:
        server_mod._token_var.reset(reset)


def _lease(token: str):
    reset = server_mod._token_var.set(token)
    try:
        ctx = server_mod._leased_service()
        ctx.__enter__()
        return ctx
    finally:
        server_mod._token_var.reset(reset)


def test_eviction_parks_a_busy_source_and_closes_it_on_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Evicting a token whose call is mid-flight must NOT close its client —
    it parks, and the last lease release closes it."""
    closed = _install_fake_factory(monkeypatch)
    monkeypatch.setattr(server_mod, "_MAX_CACHED_TOKENS", 2)

    lease_a = _lease("tok-a")  # in flight
    try:
        _resolve("tok-b")
        _resolve("tok-c")  # cap 2 -> tok-a (oldest) evicted while leased
        assert "tok-a" not in server_mod._sources
        assert closed == []  # parked, NOT closed: a thread is mid-request on it
        assert len(server_mod._parked) == 1
    finally:
        lease_a.__exit__(None, None, None)

    assert closed == [("tok-a", 1)]  # last release closed it
    assert server_mod._parked == {} and server_mod._in_flight == {}
    server_mod._clients.clear()
    server_mod._sources.clear()


def test_two_generations_of_one_token_park_and_close_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evict -> recreate -> evict-again of ONE token while generation 1 is
    still in flight: the park slot is keyed by instance, so the generations
    must never collide (D9 — the token-keyed design closed a live client)."""
    closed = _install_fake_factory(monkeypatch)
    monkeypatch.setattr(server_mod, "_MAX_CACHED_TOKENS", 1)

    lease_gen1 = _lease("tok-a")  # serial 1
    _resolve("tok-b")  # serial 2; cap 1 -> gen1 evicted while busy -> parked
    assert closed == []
    lease_gen2 = _lease("tok-a")  # serial 3; evicts idle tok-b (closed now)
    assert closed == [("tok-b", 2)]
    _resolve("tok-c")  # serial 4; evicts gen2 while busy -> parked
    assert len(server_mod._parked) == 2  # both generations parked, distinct slots

    lease_gen1.__exit__(None, None, None)
    assert closed == [("tok-b", 2), ("tok-a", 1)]  # gen1 closed, gen2 still alive
    lease_gen2.__exit__(None, None, None)
    assert closed == [("tok-b", 2), ("tok-a", 1), ("tok-a", 3)]
    assert server_mod._parked == {} and server_mod._in_flight == {}
    server_mod._clients.clear()
    server_mod._sources.clear()
