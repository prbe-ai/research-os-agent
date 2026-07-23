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

    results = service.search_knowledge("DockQ paths")
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
    out = service.search_knowledge(
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
    service.search_knowledge("adam sweep", cursor=out["next_cursor"])
    assert app.search_requests[-1]["exact_cursor"] == "exact-c1"
    assert "semantic_cursor" not in app.search_requests[-1]


def test_search_transcripts_corpus_maps_to_backend(client, app):
    # transcripts is now a first-class backend corpus (POST /v1/search accepts and
    # defaults to it), so the tool maps it through instead of degrading it to an
    # unsupported kb_corpora miss.
    app.search_response = _search_response()
    service = ResearchReadService(ResearchOSSource(client))
    out = service.search_knowledge("q", corpora=["transcripts", "assets"])
    assert app.search_requests[-1]["corpus"] == ["experiments", "files", "transcripts"]
    assert out["completeness"] == {"state": "complete", "missing": []}
    assert out["data"]["unsupported_corpora"] == []


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
    out = service.search_knowledge("DockQ paths", corpora=["documents"])
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
    out = service.search_knowledge("folding", collapse=None)
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
        service.search_knowledge("q", workspace_id="ws-missing")
    # original + capability probe + ONE retry. The retry costs a wasted request
    # on a genuinely-absent scope, and buys a correct answer when the 404 came
    # from a stale pod mid-deploy instead. Attributing a scoped 404 to the scope
    # without retrying turns a rolling deploy into "your project does not
    # exist", which is a wrong answer about the caller's own data.
    assert len(app.search_requests) == 3  # original + ONE probe + ONE retry


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
    out = service.search_knowledge("q", top_k=4, collapse=None)
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
    page1 = service.search_knowledge("q", top_k=4, collapse=None)
    assert len(page1["data"]["results"]) == 4
    page2 = service.search_knowledge("q", top_k=4, collapse=None, cursor=page1["next_cursor"])
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
    out = service.search_knowledge("adam")
    results = out["data"]["results"]
    assert [r["id"] for r in results] == ["e-1"]
    assert results[0]["entity_type"] == "experiment"
    assert results[0]["why_matched"]["channel"] == "semantic"
    assert results[0]["why_matched"]["score"] == 0.9

    # collapse=None keeps the heterogeneous merged view
    out = service.search_knowledge("adam", collapse=None)
    assert {r["entity_type"] for r in out["data"]["results"]} == {
        "experiment", "project", "file",
    }
    assert len(out["data"]["results"]) == 4


def test_search_workspace_scope_rejected_on_pre_search_backend(client, app):
    # A pre-search server has no workspaces; silently returning tenant-wide
    # results would be worse than failing loudly.
    service = ResearchReadService(ResearchOSSource(client))
    with pytest.raises(errors.ValidationError):
        service.search_knowledge("q", workspace_id="ws-1")


def test_keyword_fallback_ignores_incoming_cursor(client, app):
    # Version skew: a packed /v1/search cursor arrives at a pre-search server.
    # Echoing it back would make cursor-following consumers loop forever.
    service = ResearchReadService(ResearchOSSource(client))
    out = service.search_knowledge("q", cursor='{"exact": "ex-2"}')
    assert out["data"]["results"] == []
    assert out["next_cursor"] is None


def test_search_forwards_project_scope_to_the_backend(client, app):
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
    out = service.search_knowledge("q", project_id="p-1", collapse=None)
    # Project scope is the BACKEND's job now (research-os #103): it re-resolves
    # semantic hits against live rows and over-fetches so the filter runs before
    # the cap. The tool passes the scope through and keeps whatever comes back.
    #
    # This previously emptied the semantic channel client-side and reported
    # project_scope_unsupported, so a scoped search silently degraded to trigram
    # matching -- the single most natural way to search was also the most
    # degraded.
    assert app.search_requests[-1]["project_id"] == "p-1"
    assert out["data"]["channels"]["semantic"]["error"] is None
    assert "file:f-1" in [r["id"] for r in out["data"]["results"]] or any(
        r["entity_type"] == "file" for r in out["data"]["results"]
    )
    assert out["completeness"]["state"] == "complete"


def test_keyword_fallback_scopes_by_project(client, app):
    # pre-search server (no search_response): the fallback keeps project scoping
    p1 = client.ensure_project("proj-one")
    client.ensure_project("proj-two")
    client.run(project="proj-one", experiment="exp-one", hypothesis="h1", name="r1")
    client.run(project="proj-two", experiment="exp-two", hypothesis="h2", name="r2")
    service = ResearchReadService(ResearchOSSource(client))
    out = service.search_knowledge("exp", project_id=p1["id"])
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
    out = service.search_knowledge("adam", collapse=None)
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
    rows = service2.search_knowledge("kw-exp")["data"]["results"]
    assert rows
    for row in rows:
        assert set(row["why_matched"]) == expected_keys
    assert rows[0]["why_matched"]["terms"] == ["kw-exp"]
    assert rows[0]["why_matched"]["channel"] == "keyword"


def test_unsupported_verdict_short_circuits_then_expires(client, app):
    service = ResearchReadService(ResearchOSSource(client))
    source = service.source

    service.search_knowledge("q")  # 404 -> probe 404 -> cached unsupported
    assert len(app.search_requests) == 2
    service.search_knowledge("q")  # fresh verdict short-circuits: no doomed POST
    assert len(app.search_requests) == 2

    # after the recheck window the verdict expires and the upgrade is noticed
    app.search_response = _search_response()
    source._search_checked_at -= 301
    out = service.search_knowledge("q")
    assert len(app.search_requests) == 3  # original + ONE probe + ONE retry
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
    service.search_knowledge("q")  # establishes supported=True (1 request)
    app.search_404_once = True  # one stale pod mid rolling deploy
    out = service.search_knowledge("q")  # 404 -> probe ok -> retried once
    assert len(app.search_requests) == 4
    assert [r["id"] for r in out["data"]["results"]] == ["e-1"]
    assert out["capabilities"]["unified_search"] is True


def test_malformed_cursor_raises_validation_error(client, app):
    app.search_response = _search_response()
    service = ResearchReadService(ResearchOSSource(client))
    with pytest.raises(errors.ValidationError):
        service.search_knowledge("q", cursor="not-a-packed-cursor")
    with pytest.raises(errors.ValidationError):
        service.search_knowledge("q", cursor='["wrong-shape"]')


def test_search_unknown_collapse_rejected(client, app):
    # only "experiment" (dedupe) and null (heterogeneous) are defined; anything
    # else must fail loudly instead of silently falling through
    app.search_response = _search_response()
    service = ResearchReadService(ResearchOSSource(client))
    with pytest.raises(errors.ValidationError):
        service.search_knowledge("q", collapse="run")
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

    # The server builds its Client from an explicit Settings (so a missing mcp_token
    # can never fall back to the write token), so read the token off settings.
    monkeypatch.setattr(
        server_mod,
        "Client",
        lambda *, settings, fail_open, surface=None: FakeClient(settings.token),
    )

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
    out = service.search_knowledge("q", collapse=None)
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
    out = service.search_knowledge("q", collapse=None)
    assert [r["id"] for r in out["data"]["results"]] == ["e-1"]
    assert out["data"]["channels"]["exact"]["error"] == "malformed_response"
    assert out["next_cursor"] is None  # the non-string cursor is dropped

    # an entirely non-dict body degrades the same way
    app.search_response = ["garbage"]
    out = service.search_knowledge("q", collapse=None)
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
        second.get_entity("run:some-run")
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


def test_server_exposes_exactly_the_read_tools(client):
    """Thin harness: coverage grows through research_get's `view`/`filters`, NEVER
    through more tools. Spans, groups, events, execution records and experiment
    versions all became reachable while the tool count went DOWN (trace_file, which
    had no backend, was removed). If this set grows, the fat-skills seam was
    abandoned for a research_get_spans-shaped shortcut.

    `research_browse` is the one addition that earns its place, and the bar it
    cleared is worth recording. It is not another way to read an entity -- that
    is research_get's job and no view of it enumerates the tree. It answers a
    question the other tools structurally cannot: search ranks by relevance to a
    query, so it requires you to already know what to search for, and nothing
    answered "what exists here?" A `research_get_spans` would fail this bar,
    because `research_get(view="trajectory")` already answers it.
    """
    service = ResearchReadService(ResearchOSSource(client))
    server = create_server(service)
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    # The real surface is THREE. The other five are deprecation aliases that
    # exist for exactly one release, because MCP tools are served by the SERVER
    # and .mcp.json pins one url for every plugin version -- so renaming a tool
    # breaks every installed client the instant the image rolls.
    # (Comparing NEW_SURFACE/ALIASES to their own literals would be a tautology;
    # the load-bearing assertion is that the SERVER exposes exactly their union.)
    assert names == NEW_SURFACE | ALIASES
    assert not any(name.startswith(("create", "update", "promote", "upload")) for name in names)


NEW_SURFACE = {"browse_research", "search_knowledge", "get_entity"}
ALIASES = {
    "research_context",
    "research_search",
    "research_get",
    "research_compare",
    "research_resolve",
}


def test_every_alias_still_answers(client, app):
    """An alias that 404s is not a deprecation window, it is an outage.

    These keep the OLD signatures and the OLD payloads deliberately: an alias
    returning a different shape is a breaking change wearing a compatibility
    label.
    """
    import asyncio as _asyncio

    app.search_response = _search_response()
    service = ResearchReadService(ResearchOSSource(client))
    server = create_server(service)
    tools = {t.name for t in _asyncio.run(server.list_tools())}
    assert ALIASES <= tools

    # Through the TOOL LAYER, not the service: the translation being tested
    # lives in the alias closure, and calling the service directly would exercise
    # none of it. The earlier version of this test called
    # service.search_knowledge(project_id=...) -- with the typed parameter
    # already applied -- and would have passed with the alias body replaced by
    # `return {}`.
    out = _call_tool(
        server, "research_search", {"query": "q", "filters": {"project_id": "p-1"}}
    )
    assert app.search_requests[-1]["project_id"] == "p-1"
    assert out["data"]["query"] == "q"

    # `limit` -> `top_k`, and workspace_id from either place.
    _call_tool(
        server,
        "research_search",
        {"query": "q", "limit": 4, "filters": {"workspace_id": "ws-9"}},
    )
    assert app.search_requests[-1]["workspace_id"] == "ws-9"
    assert app.search_requests[-1]["exact_limit"] == 2  # ceil(4/2) per channel


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


def _browse_payload() -> dict:
    return {
        "projects": [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "name": "bird-sql",
                "slug": "bird-sql",
                "workspace_id": None,
                "created_at": "2026-07-01T00:00:00Z",
                "experiment_count": 2,
                "active_run_count": 1,
                "experiments": None,
            }
        ],
        "experiments": None,
        "runs": None,
        "cursor": None,
        "depth": 1,
        "limit": 50,
        "truncated": False,
    }


