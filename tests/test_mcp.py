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
    out = service.research_search(
        "adam sweep", corpora=["documents"], workspace_id="ws-1", collapse=None
    )

    # corpora vocabulary maps onto backend corpus values (documents -> github+files,
    # experiments always searched); the workspace lens rides along and the limit
    # budget is split per channel (limit=8 -> 4+4, no post-merge truncation).
    body = app.search_requests[-1]
    assert body["query"] == "adam sweep"
    assert body["corpus"] == ["experiments", "files", "github"]
    assert body["workspace_id"] == "ws-1"
    assert body["top_k"] == 4 and body["exact_limit"] == 4

    # merged result list keeps the tool contract with per-channel provenance
    results = out["data"]["results"]
    assert [r["why_matched"]["channel"] for r in results] == ["exact", "semantic", "exact"]
    assert results[0]["entity_type"] == "experiment"
    assert results[0]["resource"] == "research://experiments/e-1/card"
    assert results[0]["why_matched"] == {
        "mode": "exact", "channel": "exact", "score": 0.91, "terms": [],
    }
    assert results[1]["entity_type"] == "file" and results[1]["id"] == "f-1"
    assert results[1]["card"]["snippet"] == "adam beta2 ..."
    assert results[2]["entity_type"] == "artifact" and results[2]["card"]["run_id"] == "r-1"

    assert out["completeness"] == {"state": "complete", "missing": []}
    assert out["capabilities"]["unified_search"] is True
    assert out["capabilities"]["semantic_search"] is True
    assert out["capabilities"]["kb_documents"] is True

    # Per-channel backend cursors ride one opaque tool cursor, round-trippable.
    # Opaque means opaque: this used to assert json.loads(next_cursor) == {...},
    # pinning the cursor to raw JSON -- the very thing that made it unusable through
    # the MCP tool layer (FastMCP pre-parses a JSON-object string arg into a dict,
    # which then fails `cursor: str`). What matters is the ROUND TRIP, below; the
    # encoding is nobody's business. See tests/test_mcp_tool_layer.py.
    with pytest.raises(json.JSONDecodeError):
        json.loads(out["next_cursor"])
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
    assert out["next_cursor"] is None  # the fallback cannot paginate


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
    out = service.research_search("folding", collapse=None)
    assert out["completeness"] == {"state": "partial", "missing": ["semantic_search"]}
    assert out["data"]["channels"]["semantic"]["error"] == "engine_timeout"
    assert [r["entity_type"] for r in out["data"]["results"]] == ["project"]
    assert out["data"]["results"][0]["resource"] == "research://projects/p-1/card"
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


def test_search_emits_every_fetched_row_when_channels_overflow(client, app):
    # More rows come back than `limit`: nothing may be silently dropped, and
    # with no backend cursors the response must not pretend to paginate.
    exact = [
        {"entity_type": "experiment", "id": f"e-{i}", "name": f"exp {i}", "slug": f"exp-{i}",
         "workspace_id": None, "project_id": "p-1", "experiment_id": None, "run_id": None,
         "score": 1.0 - i / 10}
        for i in range(3)
    ]
    semantic = [
        {"doc_id": f"file:f-{i}", "title": f"doc {i}", "snippet": "...", "score": 0.9 - i / 10,
         "source_system": "workspace", "source_url": None, "ref": {"kind": "file", "id": f"f-{i}"}}
        for i in range(3)
    ]
    app.search_response = _search_response(exact=exact, semantic=semantic)
    service = ResearchReadService(ResearchOSSource(client))
    out = service.research_search("q", limit=4, collapse=None)
    body = app.search_requests[-1]
    assert body["top_k"] == 2 and body["exact_limit"] == 2  # split budget
    assert len(out["data"]["results"]) == 6  # emitted == fetched, no truncation
    assert out["next_cursor"] is None


