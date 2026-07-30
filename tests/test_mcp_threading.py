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


@pytest.fixture(autouse=True)
def _clean_server_state():
    """Module-global teardown: a failing assert must not leak fake clients,
    leases, or parked sources into unrelated tests (limiters self-clean —
    the WeakKeyDictionary entry dies with each test's event loop)."""
    yield
    server_mod._clients.clear()
    server_mod._sources.clear()
    server_mod._in_flight.clear()
    server_mod._parked.clear()


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


# -- offload: the regression test ---------------------------------------------
def test_healthz_answers_while_a_tool_call_blocks() -> None:
    """THE 2026-07-30 regression: with inline dispatch this deadlocks (the loop
    is inside the tool body, so /healthz can never be served and the 2s
    wait_for trips). With the offload, healthz answers in milliseconds."""
    svc = BlockingService()

    async def run() -> tuple[int, float, httpx.Response]:
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
        def __init__(self, *, settings, fail_open, surface=None, transport=None):
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

    res_alpha, res_beta = asyncio.run(run())

    assert "probe_pat_alpha" in res_alpha.text and "probe_pat_beta" not in res_alpha.text
    assert "probe_pat_beta" in res_beta.text and "probe_pat_alpha" not in res_beta.text


# -- schema contract -----------------------------------------------------------
def test_tool_schemas_match_the_pre_offload_baseline() -> None:
    """The _tool wrapper relies on functools.wraps for FastMCP's schema
    generation; this pins every tool's schema byte-for-byte against the
    baseline captured from the pre-offload code (decbed3). A mismatch means
    agents see wrong/empty tool parameters while every probe stays green.

    Intentional schema changes re-capture the baseline (do NOT hand-edit):
      .venv/bin/python -c "import anyio, json; from probe.mcp.server import \
        create_server; t = anyio.run(create_server().list_tools); \
        open('tests/fixtures/mcp_tool_schemas.json','w').write(json.dumps( \
        sorted((x.model_dump(mode='json', exclude_none=True) for x in t), \
        key=lambda d: d['name']), indent=2, sort_keys=True) + chr(10))"
    """
    import inspect

    def normalized(tools: list[dict]) -> list[dict]:
        # Python 3.13 dedents docstrings at COMPILE time; 3.11/3.12 (the
        # deployed image is 3.12) keep raw indentation, so descriptions —
        # which FastMCP takes verbatim from __doc__ — differ across
        # interpreters by leading whitespace only. cleandoc both sides:
        # the pin stays byte-exact on every wrapper-sensitive field
        # (parameters, types, required) and whitespace-blind on prose.
        out = []
        for t in tools:
            t = dict(t)
            if "description" in t:
                t["description"] = inspect.cleandoc(t["description"])
            out.append(t)
        return out

    mcp = create_server()
    tools = anyio.run(mcp.list_tools)
    current = normalized(
        sorted(
            (t.model_dump(mode="json", exclude_none=True) for t in tools),
            key=lambda t: t["name"],
        )
    )
    expected = normalized(json.loads(_SCHEMA_BASELINE.read_text()))
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
        def __init__(self, *, settings, fail_open, surface=None, transport=None):
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


def test_two_generations_of_one_token_park_and_close_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """evict -> recreate -> evict-again of ONE token while generation 1 is
    still in flight: the park slot is keyed by instance, so the generations
    must never collide (D9 — the token-keyed design closed a live client)."""
    closed = _install_fake_factory(monkeypatch)
    monkeypatch.setattr(server_mod, "_MAX_CACHED_TOKENS", 1)

    open_leases: list = []
    try:
        lease_gen1 = _lease("tok-a")  # serial 1
        open_leases.append(lease_gen1)
        _resolve("tok-b")  # serial 2; cap 1 -> gen1 evicted while busy -> parked
        assert closed == []
        lease_gen2 = _lease("tok-a")  # serial 3; evicts idle tok-b (closed now)
        open_leases.append(lease_gen2)
        assert closed == [("tok-b", 2)]
        _resolve("tok-c")  # serial 4; evicts gen2 while busy -> parked
        assert len(server_mod._parked) == 2  # both generations parked, distinct slots

        lease_gen1.__exit__(None, None, None)
        open_leases.remove(lease_gen1)
        assert closed == [("tok-b", 2), ("tok-a", 1)]  # gen1 closed, gen2 still alive
        lease_gen2.__exit__(None, None, None)
        open_leases.remove(lease_gen2)
    finally:
        for ctx in open_leases:
            ctx.__exit__(None, None, None)
    assert closed == [("tok-b", 2), ("tok-a", 1), ("tok-a", 3)]
    assert server_mod._parked == {} and server_mod._in_flight == {}