def test_browse_annotates_nodes_with_ref_and_available_views(app, client):
    """Every node carries what the NEXT call needs: a ref research_get accepts,
    and the exact views valid for that kind -- derived from the same matrix
    research_get validates against, so the two cannot disagree."""
    app.browse_response = _browse_payload()
    service = ResearchReadService(ResearchOSSource(client))
    envelope = service.browse_research()

    [node] = envelope["data"]["projects"]
    assert node["ref"] == "project:11111111-1111-1111-1111-111111111111"
    assert node["entity_type"] == "project"
    # Derived, never hand-written: a project has exactly one view today.
    assert node["available_views"] == ["card"]
    assert envelope["completeness"]["state"] == "complete"
    # Unexpanded levels stay None -- distinct from [] (expanded, empty).
    assert envelope["data"]["experiments"] is None


def test_browse_available_views_track_the_real_view_matrix(app, client):
    """The views advertised must be the views research_get actually accepts.

    A hand-maintained list is how docs end up naming views that no longer
    exist, so this asserts the advertised set IS the validated set.
    """
    from probe.mcp.service import _supported_views

    app.browse_response = {
        **_browse_payload(),
        "projects": None,
        "runs": [
            {
                "id": "22222222-2222-2222-2222-222222222222",
                "name": "r",
                "short_id": None,
                "status": "running",
                "created_at": "2026-07-01T00:00:00Z",
                "started_at": None,
                "ended_at": None,
                "alive": None,
            }
        ],
    }
    service = ResearchReadService(ResearchOSSource(client))
    [run] = service.browse_research(scope="experiment:x")["data"]["runs"]
    assert run["available_views"] == _supported_views("run")
    assert "trajectory" in run["available_views"]