def test_search_two_page_walk_skips_and_duplicates_nothing(client, app):
    def exact_row(i):
        return {"entity_type": "experiment", "id": f"e-{i}", "name": f"exp {i}",
                "slug": f"exp-{i}", "workspace_id": None, "project_id": "p-1",
                "experiment_id": None, "run_id": None, "score": 1.0 - i / 10}

    def semantic_row(i):
        return {"doc_id": f"file:f-{i}", "title": f"doc {i}", "snippet": "...",
                "score": 0.9 - i / 10, "source_system": "workspace", "source_url": None,
                "ref": {"kind": "file", "id": f"f-{i}"}}

    app.search_responses = [
        _search_response(
            exact=[exact_row(0), exact_row(1)], semantic=[semantic_row(0), semantic_row(1)],
            exact_cursor="ex-2", semantic_cursor="se-2",
        ),
        _search_response(exact=[exact_row(2)], semantic=[semantic_row(2)]),
    ]
    service = ResearchReadService(ResearchOSSource(client))
    page1 = service.research_search("q", limit=4, collapse=None)
    assert len(page1["data"]["results"]) == 4
    page2 = service.research_search("q", limit=4, collapse=None, cursor=page1["next_cursor"])
    assert app.search_requests[-1]["exact_cursor"] == "ex-2"
    assert app.search_requests[-1]["semantic_cursor"] == "se-2"
    ids = [r["id"] for r in page1["data"]["results"] + page2["data"]["results"]]
    assert sorted(ids) == ["e-0", "e-1", "e-2", "f-0", "f-1", "f-2"]  # nothing skipped
    assert len(set(ids)) == len(ids)  # nothing duplicated
    assert page2["next_cursor"] is None


def test_search_collapse_experiment_dedupes_across_channels(client, app):
    app.search_response = _search_response(
        exact=[
            {"entity_type": "experiment", "id": "e-1", "name": "adam", "slug": "adam",
             "workspace_id": None, "project_id": "p-1", "experiment_id": None,
             "run_id": None, "score": 0.7},
            {"entity_type": "project", "id": "p-1", "name": "folding", "slug": "folding",
             "workspace_id": None, "project_id": None, "experiment_id": None,
             "run_id": None, "score": 0.95},
        ],
        semantic=[
            {"doc_id": "experiment:e-1", "title": "adam card", "snippet": "...",
             "score": 0.9, "source_system": "experiments", "source_url": None,
             "ref": {"kind": "experiment", "id": "e-1"}},
            {"doc_id": "file:f-1", "title": "notes", "snippet": "...", "score": 0.8,
             "source_system": "workspace", "source_url": None,
             "ref": {"kind": "file", "id": "f-1"}},
        ],
    )
    service = ResearchReadService(ResearchOSSource(client))

    # default collapse="experiment": one deduped experiment-level result, keeping
    # the best-scoring representative's channel provenance (semantic, 0.9 > 0.7)
    out = service.research_search("adam")
    results = out["data"]["results"]
    assert [r["id"] for r in results] == ["e-1"]
    assert results[0]["entity_type"] == "experiment"
    assert results[0]["why_matched"]["channel"] == "semantic"
    assert results[0]["why_matched"]["score"] == 0.9

    # collapse=None keeps the heterogeneous merged view
    out = service.research_search("adam", collapse=None)
    assert {r["entity_type"] for r in out["data"]["results"]} == {
        "experiment", "project", "file",
    }
    assert len(out["data"]["results"]) == 4


def test_search_workspace_scope_rejected_on_pre_search_backend(client, app):
    # A pre-search server has no workspaces; silently returning tenant-wide
    # results would be worse than failing loudly.
    service = ResearchReadService(ResearchOSSource(client))
    with pytest.raises(errors.ValidationError):
        service.research_search("q", workspace_id="ws-1")


