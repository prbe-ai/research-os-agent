"""research_get's `view` seam: every view means something, and says so honestly.

The bugs these guard against were all live before this suite existed:
  * `reproduce`/`handoff`/`metrics`/`artifacts` all attached the SAME run bundle —
    four advertised views, one payload;
  * `contract`/`versions`/`usage` unconditionally reported missing:["versioned_assets"]
    and had never been implemented;
  * `token_budget` and (on research_get) `cursor` were echoed back and bounded nothing;
  * spans/groups/events/execution records were reachable from the SDK and invisible here,
    so an agent could see that 500 rollouts happened and not one of what they did.
"""

from __future__ import annotations

import json
import uuid

import pytest

from probe.mcp.service import _VIEWS, ResearchReadService
from probe.mcp.source import ResearchOSSource
from probe.sdk import errors


def _service(client) -> ResearchReadService:
    return ResearchReadService(ResearchOSSource(client))


def _populated(client, app, *, spans: int = 3):
    """A run with EVERYTHING a view could want: spans, series, metric points,
    artifacts, an execution record, a group, an experiment version, and events.

    Fully populated on purpose: it is what lets test_no_view_reports_missing_
    unconditionally tell "genuinely absent" apart from "always claims absence".
    """
    record = client.execution_record(
        code={"git_sha": "abc123"}, deps={"torch": "2.4"}, hardware={"gpu": "H100"}
    )
    run = client.run(
        project="folding",
        experiment="dockq-path",
        hypothesis="relative paths fix scoring",
        name="eval-1",
    )
    rid = run.id
    experiment_id = app.runs[rid]["experiment_id"]
    app.runs[rid]["env_ref"] = record["content_hash"]

    app.spans[rid] = [
        {
            "id": f"span-{i}",
            "run_id": rid,
            "span_type": "rollout" if i % 2 == 0 else "tool_call",
            "name": f"rollout-{i}",
            "step_index": i,
            "status": "ok",
            "parent_span_id": None,
            "attributes": {"reward": i * 0.1},
            "summary": {},
            "started_at": "2026-07-16T00:00:00Z",
            "ended_at": "2026-07-16T00:00:01Z",
            "customer_id": "lab-42",
            "created_at": "2026-07-16T00:00:00Z",
        }
        for i in range(spans)
    ]
    app.series[rid] = [
        {"run_id": rid, "key": "loss", "kind": "scalar", "x_axis": "step",
         "dimensions": {}, "point_count": 2, "last_value": 0.3,
         "min_value": 0.3, "max_value": 0.9, "first_step_index": 0, "last_step_index": 1},
    ]
    app.metric_points[rid] = [
        {"id": 1, "run_id": rid, "key": "loss", "kind": "scalar", "value": 0.9,
         "step_index": 0, "dimensions": {}, "wall_clock": "2026-07-16T00:00:00Z"},
        {"id": 2, "run_id": rid, "key": "loss", "kind": "scalar", "value": 0.3,
         "step_index": 1, "dimensions": {}, "wall_clock": "2026-07-16T00:00:01Z"},
    ]
    artifact_id = str(uuid.uuid4())
    app.artifacts[rid] = [
        {"id": artifact_id, "run_id": rid, "name": "loss.png", "kind": "figure",
         "status": "ready", "is_reference": False, "uri": "s3://b/loss.png",
         "customer_id": "lab-42", "created_at": "2026-07-16T00:00:00Z"},
    ]
    app.run_events[rid] = [
        {"id": "ev-1", "customer_id": "lab-42", "event_type": "run.created",
         "subject_type": "run", "subject_id": rid, "actor": "ingest:test",
         "payload": {}, "created_at": "2026-07-16T00:00:00Z"},
    ]
    group = client.create_group(experiment_id, "lr-sweep", kind="sweep", spec={"lr": [1, 2]})
    client.experiment_version(experiment_id, label="v1")
    client.add_edge(
        source_type="run", source_id=rid, relation="produces",
        target_type="artifact", target_id=artifact_id,
    )
    return rid, experiment_id, group["id"], record["content_hash"]