def test_browse_on_an_old_backend_reports_missing_not_empty(app, client):
    """"Nothing exists" and "I cannot tell you what exists" are opposite claims.

    Returning an empty tree for a backend without the route would stop an agent
    looking any further, so the envelope says the capability is missing instead.
    """
    app.browse_response = None  # route 404s, as a pre-browse backend does
    service = ResearchReadService(ResearchOSSource(client))
    envelope = service.browse_research()

    assert envelope["completeness"]["state"] == "partial"
    assert "structured_browse" in envelope["completeness"]["missing"]
    assert envelope["data"]["projects"] is None
    assert envelope["capabilities"]["structured_browse"] is False


def test_browse_scoped_404_is_a_missing_scope_not_a_missing_route(app, client):
    """A scoped 404 means the SCOPE was not found on a backend that HAS the
    route. Treating it as "no browse endpoint" would blind the tool to the whole
    tree because one id was wrong."""
    app.browse_response = None
    service = ResearchReadService(ResearchOSSource(client))
    with pytest.raises(errors.NotFoundError):
        service.browse_research(scope="project:does-not-exist")


def test_browse_truncation_is_reported(app, client):
    """A cut tree must not read as a complete one: an absent child would
    otherwise look like evidence of absence."""
    app.browse_response = {**_browse_payload(), "truncated": True}
    service = ResearchReadService(ResearchOSSource(client))
    envelope = service.browse_research(depth=2)
    assert envelope["completeness"]["state"] == "partial"
    assert "truncated_by_token_budget" in envelope["completeness"]["missing"]