def test_keyword_fallback_ignores_incoming_cursor(client, app):
    # Version skew: a packed /v1/search cursor arrives at a pre-search server.
    # Echoing it back would make cursor-following consumers loop forever.
    service = ResearchReadService(ResearchOSSource(client))
    out = service.research_search("q", cursor='{"exact": "ex-2"}')
    assert out["data"]["results"] == []
    assert out["next_cursor"] is None


def test_search_project_filter_scopes_exact_and_marks_semantic(client, app):
    app.search_response = _search_response(
        exact=[
            {"entity_type": "experiment", "id": "e-1", "name": "in", "slug": "in",
             "workspace_id": None, "project_id": "p-1", "experiment_id": None,
             "run_id": None, "score": 0.9},
            {"entity_type": "experiment", "id": "e-2", "name": "out", "slug": "out",
             "workspace_id": None, "project_id": "p-2", "experiment_id": None,
             "run_id": None, "score": 0.8},
            {"entity_type": "project", "id": "p-1", "name": "proj", "slug": "proj",
             "workspace_id": None, "project_id": None, "experiment_id": None,
             "run_id": None, "score": 0.7},
            {"entity_type": "artifact", "id": "a-1", "name": "unlinked.csv", "slug": None,
             "workspace_id": None, "project_id": None, "experiment_id": None,
             "run_id": "r-9", "score": 0.6},
        ],
        semantic=[
            {"doc_id": "file:f-1", "title": "doc", "snippet": "...", "score": 0.9,
             "source_system": "workspace", "source_url": None,
             "ref": {"kind": "file", "id": "f-1"}},
        ],
        semantic_cursor="se-2",
    )
    service = ResearchReadService(ResearchOSSource(client))
    out = service.research_search("q", filters={"project_id": "p-1"}, collapse=None)
    # only in-project exact hits survive (the un-linked artifact is dropped
    # conservatively); the semantic channel is excluded and marked, and its
    # cursor is NOT advanced past rows that were never emitted
    assert [r["id"] for r in out["data"]["results"]] == ["e-1", "p-1"]
    assert out["data"]["channels"]["semantic"]["error"] == "project_scope_unsupported"
    assert "semantic_search" in out["completeness"]["missing"]
    assert out["completeness"]["state"] == "partial"
    assert out["next_cursor"] is None


def test_keyword_fallback_scopes_by_project(client, app):
    # pre-search server (no search_response): the fallback keeps project scoping
    p1 = client.ensure_project("proj-one")
    client.ensure_project("proj-two")
    client.run(project="proj-one", experiment="exp-one", hypothesis="h1", name="r1")
    client.run(project="proj-two", experiment="exp-two", hypothesis="h2", name="r2")
    service = ResearchReadService(ResearchOSSource(client))
    out = service.research_search("exp", filters={"project_id": p1["id"]})
    names = [r["card"]["name"] for r in out["data"]["results"]]
    assert names == ["exp-one"]


def test_why_matched_shape_is_uniform_across_channels(client, app):
    expected_keys = {"mode", "channel", "score", "terms"}
    app.search_response = _search_response(
        exact=[
            {"entity_type": "experiment", "id": "e-1", "name": "adam", "slug": "adam",
             "workspace_id": None, "project_id": "p-1", "experiment_id": None,
             "run_id": None, "score": 0.7},
        ],
        semantic=[
            {"doc_id": "file:f-1", "title": "notes", "snippet": "...", "score": 0.8,
             "source_system": "workspace", "source_url": None,
             "ref": {"kind": "file", "id": "f-1"}},
        ],
    )
    service = ResearchReadService(ResearchOSSource(client))
    out = service.research_search("adam", collapse=None)
    assert out["data"]["results"]
    for row in out["data"]["results"]:
        assert set(row["why_matched"]) == expected_keys

    # and the keyword fallback emits the same superset (terms populated)
    from tests.conftest import FakeApp, make_client

    app2 = FakeApp()
    client2 = make_client(app2)
    client2.ensure_project("p")
    client2.run(project="p", experiment="kw-exp", hypothesis="h", name="r")
    service2 = ResearchReadService(ResearchOSSource(client2))
    rows = service2.research_search("kw-exp")["data"]["results"]
    assert rows
    for row in rows:
        assert set(row["why_matched"]) == expected_keys
    assert rows[0]["why_matched"]["terms"] == ["kw-exp"]
    assert rows[0]["why_matched"]["channel"] == "keyword"