# -- saturation breadcrumb -----------------------------------------------------
def test_saturation_warning_logged_when_a_call_waits(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A call that WAITS past _QUEUE_WARN_S (but under the shed timeout) must
    leave the breadcrumb log — the only server-side signal of a saturated
    pool short of an actual shed."""
    svc = BlockingService()
    monkeypatch.setattr(server_mod, "_TOOL_CAPACITY", 1)
    monkeypatch.setattr(server_mod, "_QUEUE_WARN_S", 0.05)

    async def run() -> httpx.Response:
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
                waiter = asyncio.create_task(
                    c.post("/mcp", json=_tool_call("browse_research", {}, 2), headers=_HEADERS)
                )
                await asyncio.sleep(0.2)  # waiter is now past _QUEUE_WARN_S in the queue
                svc.release.set()
                await slow
                return await waiter

    with caplog.at_level("WARNING", logger="probe.mcp.server"):
        second = asyncio.run(run())
    assert _result(second).get("isError") is not True
    assert any(
        "waited" in r.getMessage() and "browse_research" in r.getMessage()
        for r in caplog.records
    ), [r.getMessage() for r in caplog.records]


def test_shed_is_logged_server_side(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The shed itself must log: it is the strongest overload event and every
    probe stays green while it happens — a silent shed is invisible to
    operators for as long as the overload lasts."""
    svc = BlockingService()
    monkeypatch.setattr(server_mod, "_TOOL_CAPACITY", 1)
    monkeypatch.setattr(server_mod, "_QUEUE_TIMEOUT_S", 0.3)

    async def run() -> httpx.Response:
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
                shed = await c.post(
                    "/mcp", json=_tool_call("browse_research", {}, 2), headers=_HEADERS
                )
                svc.release.set()
                await slow
                return shed

    with caplog.at_level("WARNING", logger="probe.mcp.server"):
        shed = asyncio.run(run())
    assert _result(shed)["isError"] is True
    assert any("SHED" in r.getMessage() for r in caplog.records), [
        r.getMessage() for r in caplog.records
    ]


# -- admission slot hygiene ----------------------------------------------------
def test_admission_slot_is_released_after_a_tool_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tool body that raises must return its admission permit: at capacity 1
    a leaked slot would make the SECOND call shed instead of erroring."""
    monkeypatch.setattr(server_mod, "_TOOL_CAPACITY", 1)
    monkeypatch.setattr(server_mod, "_QUEUE_TIMEOUT_S", 1.0)

    class ExplodingService:
        def browse_research(self, **_kw: object) -> dict:
            raise RuntimeError("boom-sentinel")

    async def run() -> tuple[httpx.Response, httpx.Response]:
        mcp = create_server(ExplodingService(), transport_security=_OPEN)
        mcp.settings.streamable_http_path = "/mcp"
        inner = mcp.streamable_http_app()
        async with inner.router.lifespan_context(inner):
            transport = httpx.ASGITransport(app=inner)
            async with httpx.AsyncClient(transport=transport, base_url="http://mcp.test") as c:
                first = await c.post(
                    "/mcp", json=_tool_call("browse_research", {}, 1), headers=_HEADERS
                )
                second = await c.post(
                    "/mcp", json=_tool_call("browse_research", {}, 2), headers=_HEADERS
                )
                return first, second

    first, second = asyncio.run(run())
    assert "boom-sentinel" in _result(first)["content"][0]["text"]
    # A leaked slot turns this into "server overloaded" instead of the error.
    assert "boom-sentinel" in _result(second)["content"][0]["text"]


# -- structural enforcement ----------------------------------------------------
def test_every_registered_tool_is_offloaded() -> None:
    """A future @mcp.tool() added WITHOUT @_tool dispatches inline on the
    event loop again — silently reintroducing the 2026-07-30 incident while
    every other test keeps passing. FastMCP awaits async fns; sync fns run
    inline — so 'every registered tool is async' IS the offload guarantee."""
    mcp = create_server()
    tools = mcp._tool_manager.list_tools()
    assert tools, "no tools registered?"
    not_offloaded = [t.name for t in tools if not t.is_async]
    assert not_offloaded == [], (
        f"tools dispatching inline on the event loop (missing @_tool): {not_offloaded}"
    )


def test_grace_budget_covers_the_worst_case_call() -> None:
    """deploy/mcp/k8s.yaml's terminationGracePeriodSeconds is derived from SDK
    constants that live in this repo; if the transport budget grows past the
    manifest's grace ceiling, drains truncate exactly the calls the preStop
    exists to protect. This pins the arithmetic so drift fails CI."""
    import re

    from probe.sdk import transport

    manifest = (Path(__file__).parent.parent / "deploy" / "mcp" / "k8s.yaml").read_text()
    grace = int(re.search(r"terminationGracePeriodSeconds:\s*(\d+)", manifest).group(1))
    pre_stop = int(re.search(r"preStop:\n\s*sleep:\n\s*seconds:\s*(\d+)", manifest).group(1))

    worst_call = (transport._MAX_RETRIES + 1) * transport._DEFAULT_TIMEOUT
    needed = pre_stop + worst_call + server_mod._QUEUE_TIMEOUT_S
    assert grace >= needed, (
        f"grace {grace}s < preStop {pre_stop}s + worst call {worst_call:.0f}s "
        f"+ queue wait {server_mod._QUEUE_TIMEOUT_S:.0f}s = {needed:.0f}s"
    )