# -- the headline gap: a trajectory is readable at all ------------------------


def test_trajectory_view_reads_the_actual_spans(client, app):
    """The gap that mattered most: the run bundle carries span_type COUNTS, so
    there was no way to read a trajectory through the MCP at all."""
    rid, _, _, _ = _populated(client, app, spans=3)
    result = _service(client).research_get(f"run:{rid}", view="trajectory")

    spans = result["data"]["spans"]
    assert [s["name"] for s in spans] == ["rollout-0", "rollout-1", "rollout-2"]
    assert spans[0]["attributes"] == {"reward": 0.0}  # the payload, not a count
    assert result["completeness"]["state"] == "complete"


def test_trajectory_filters_push_to_the_backend(client, app):
    rid, _, _, _ = _populated(client, app, spans=6)
    result = _service(client).research_get(
        f"run:{rid}", view="trajectory", filters={"span_type": "tool_call", "step_from": 3}
    )
    assert [s["step_index"] for s in result["data"]["spans"]] == [3, 5]
    assert any("span_type=tool_call" in str(r.url) for r in app.requests)


# -- the core regression: views must not collapse into one payload ------------


def test_every_run_view_returns_a_materially_different_payload(client, app):
    """`reproduce`/`handoff`/`metrics`/`artifacts` all used to attach the same
    self.source.bundle(...). Four advertised views, one identical response."""
    rid, _, _, _ = _populated(client, app)
    service = _service(client)
    views = ["card", "trajectory", "metrics", "artifacts", "reproduce", "handoff",
             "lineage", "events"]

    payloads = {view: service.research_get(f"run:{rid}", view=view)["data"] for view in views}
    for view, data in payloads.items():
        assert data["view"] == view

    serialized = {view: json.dumps(data, sort_keys=True, default=str) for view, data in payloads.items()}
    assert len(set(serialized.values())) == len(views), "two views returned the same payload"
    # The four that were literally identical before, spelled out.
    assert payloads["metrics"] != payloads["artifacts"]
    assert payloads["reproduce"] != payloads["handoff"]


def test_no_view_reports_missing_unconditionally(client, app):
    """THE honest-envelope guard, and the one tests/test_parity.py cannot give us:
    parity guards HTTP reachability, not whether a view is a lie.

    Against a fully-populated entity every view must be able to say `complete`. A
    view that cannot is either unimplemented (contract/versions/usage) or reporting
    absence that is not real — and `missing` stops meaning anything the moment it
    is always populated."""
    rid, experiment_id, group_id, _ = _populated(client, app)
    project_id = app.runs[rid].get("project_id") or client.list_projects().items[0]["id"]
    refs = {"run": f"run:{rid}", "experiment": f"experiment:{experiment_id}",
            "project": f"project:{project_id}", "group": f"group:{group_id}"}
    service = _service(client)

    for kind, view in sorted(_VIEWS):
        result = service.research_get(refs[kind], view=view, token_budget=100_000)
        assert result["completeness"]["missing"] == [], (
            f"view={view!r} on a {kind} reports missing "
            f"{result['completeness']['missing']} against a fully-populated entity"
        )
        assert result["completeness"]["state"] == "complete"


def test_deleted_phantom_views_are_rejected_by_name(client, app):
    """contract/usage are gone, not degraded: AssetOut has no contract concept and
    there is no reverse-usage index anywhere in the schema, so they could never be
    implemented. A loud error naming the real views beats an envelope that says
    `missing` forever, which reads as "temporarily degraded"."""
    rid, _, _, _ = _populated(client, app)
    service = _service(client)
    for view in ("contract", "usage"):
        with pytest.raises(errors.ValidationError) as excinfo:
            service.research_get(f"run:{rid}", view=view)
        assert "trajectory" in str(excinfo.value)  # names what a run really supports


def test_view_not_available_for_this_kind_names_the_kinds_real_views(client, app):
    _, experiment_id, _, _ = _populated(client, app)
    with pytest.raises(errors.ValidationError) as excinfo:
        _service(client).research_get(f"experiment:{experiment_id}", view="trajectory")
    message = str(excinfo.value)
    assert "experiment supports" in message and "groups" in message