def test_unsupported_verdict_short_circuits_then_expires(client, app):
    service = ResearchReadService(ResearchOSSource(client))
    source = service.source

    service.research_search("q")  # 404 -> probe 404 -> cached unsupported
    assert len(app.search_requests) == 2
    service.research_search("q")  # fresh verdict short-circuits: no doomed POST
    assert len(app.search_requests) == 2

    # after the recheck window the verdict expires and the upgrade is noticed
    app.search_response = _search_response()
    source._search_checked_at -= 301
    out = service.research_search("q")
    assert len(app.search_requests) == 3
    assert out["capabilities"]["unified_search"] is True


def test_search_retries_once_on_stale_pod_404(client, app):
    app.search_response = _search_response(
        exact=[
            {"entity_type": "experiment", "id": "e-1", "name": "adam", "slug": "adam",
             "workspace_id": None, "project_id": "p-1", "experiment_id": None,
             "run_id": None, "score": 0.7},
        ],
    )
    service = ResearchReadService(ResearchOSSource(client))
    service.research_search("q")  # establishes supported=True (1 request)
    app.search_404_once = True  # one stale pod mid rolling deploy
    out = service.research_search("q")  # 404 -> probe ok -> retried once
    assert len(app.search_requests) == 4
    assert [r["id"] for r in out["data"]["results"]] == ["e-1"]
    assert out["capabilities"]["unified_search"] is True


def test_malformed_cursor_raises_validation_error(client, app):
    app.search_response = _search_response()
    service = ResearchReadService(ResearchOSSource(client))
    with pytest.raises(errors.ValidationError):
        service.research_search("q", cursor="not-a-packed-cursor")
    with pytest.raises(errors.ValidationError):
        service.research_search("q", cursor='["wrong-shape"]')


def test_search_unknown_collapse_rejected(client, app):
    # only "experiment" (dedupe) and null (heterogeneous) are defined; anything
    # else must fail loudly instead of silently falling through
    app.search_response = _search_response()
    service = ResearchReadService(ResearchOSSource(client))
    with pytest.raises(errors.ValidationError):
        service.research_search("q", collapse="run")
    assert app.search_requests == []  # rejected before any backend call


def test_token_factory_evicts_oldest_pair_beyond_cap(monkeypatch):
    from probe.mcp import server as server_mod

    server_mod._clients.clear()
    server_mod._sources.clear()
    monkeypatch.setattr(server_mod, "_MAX_CACHED_TOKENS", 3)
    closed: list[str] = []

    class FakeClient:
        def __init__(self, token):
            self.token = token

        def close(self):
            closed.append(self.token)

    monkeypatch.setattr(server_mod, "Client", lambda token, fail_open: FakeClient(token))

    def resolve(token):
        reset = server_mod._token_var.set(token)
        try:
            return server_mod._service_from_token()
        finally:
            server_mod._token_var.reset(reset)

    try:
        for token in ("t1", "t2", "t3"):
            resolve(token)
        resolve("t1")  # refresh t1's recency: t2 is now the stalest
        resolve("t4")  # exceeds the cap -> t2 evicted, its client closed
        assert closed == ["t2"]
        assert list(server_mod._sources) == ["t3", "t1", "t4"]
        assert list(server_mod._clients) == list(server_mod._sources)  # kept in step
        # an evicted token re-creates cleanly (and evicts the next stalest)
        service = resolve("t2")
        assert service.source.client.token == "t2"
        assert closed == ["t2", "t3"]
        assert list(server_mod._sources) == ["t1", "t4", "t2"]
        assert list(server_mod._clients) == list(server_mod._sources)
    finally:
        server_mod._clients.clear()
        server_mod._sources.clear()