# -- the prose contract ------------------------------------------------------
# Two halves of this product are prose: the server `instructions` (ships with
# the image, cannot go stale) and the tool docstrings (same). Both are load-
# bearing -- they are the entire mechanism by which an agent decides to call
# anything. These guard the claims that would silently rot.

def _call_tool(server, tool: str, args: dict):
    """Invoke a tool the way a real MCP client does, and unwrap the payload."""
    import asyncio as _asyncio

    result = _asyncio.run(server.call_tool(tool, args))
    payload = result[1] if isinstance(result, tuple) else result
    if isinstance(payload, dict) and "result" in payload:
        return payload["result"]
    if isinstance(payload, list):
        return json.loads(payload[0].text)
    return payload


def _tool_docs(client) -> dict[str, str]:
    import asyncio as _asyncio

    server = create_server(ResearchReadService(ResearchOSSource(client)))
    return {t.name: (t.description or "") for t in _asyncio.run(server.list_tools())}


def test_routing_rule_is_stated_in_both_places_and_agrees(client):
    """browse-vs-search must not become the new context-vs-search.

    research_context rotted because nothing told an agent when to prefer it, so
    agents defaulted to search. The rule that works is about what you HAVE, not
    what you want -- and it is stated in the instructions AND at both call
    sites, because an agent choosing a tool reads the docstring, not the
    preamble. Stating it twice is a drift risk, which is what this test is for.
    """
    from probe.mcp.server import MCP_INSTRUCTIONS

    docs = _tool_docs(client)

    # The instructions carry the rule and name both tools.
    assert "browse_research" in MCP_INSTRUCTIONS
    assert "search_knowledge" in MCP_INSTRUCTIONS
    assert "what you HAVE" in MCP_INSTRUCTIONS

    # Each tool points at the other, so whichever one the agent reads first
    # tells it when the other is the right call.
    assert "search_knowledge" in docs["browse_research"]
    assert "browse_research" in docs["search_knowledge"]
    assert "what you HAVE" in docs["browse_research"]


def test_query_formulation_is_taught_where_the_query_is_written(client):
    """The Good/Bad pair must live on the tool that takes the query.

    An agent forms the query at the call site; a rule that exists only in the
    preamble is read once and forgotten by the time it matters.
    """
    from probe.mcp.server import MCP_INSTRUCTIONS

    docs = _tool_docs(client)
    for text in (MCP_INSTRUCTIONS, docs["search_knowledge"]):
        assert "Good:" in text and "Bad:" in text
        assert "kl_coef" in text  # a concrete, domain-real example