# -- reproduce: an actual reproduction ---------------------------------------


def test_reproduce_resolves_env_ref_through_its_execution_record(client, app):
    """This used to return a bundle and call that reproduction."""
    rid, _, _, content_hash = _populated(client, app)
    data = _service(client).research_get(f"run:{rid}", view="reproduce")["data"]

    assert data["hypothesis"] == "relative paths fix scoring"
    assert data["env_ref"] == content_hash
    assert data["execution_record"]["code"] == {"git_sha": "abc123"}
    assert data["execution_record"]["hardware"] == {"gpu": "H100"}
    assert "bundle" not in data


def test_reproduce_without_an_env_ref_reports_it_missing(client, app):
    """CONDITIONAL missing — the honest kind. This run captured no environment, so
    it genuinely cannot be reproduced from here."""
    run = client.run(project="folding", experiment="e", hypothesis="h", name="no-env")
    result = _service(client).research_get(f"run:{run.id}", view="reproduce")
    assert result["completeness"]["missing"] == ["execution_record"]
    assert result["completeness"]["state"] == "partial"


# -- groups reached by a parameter, never by a seventh tool -------------------


def test_groups_are_reachable_by_view_and_by_ref(client, app):
    """The thin-harness move: a sweep is an experiment-shaped noun, so it rides the
    existing `view=` / `ref=` seams instead of a research_list_groups tool."""
    _, experiment_id, group_id, _ = _populated(client, app)
    service = _service(client)

    listed = service.research_get(f"experiment:{experiment_id}", view="groups")["data"]
    assert [g["name"] for g in listed["groups"]] == ["lr-sweep"]

    one = service.research_get(f"group:{group_id}")["data"]
    assert one["entity_type"] == "group"
    assert one["entity"]["spec"] == {"lr": [1, 2]}


def test_versions_view_is_real_against_the_live_registry(client, app):
    """Was: unconditionally missing:["versioned_assets"], never implemented."""
    _, experiment_id, _, _ = _populated(client, app)
    result = _service(client).research_get(f"experiment:{experiment_id}", view="versions")
    assert [v["label"] for v in result["data"]["versions"]] == ["v1"]
    assert result["completeness"]["missing"] == []


# -- token_budget actually bounds ---------------------------------------------


def test_token_budget_bounds_a_large_trajectory(client, app):
    """The knob was accepted, echoed, and bounded nothing — so an agent asking for
    2000 tokens could take a 500-span trajectory to the face."""
    rid, _, _, _ = _populated(client, app, spans=500)
    service = _service(client)

    unbounded = service.research_get(f"run:{rid}", view="trajectory", token_budget=1_000_000)
    bounded = service.research_get(f"run:{rid}", view="trajectory", token_budget=600)

    assert 0 < len(bounded["data"]["spans"]) < len(unbounded["data"]["spans"])
    assert len(json.dumps(bounded["data"]["spans"])) // 4 <= 600
    assert bounded["completeness"]["state"] == "partial"
    assert "truncated_by_token_budget" in bounded["completeness"]["missing"]
    assert bounded["next_cursor"] is not None


def test_a_bounded_fetch_window_never_passes_itself_off_as_the_whole_trajectory(client, app):
    """Even with an effectively infinite budget, one call reads a WINDOW of a
    500-span run, not the run. Emitting those 200 rows with next_cursor=None would
    tell the agent it had read the entire trajectory — the exact confident-wrong
    answer this whole change is about."""
    rid, _, _, _ = _populated(client, app, spans=500)
    result = _service(client).research_get(
        f"run:{rid}", view="trajectory", token_budget=1_000_000
    )
    assert len(result["data"]["spans"]) == 200  # _PAGE_FETCH, not 500
    assert result["next_cursor"] is not None  # ... and it SAYS so
    # Not a budget truncation, so this is ordinary pagination, as in research_search.
    assert result["completeness"]["state"] == "complete"