def test_search_malformed_response_degrades_to_partial(client, app):
    # A broken proxy/server returning garbage must degrade, never AttributeError.
    app.search_response = {"state": "ok", "exact": "broken", "semantic": ["nope"]}
    service = ResearchReadService(ResearchOSSource(client))
    out = service.research_search("q", collapse=None)
    assert out["data"]["results"] == []
    assert out["completeness"]["state"] == "partial"
    assert out["data"]["channels"]["exact"]["error"] == "malformed_response"
    assert out["data"]["channels"]["semantic"]["error"] == "malformed_response"

    # wrong-typed rows inside a well-typed section are filtered and marked
    app.search_response = {
        "state": "ok",
        "exact": {"results": [123, {"entity_type": "experiment", "id": "e-1", "name": "x",
                                    "slug": "x", "score": 1.0}], "cursor": 7, "error": None},
        "semantic": {"results": [], "cursor": None, "error": None},
    }
    out = service.research_search("q", collapse=None)
    assert [r["id"] for r in out["data"]["results"]] == ["e-1"]
    assert out["data"]["channels"]["exact"]["error"] == "malformed_response"
    assert out["next_cursor"] is None  # the non-string cursor is dropped

    # an entirely non-dict body degrades the same way
    app.search_response = ["garbage"]
    out = service.research_search("q", collapse=None)
    assert out["data"]["results"] == []
    assert out["completeness"]["state"] == "partial"


def test_capability_cache_shared_across_token_factory_calls(client, app):
    # Exercises the REAL per-token wiring: _service_from_token must reuse one
    # source (and thus one cached probe) across tool calls, not probe per call.
    from probe.mcp import server as server_mod

    server_mod._clients.clear()
    server_mod._sources.clear()
    server_mod._clients["tok-a"] = client
    app.search_response = _search_response()
    reset = server_mod._token_var.set("tok-a")
    try:
        first = server_mod._service_from_token()
        first.research_context("anything")
        second = server_mod._service_from_token()
        second.research_get("run:some-run")
        assert first.source is second.source
        assert len(app.search_requests) == 1  # one probe total, not per call
    finally:
        server_mod._token_var.reset(reset)
        server_mod._clients.clear()
        server_mod._sources.clear()


def test_asset_resolve_returns_no_match_when_not_found(client):
    # The registry exists now (fold #5); resolving an absent asset is an honest
    # no_match over the real registry, not a missing-capability.
    service = ResearchReadService(ResearchOSSource(client))
    result = service.research_resolve("dockq-scorer", kind="script")
    assert result["data"]["state"] == "no_match"


def test_server_exposes_only_the_five_read_tools(client):
    """Thin harness: coverage grows through research_get's `view`/`filters`, NEVER
    through more tools. Spans, groups, events, execution records and experiment
    versions all became reachable while the tool count went DOWN (trace_file, which
    had no backend, was removed). If this number ever climbs, the fat-skills seam
    was abandoned for a research_get_spans-shaped shortcut."""
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
    from probe.mcp.server import _clients, _service_from_token, _sources, _token_var

    _clients.clear()
    _sources.clear()
    reset = _token_var.set("read-only-tok")
    try:
        _service_from_token()
        assert "read-only-tok" in _clients
        assert "read-only-tok" in _sources
        assert _clients["read-only-tok"].settings.token == "read-only-tok"
    finally:
        _token_var.reset(reset)
        _clients.clear()
        _sources.clear()


def test_http_app_builds():
    from probe.mcp.server import http_app

    app = http_app()  # FastMCP streamable-http + auth/health wrapper
    assert callable(app)