def test_reuse_before_create_is_in_the_durable_half(client):
    """The most valuable rule in the product must not live only in a skill.

    It used to: track-experiment carried it, and the INSTALLED copy of that
    skill was measured 30 lines behind the repo. The instructions ship with the
    image and cannot drift, so the rule lives there and is repeated on the tool
    that enforces it.
    """
    from probe.mcp.server import MCP_INSTRUCTIONS

    docs = _tool_docs(client)
    assert "REUSE BEFORE YOU CREATE" in MCP_INSTRUCTIONS
    assert 'asset:<name>' in MCP_INSTRUCTIONS
    assert "REUSE BEFORE YOU CREATE" in docs["get_entity"]


def test_the_view_matrix_in_the_docstring_matches_the_real_one(client):
    """The docstring advertises views; _VIEWS decides them.

    A hand-written matrix is how documentation ends up naming views that no
    longer exist -- an agent then asks for one, gets a validation error, and the
    error contradicts the docs it just followed.
    """
    from probe.mcp.service import _VIEWS, _supported_views

    doc = _tool_docs(client)["get_entity"]
    for kind in sorted({k for k, _ in _VIEWS}):
        line = next(
            (ln for ln in doc.splitlines() if ln.strip().startswith(kind + " ")), None
        )
        assert line, f"get_entity docstring does not document kind {kind!r}"
        advertised = {p.strip() for p in line.split(None, 1)[1].split("|")}
        assert advertised == set(_supported_views(kind)), (
            f"{kind}: docstring says {sorted(advertised)}, "
            f"_VIEWS says {_supported_views(kind)}"
        )


def test_every_alias_is_marked_deprecated_in_its_own_docstring(client):
    """An alias nobody knows is temporary becomes permanent."""
    docs = _tool_docs(client)
    for name in ALIASES:
        assert "DEPRECATED" in docs[name], f"{name} does not say it is deprecated"
        assert "next release" in docs[name], f"{name} does not say when it goes"


def test_verbose_false_keeps_the_fields_an_agent_acts_on(client, app):
    """Compaction must drop bookkeeping, never signal.

    The interesting half of a drop-list is what it KEEPS: a list without stated
    reasons rots into dropping something load-bearing.
    """
    app.search_response = _search_response(
        exact=[
            {"entity_type": "experiment", "id": "e-1", "name": "n", "slug": "s",
             "workspace_id": None, "project_id": None, "experiment_id": None,
             "run_id": None, "score": 0.9},
        ],
        semantic_error="engine_timeout",
    )
    service = ResearchReadService(ResearchOSSource(client))
    full = service.search_knowledge("q", verbose=True)
    lean = service.search_knowledge("q", verbose=False)

    # Constant-per-token bookkeeping goes.
    for gone in ("schema_version", "as_of", "scope"):
        assert gone in full and gone not in lean

    # completeness ALWAYS survives: it is the only field saying what the
    # response could not cover. Stripping it turns a partial answer into a
    # confident one.
    assert lean["completeness"]["state"] == "partial"
    assert "semantic_search" in lean["completeness"]["missing"]
    assert lean["data"]["results"] == full["data"]["results"]


def test_verbose_false_keeps_only_the_capabilities_that_are_false(client, app):
    """A True capability is noise repeated on every call. A False one says this
    backend cannot do something you may be about to rely on."""
    service = ResearchReadService(ResearchOSSource(client))
    lean = service.search_knowledge("q", verbose=False)
    # The key is ALWAYS present -- omitting it would overload absence to mean
    # both "all good" and "not reported", so a caller indexing into it would
    # KeyError on a healthy backend and succeed on a degraded one.
    assert "capabilities" in lean
    # This fake predates /v1/search, so several capabilities are genuinely False,
    # and ONLY the false ones survive compaction.
    assert lean["capabilities"]
    assert all(v is False for v in lean["capabilities"].values())
    assert "unified_search" in lean["capabilities"]