def test_a_budget_too_small_for_one_row_still_makes_progress(client, app):
    """Never return zero rows with a cursor: a walk would spin forever. Emit one
    and report the overflow instead."""
    rid, _, _, _ = _populated(client, app, spans=5)
    result = _service(client).research_get(f"run:{rid}", view="trajectory", token_budget=1)
    assert len(result["data"]["spans"]) == 1
    assert result["completeness"]["state"] == "partial"


def test_reproduce_is_atomic_and_reports_overflow_instead_of_truncating(client, app):
    """A reproduction manifest with fields dropped to fit reproduces nothing, so
    overflow is REPORTED rather than silently corrupting the answer."""
    rid, _, _, _ = _populated(client, app)
    app.runs[rid]["config"] = {f"hyperparam_{i}": "x" * 100 for i in range(50)}

    result = _service(client).research_get(f"run:{rid}", view="reproduce", token_budget=50)
    assert result["completeness"]["missing"] == ["token_budget_exceeded"]
    assert result["completeness"]["state"] == "partial"
    assert result["next_cursor"] is None  # nothing to paginate
    assert len(result["data"]["config"]) == 50  # intact, not quietly trimmed


# -- cursor: real, and un-rebasable -------------------------------------------


def test_cursor_walks_a_trajectory_without_skipping_or_duplicating(client, app):
    rid, _, _, _ = _populated(client, app, spans=40)
    service = _service(client)

    seen: list[str] = []
    cursor, pages = None, 0
    while True:
        result = service.research_get(
            f"run:{rid}", view="trajectory", token_budget=400, cursor=cursor
        )
        seen.extend(s["id"] for s in result["data"]["spans"])
        cursor = result["next_cursor"]
        pages += 1
        if cursor is None or pages > 50:
            break

    assert pages > 1, "budget did not force pagination; the walk proves nothing"
    assert seen == [f"span-{i}" for i in range(40)]
    assert len(seen) == len(set(seen))


def test_backend_ceiling_marker_reflects_the_backend_not_the_offset(client, app):
    """`spans_beyond_backend_limit` must mean the BACKEND refused to go further.

    Deriving it as `offset + len(rows) >= 10000` fired on a short run read at a high
    offset — a false `missing` marker on a trajectory the agent had seen in full.
    A wrong entry in `missing` corrupts the one signal the envelope exists to
    carry, so it is worse than no marker at all."""
    rid, _, _, _ = _populated(client, app, spans=3)
    cursor = json.dumps({"offset": 9_900, "view": "trajectory"}, sort_keys=True)
    result = _service(client).research_get(f"run:{rid}", view="trajectory", cursor=cursor)

    assert result["data"]["spans"] == []  # read past the end
    assert result["completeness"]["missing"] == []  # ... and says nothing is hidden
    assert result["completeness"]["state"] == "complete"


def test_cursor_from_another_view_is_rejected_not_rebased(client, app):
    """Offset 40 of a trajectory means nothing in an events list; silently
    reinterpreting it would skip 40 events with no signal."""
    rid, _, _, _ = _populated(client, app, spans=40)
    service = _service(client)
    cursor = service.research_get(f"run:{rid}", view="trajectory", token_budget=400)["next_cursor"]

    with pytest.raises(errors.ValidationError) as excinfo:
        service.research_get(f"run:{rid}", view="events", cursor=cursor)
    assert "issued for view='trajectory'" in str(excinfo.value)


def test_malformed_cursor_raises_validation_error(client, app):
    rid, _, _, _ = _populated(client, app)
    with pytest.raises(errors.ValidationError):
        _service(client).research_get(f"run:{rid}", view="trajectory", cursor="not-json")


def test_atomic_view_cannot_be_paginated(client, app):
    rid, _, _, _ = _populated(client, app)
    cursor = json.dumps({"offset": 5, "view": "reproduce"}, sort_keys=True)
    with pytest.raises(errors.ValidationError) as excinfo:
        _service(client).research_get(f"run:{rid}", view="reproduce", cursor=cursor)
    assert "cannot be paginated" in str(excinfo.value)


# -- filters are honest -------------------------------------------------------


def test_unknown_filter_is_rejected_with_the_supported_set(client, app):
    """A silently-ignored filter returns a full result set the agent believes was
    narrowed."""
    rid, _, _, _ = _populated(client, app)
    with pytest.raises(errors.ValidationError) as excinfo:
        _service(client).research_get(f"run:{rid}", view="trajectory", filters={"nope": 1})
    assert "span_type" in str(excinfo.value)


def test_filters_are_rejected_on_a_view_that_cannot_honor_them(client, app):
    """GET /v1/experiments/{id}/artifacts takes no filters, so `kind` is honest on a
    run's artifacts and a lie on an experiment's."""
    _, experiment_id, _, _ = _populated(client, app)
    with pytest.raises(errors.ValidationError) as excinfo:
        _service(client).research_get(
            f"experiment:{experiment_id}", view="artifacts", filters={"kind": "figure"}
        )
    assert "accepts no filters" in str(excinfo.value)


def test_metrics_view_drills_from_series_summary_to_raw_points(client, app):
    """Progressive disclosure INSIDE a view: summaries by default, points on
    request — rather than dumping every metric point a run ever logged."""
    rid, _, _, _ = _populated(client, app)
    service = _service(client)

    summary = service.research_get(f"run:{rid}", view="metrics")["data"]
    assert summary["granularity"] == "series_summary"
    assert [s["key"] for s in summary["series"]] == ["loss"]

    points = service.research_get(f"run:{rid}", view="metrics", filters={"key": "loss"})["data"]
    assert points["granularity"] == "points"
    assert [p["value"] for p in points["points"]] == [0.9, 0.3]


# -- the capability map is not a permanent lie --------------------------------


def test_research_context_can_finally_report_complete(client, app):
    """`missing` was derived from every False capability flag, and four were
    hardcoded False — so EVERY context envelope was partial no matter what it
    returned, which trains agents to ignore the signal. `portable_snapshots` is
    still honestly False and must NOT, by itself, make the answer partial."""
    _populated(client, app)
    result = _service(client).research_context("dockq")

    assert result["capabilities"]["versioned_assets"] is True
    assert result["capabilities"]["managed_artifact_upload"] is True
    assert result["capabilities"]["portable_snapshots"] is False  # honest, and not "missing"
    assert "promotion_manifests" not in result["capabilities"]  # rejected, not "coming"
    assert result["completeness"]["missing"] == []
    assert result["completeness"]["state"] == "complete"
    assert result["data"]["warnings"] == []


def test_research_context_reads_official_assets_instead_of_claiming_none(client, app):
    """official_assets was hardcoded []. That was survivable only while a warning
    said the registry was unavailable; with versioned_assets now True, an empty
    list would read as "there are no official assets" — an answer nobody looked
    for. The registry has been live the whole time."""
    _populated(client, app)
    asset = client.assets.register("dockq-scorer", kind="method")
    client.assets.add_version(asset["id"], content_hash="sha256:abc", uri="s3://b/v1.py", label="v1")

    result = _service(client).research_context("dockq", token_budget=100_000)
    assert [a["name"] for a in result["data"]["official_assets"]] == ["dockq-scorer"]


def test_research_context_token_budget_bounds_its_lists(client, app):
    """The other inert knob: research_context accepted token_budget and echoed it
    straight back into the payload, bounding nothing."""
    for i in range(30):
        client.assets.register(f"asset-{i}", kind="dataset", description="x" * 200)
    service = _service(client)

    big = service.research_context("dockq", token_budget=100_000)
    small = service.research_context("dockq", token_budget=400)

    assert len(small["data"]["official_assets"]) < len(big["data"]["official_assets"])
    assert len(json.dumps(small["data"], default=str)) // 4 <= 400 * 1.5
    assert "truncated_by_token_budget" in small["completeness"]["missing"]
    assert small["completeness"]["state"] == "partial"
    assert "token_budget" not in small["data"]  # the echo is gone, not decorated