def test_aliases_still_return_the_full_envelope(client, app):
    """The whole point of an alias is that nothing changes for its callers."""
    app.search_response = _search_response()
    server = create_server(ResearchReadService(ResearchOSSource(client)))
    # Through the TOOL LAYER for every alias. Calling the service directly would
    # only prove the service DEFAULT is verbose=True, not that the aliases omit
    # the argument -- so "tidying" server.py to pass verbose=False would leave
    # this green while every installed client started receiving compacted
    # payloads, which is the precise breakage aliases exist to prevent.
    full_envelope = {
        "schema_version", "as_of", "scope", "capabilities",
        "data", "evidence", "completeness", "next_cursor",
    }
    calls = {
        "research_search": {"query": "q"},
        "research_context": {"task": "t"},
        "research_resolve": {"name": "a"},
    }
    for tool, args in calls.items():
        out = _call_tool(server, tool, args)
        assert full_envelope <= set(out), f"{tool} alias lost {full_envelope - set(out)}"

    # ...and the NEW surface is compact by contrast, which is the whole point.
    lean = _call_tool(server, "search_knowledge", {"query": "q"})
    assert "schema_version" not in lean
    assert "completeness" in lean


def test_project_scope_degrades_on_a_backend_that_ignores_it(client, app):
    """A backend predating server-side project scope must not answer silently.

    SearchRequest does not forbid extra body fields, so an older server accepts
    `project_id`, ignores it, and returns TENANT-WIDE results with
    state="complete". An agent then attributes another project's runs to this
    one -- worse than any error, because it is confident and unmarked.

    The echo is the only available signal, so its absence is treated as
    unsupported. A false refusal is loud and correctable; a false answer is not.
    """
    app.search_response = _search_response(
        exact=[
            {"entity_type": "experiment", "id": "e-1", "name": "in", "slug": "in",
             "workspace_id": None, "project_id": "p-1", "experiment_id": None,
             "run_id": None, "score": 0.9},
            {"entity_type": "experiment", "id": "e-2", "name": "out", "slug": "out",
             "workspace_id": None, "project_id": "p-OTHER", "experiment_id": None,
             "run_id": None, "score": 0.8},
        ],
        semantic=[
            {"doc_id": "file:f-1", "title": "d", "snippet": "s", "score": 0.9,
             "source_system": "workspace", "source_url": None,
             "ref": {"kind": "file", "id": "f-1"}},
        ],
    )
    app.echoes_project_scope = False
    service = ResearchReadService(ResearchOSSource(client))
    out = service.search_knowledge("q", project_id="p-1", collapse=None)

    # Out-of-project rows are removed rather than passed through: the whole
    # danger is tenant-wide results wearing a project scope.
    assert [r["id"] for r in out["data"]["results"]] == ["e-1"]
    # The semantic channel cannot be scoped client-side, so it is EMPTIED and
    # marked -- never passed through unscoped.
    assert out["data"]["channels"]["semantic"]["error"] == "project_scope_unsupported"
    assert out["completeness"]["state"] == "partial"
    assert "semantic_search" in out["completeness"]["missing"]

    # An echoing backend keeps its semantic channel, which is the point of the
    # backend change: scoping stops costing you semantic retrieval.
    app.echoes_project_scope = True
    ok = service.search_knowledge("q", project_id="p-1", collapse=None)
    assert ok["data"]["channels"]["semantic"]["error"] is None
    assert any(r["entity_type"] == "file" for r in ok["data"]["results"])


def test_backend_truncation_is_surfaced_not_swallowed(client, app):
    """A trimmed response must not read as a complete one.

    The backend drops chunks and whole results to fit its byte budget and says
    so. If the tool swallows that, an agent reads a short result set as "the lab
    has nothing else" -- the silent false negative this whole surface is built
    to avoid.
    """
    app.search_response = {**_search_response(), "truncated": True}
    service = ResearchReadService(ResearchOSSource(client))
    out = service.search_knowledge("q")
    assert out["completeness"]["state"] == "partial"
    assert "truncated_by_response_budget" in out["completeness"]["missing"]

    # ...and an untruncated response stays complete, so the marker means something.
    app.search_response = _search_response()
    assert service.search_knowledge("q")["completeness"]["state"] == "complete"
