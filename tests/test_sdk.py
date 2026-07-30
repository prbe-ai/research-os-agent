"""SDK behavior against the fake v3 API."""

from __future__ import annotations

import json
import sys
import threading
import warnings

import pytest

from probe import errors
from probe._generated.models import ExperimentCreate, IngestRunRequest
from tests.conftest import open_run


def test_run_resolves_its_experiment_and_creates_only_the_run(client, app):
    """The central claim of explicit creation: run() writes exactly ONE thing.

    The old assertion checked that a POST /v1/experiments happened — which the
    `open_run` HELPER now issues, not run(). It would have passed even if run()
    never touched experiments at all, so it could not see the behaviour it was
    named for. Snapshot the request trail AFTER the explicit create and assert
    run() adds a run and nothing else.
    """
    project = client.create_project("dockq-project")
    client.create_experiment(
        "dockq-sweep", "DockQ", hypothesis="h", project_id=project["id"]
    )
    before = len(app.requests)

    run = client.run(experiment="dockq-sweep", name="run-1")

    assert run.id in app.runs
    after = [(r.method, r.url.path) for r in app.requests[before:]]
    assert ("POST", "/v1/experiments") not in after, "run() created an experiment"
    assert ("POST", "/v1/projects") not in after, "run() created a project"
    posts = [p for p in after if p[0] == "POST"]
    assert len(posts) == 1 and posts[0][1].endswith("/runs"), posts


def test_creating_a_taken_slug_raises_instead_of_returning_the_existing_one(client, app):
    """Inverted deliberately. Returning the existing row on conflict is what
    'get-or-create' MEANT, and it is the behaviour that made `probe run start`
    able to conjure a chain — so the conflict has to surface."""
    app.experiment_conflict_id = "existing-123"
    with pytest.raises(errors.ConflictError):
        client.create_experiment("dockq", "DockQ", hypothesis="h", project_id="p")


def test_log_metrics(client, app):
    run = open_run(client, experiment="e", name="r")
    run.log({"loss": 0.42, "dockq": 0.71}, step=42)
    assert app.metrics_inserted == 2
    body = json.loads(app.requests[-1].content)
    assert body["points"][0]["step_index"] == 42


def test_log_hw_sends_real_dimensions(client, app):
    run = open_run(client, experiment="e", name="r")
    run.log_hw({"gpu_temp": 88.0}, device=3, host="n1")
    body = json.loads(app.requests[-1].content)
    point = body["points"][0]
    assert point["key"] == "gpu_temp"  # key is clean; dims are first-class now
    assert point["kind"] == "hardware"
    assert point["dimensions"] == {"device": 3, "host": "n1"}


def test_log_dimensions_passthrough(client, app):
    run = open_run(client, experiment="e", name="r")
    run.log({"loss": 0.1}, step=1, dimensions={"rank": 0})
    body = json.loads(app.requests[-1].content)
    assert body["points"][0]["dimensions"] == {"rank": 0}


# -- non-numeric values --------------------------------------------------------
def _attrs(app, run, step=0):
    return app.steps[run.id][step]["attributes"]


def test_mixed_types_in_one_call_split_by_where_they_can_be_stored(client, app):
    """`metric_points.value` is DOUBLE PRECISION NOT NULL, so this is a schema
    boundary, not a preference. Numbers plot; everything else lands in that
    step's record, at the same step index."""
    run = open_run(client, experiment="e", name="r")
    run.log({"loss": 0.4, "phase": "eval", "cfg": {"lr": 3e-4}}, step=7)

    points = json.loads(app.requests[-2].content)["points"]
    assert [(p["key"], p["value"]) for p in points] == [("loss", 0.4)]
    assert _attrs(app, run, 7) == {"phase": "eval", "cfg": {"lr": 3e-4}}


def test_a_string_no_longer_takes_its_numeric_neighbours_down(client, app):
    """The real bug this fixes. `float()` ran while building MetricBatch — before
    client.write()'s try/except — so one non-numeric key raised out of the
    training loop AND discarded every numeric metric in the same call."""
    run = open_run(client, experiment="e", name="r")
    run.log({"loss": 0.4, "dockq": 0.8, "note": "converged"}, step=1)

    points = json.loads(app.requests[-2].content)["points"]
    assert {p["key"] for p in points} == {"loss", "dockq"}
    assert _attrs(app, run, 1) == {"note": "converged"}


def test_types_survive_the_round_trip(client, app):
    run = open_run(client, experiment="e", name="r")
    run.log({"s": "text", "d": {"a": 1}, "l": [1, 2], "n": None}, step=0)
    assert _attrs(app, run) == {"s": "text", "d": {"a": 1}, "l": [1, 2], "n": None}


def test_a_numeric_string_stays_a_string(client, app):
    """`float("0.4")` parses, so coercing would silently make "0.4" and 0.4
    indistinguishable afterwards. A caller who logged a string meant one."""
    run = open_run(client, experiment="e", name="r")
    run.log({"version": "0.4"}, step=0)
    assert _attrs(app, run) == {"version": "0.4"}


def test_bools_stay_plottable(client, app):
    """W&B charts bools as 0/1, and a chart is what people log them for."""
    run = open_run(client, experiment="e", name="r")
    run.log({"converged": True, "diverged": False}, step=0)
    points = json.loads(app.requests[-1].content)["points"]
    assert {p["key"]: p["value"] for p in points} == {"converged": 1.0, "diverged": 0.0}
    assert run.id not in app.steps


def test_anything_with_a_float_dunder_is_a_metric(client, app):
    """numpy scalars and 0-d torch tensors arrive this way and must not be
    demoted to attributes just because they are not `float`."""

    class Scalar:
        def __float__(self):
            return 0.25

    run = open_run(client, experiment="e", name="r")
    run.log({"loss": Scalar()}, step=0)
    assert json.loads(app.requests[-1].content)["points"][0]["value"] == 0.25


def test_an_unserialisable_value_is_kept_as_repr_not_raised(client, app):
    """attributes is JSONB, so this would otherwise fail at encode time — inside
    the loop, past the fail-open boundary. repr loses fidelity, never the loop."""
    run = open_run(client, experiment="e", name="r")
    with pytest.warns(UserWarning, match="not JSON-serialisable"):
        run.log({"seen": {1, 2}}, step=0)
    assert isinstance(_attrs(app, run)["seen"], str)


def test_non_numeric_needs_a_step_and_says_so_when_there_is_none(client, app):
    """A step record is keyed by step_index. `step=None` explicitly means "no step
    axis", so there is nowhere to put these — but the numeric ones still land."""
    run = open_run(client, experiment="e", name="r")
    with pytest.warns(UserWarning, match="dropped non-numeric"):
        run.log({"loss": 0.4, "phase": "eval"}, step=None)
    points = json.loads(app.requests[-1].content)["points"]
    assert {p["key"] for p in points} == {"loss"}
    assert app.steps == {}


def test_the_return_value_still_reports_the_metric_write(client, app):
    """connectors/harbor.py keys "confirmed" vs "spooled" off this."""
    run = open_run(client, experiment="e", name="r")
    assert run.log({"loss": 0.4, "phase": "eval"}, step=0) is not None
    # with nothing numeric to write, the step record's result stands in
    assert run.log({"phase": "eval"}, step=1) is not None


# -- auto-increment step -------------------------------------------------------
def _points(app):
    return json.loads(app.requests[-1].content)["points"]


def test_a_bare_log_auto_increments_the_step(client, app):
    """The ported W&B loop. `for batch in loader: run.log({"loss": l})` has to
    produce a curve; without a counter every point arrives with no step at all and
    lands on the wall-clock axis as an unordered pile."""
    run = open_run(client, experiment="e", name="r")
    steps = []
    for loss in (0.5, 0.4, 0.3):
        run.log({"loss": loss})
        steps.append(_points(app)[0]["step_index"])
    assert steps == [0, 1, 2]


def test_every_key_in_one_call_shares_a_step(client, app):
    run = open_run(client, experiment="e", name="r")
    run.log({"loss": 0.5, "dockq": 0.7})
    assert {p["step_index"] for p in _points(app)} == {0}
    run.log({"loss": 0.4, "dockq": 0.8})
    assert {p["step_index"] for p in _points(app)} == {1}


def test_an_explicit_none_still_means_no_step(client, app):
    """What the CLI, the Miles exporter and the Harbor importer all pass. Each
    invocation of `probe log` is its own process, so a per-handle counter would
    restart at 0 every time — worse than no step. Only an OMITTED step opts in."""
    run = open_run(client, experiment="e", name="r")
    run.log({"loss": 0.5}, step=None)
    assert "step_index" not in _points(app)[0]


def test_an_explicit_step_is_used_and_moves_the_counter_past_it(client, app):
    """Mixing the two forms must not stack a second series on steps the loop
    already used."""
    run = open_run(client, experiment="e", name="r")
    run.log({"loss": 0.5}, step=41)
    assert _points(app)[0]["step_index"] == 41
    run.log({"loss": 0.4})
    assert _points(app)[0]["step_index"] == 42


def test_hardware_and_model_counters_are_independent(client, app):
    """A GPU sampler on its own thread must not shift the loss curve."""
    run = open_run(client, experiment="e", name="r")
    run.log({"loss": 0.5})           # model 0
    run.log_hw({"gpu_temp": 88})     # hardware 0
    run.log_hw({"gpu_temp": 89})     # hardware 1
    assert _points(app)[0]["step_index"] == 1
    run.log({"loss": 0.4})           # model 1, unaffected by the two hw points
    assert _points(app)[0]["step_index"] == 1


def test_concurrent_logging_never_reuses_a_step(client, app):
    """Logging from several threads is ordinary — a sampler beside a training
    loop — and two threads reading the counter unguarded would put two different
    points on one step."""
    run = open_run(client, experiment="e", name="r")
    seen: list[int] = []
    lock = threading.Lock()
    threads_n, per_thread = 8, 2000

    def worker():
        for _ in range(per_thread):
            step = run._next_step("model")
            with lock:
                seen.append(step)

    # Without this the test is vacuous: the critical section is three bytecodes,
    # so at the default 5ms switch interval an UNLOCKED counter passes too —
    # verified by deleting the lock and watching the suite stay green.
    previous = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous)

    assert sorted(seen) == list(range(threads_n * per_thread))


def test_span_generates_uuid_and_posts(client, app):
    run = open_run(client, experiment="e", name="r")
    span_id = run.span("rollout", name="rollout-0", step_index=1)
    assert app.spans_upserted == 1
    body = json.loads(app.requests[-1].content)
    assert body["spans"][0]["id"] == span_id
    assert body["spans"][0]["span_type"] == "rollout"


# -- span scopes --------------------------------------------------------------
def _last_state(app, run, span_id):
    """The most recent upsert of one span. The fake appends rather than merging,
    so an upserted span appears once per write."""
    return [s for s in app.spans[run.id] if s["id"] == span_id][-1]


def test_a_span_id_is_still_an_ordinary_string(client, app):
    """SpanHandle subclasses str so the two-call form keeps working untouched —
    callers store it, format it, and pass it back as `id=`."""
    run = open_run(client, experiment="e", name="r")
    span_id = run.span("rollout", name="rollout-0")
    assert isinstance(span_id, str)
    assert f"{span_id}" == str(span_id)
    assert {span_id: 1}[span_id] == 1
    run.span("rollout", id=span_id, status="completed", ended_at="2026-07-27T00:00:00Z")
    assert _last_state(app, run, span_id)["status"] == "completed"


def test_a_span_scope_closes_the_span_on_the_way_out(client, app):
    run = open_run(client, experiment="e", name="r")
    with run.span("rollout", name="rollout-0") as span:
        assert _last_state(app, run, span)["status"] == "running"
    final = _last_state(app, run, span)
    assert final["status"] == "completed"
    assert final["ended_at"] is not None
    assert final["started_at"] is not None


def test_a_raising_body_fails_the_span_rather_than_leaving_it_running(client, app):
    """The reason this exists. Spans have no heartbeat and no server-side reaper,
    so a span abandoned by a raise stays `running` forever with nothing to correct
    it. The exception still propagates — the span records the failure, it does not
    swallow it."""
    run = open_run(client, experiment="e", name="r")
    with pytest.raises(ValueError, match="rollout diverged"):
        with run.span("rollout", name="rollout-0") as span:
            raise ValueError("rollout diverged")
    final = _last_state(app, run, span)
    assert final["status"] == "failed"
    assert final["ended_at"] is not None
    assert final["attributes"]["error.type"] == "ValueError"
    assert final["attributes"]["error.message"] == "rollout diverged"


def test_attributes_set_inside_the_block_are_sent_on_close(client, app):
    run = open_run(client, experiment="e", name="r")
    with run.span("rollout", name="rollout-0", attributes={"task": "fold"}) as span:
        span.attributes["reward"] = 0.8
    final = _last_state(app, run, span)
    assert final["attributes"] == {"task": "fold", "reward": 0.8}


def test_spans_nest_without_threading_the_parent_by_hand(client, app):
    run = open_run(client, experiment="e", name="r")
    with run.span("rollout", name="rollout-0") as rollout:
        with run.span("turn", name="turn-0") as turn:
            tool = run.span("tool_call", name="search")
        after_turn = run.span("turn", name="turn-1")
    detached = run.span("rollout", name="rollout-1")

    assert _last_state(app, run, rollout)["parent_span_id"] is None
    assert _last_state(app, run, turn)["parent_span_id"] == str(rollout)
    # a plain (non-scope) span still adopts the enclosing scope
    assert _last_state(app, run, tool)["parent_span_id"] == str(turn)
    # ...and the scope is popped on exit, so the next sibling re-parents correctly
    assert _last_state(app, run, after_turn)["parent_span_id"] == str(rollout)
    assert _last_state(app, run, detached)["parent_span_id"] is None


def test_an_explicit_none_is_honoured_so_replays_are_not_fabricated(client, app):
    """The ATIF expander and the Harbor importer replay STORED trajectories and
    pass `started_at=<maybe None>` on purpose — a stored step may have no usable
    timestamp. Defaulting those to now() would write a fabricated time into a
    historical record, so only an OMITTED argument gets a default."""
    run = open_run(client, experiment="e", name="r")
    replayed = run.span("rollout", name="old", started_at=None, parent_span_id=None)
    live = run.span("rollout", name="new")

    assert _last_state(app, run, replayed)["started_at"] is None
    assert _last_state(app, run, live)["started_at"] is not None


def test_a_replayed_span_is_not_reparented_by_an_enclosing_scope(client, app):
    run = open_run(client, experiment="e", name="r")
    with run.span("rollout", name="rollout-0"):
        replayed = run.span("turn", name="stored", parent_span_id=None)
    assert _last_state(app, run, replayed)["parent_span_id"] is None


def test_link_writes_real_foreign_keys_column(client, app):
    run = open_run(client, experiment="e", name="r")
    run.link(wandb_run_id="abc", s3_prefix="s3://x/y")
    # real runs.foreign_keys column (not metadata), server-merged
    assert app.runs[run.id]["foreign_keys"] == {"wandb_run_id": "abc", "s3_prefix": "s3://x/y"}
    # a later link merges per-key new-wins (overwrite one, keep the rest)
    run.link(wandb_run_id="def")
    assert app.runs[run.id]["foreign_keys"] == {"wandb_run_id": "def", "s3_prefix": "s3://x/y"}
    assert "foreign_keys" not in (app.runs[run.id].get("metadata") or {})


def test_artifact_with_uri(client, app):
    run = open_run(client, experiment="e", name="r")
    run.log_artifact("final.sif", uri="r2://bucket/final.sif", kind="artifact")
    body = json.loads(app.requests[-1].content)
    assert body["uri"] == "r2://bucket/final.sif"
    assert body["name"] == "final.sif"


def test_artifact_path_reference_records_pointer_without_uploading(client, app, tmp_path):
    run = open_run(client, experiment="e", name="r")
    f = tmp_path / "ckpt.pt"
    f.write_bytes(b"x" * 2048)
    run.log_artifact("ckpt.pt", path=str(f), reference=True)
    req = app.requests[-1]
    # Direct create door, NOT the presign /uploads flow -- no bytes are uploaded.
    assert req.url.path == f"/v1/runs/{run.id}/artifacts"
    body = json.loads(req.content)
    assert body["is_reference"] is True
    assert body["uri"].startswith("file://") and body["uri"].endswith("/ckpt.pt")
    assert body["meta"]["local_path"] == str(f)
    assert body["meta"]["host"]
    assert body["size_bytes"] == 2048  # os.stat, not a read
    assert "content_hash" not in body  # no --hash -> no whole-file read


def test_artifact_path_reference_hash_opts_into_fingerprint(client, app, tmp_path):
    run = open_run(client, experiment="e", name="r")
    f = tmp_path / "big.bin"
    f.write_bytes(b"y" * 4096)
    run.log_artifact("big.bin", path=str(f), reference=True, hash_content=True)
    body = json.loads(app.requests[-1].content)
    assert body["is_reference"] is True
    assert len(body["content_hash"]) == 64  # sha256 hex
    assert body["size_bytes"] == 4096


def test_artifact_path_reference_missing_path_raises_unless_allowed(client, app):
    run = open_run(client, experiment="e", name="r")
    with pytest.raises(FileNotFoundError):
        run.log_artifact("gone.pt", path="/no/such/file.pt", reference=True)
    # allow_missing records it anyway (it may live on a mount/host this machine lacks).
    run.log_artifact("gone.pt", path="/mnt/shared/gone.pt", reference=True, allow_missing=True)
    body = json.loads(app.requests[-1].content)
    assert body["is_reference"] is True
    assert body["uri"] == "file:///mnt/shared/gone.pt"
    assert "size_bytes" not in body  # not stat-able here


def test_finish_sets_status_and_ended_at(client, app):
    run = open_run(client, experiment="e", name="r")
    run.finish("completed")
    row = app.runs[run.id]
    assert row["status"] == "completed"
    assert row["ended_at"] is not None


def test_context_manager_marks_failed_on_exception(client, app):
    run = open_run(client, experiment="e", name="r")
    with pytest.raises(ValueError):
        with run:
            raise ValueError("boom")
    assert app.runs[run.id]["status"] == "failed"


def test_fail_open_spools_on_error_then_flush(app, tmp_path):
    from tests.conftest import make_client

    c = make_client(app, tmp_spool=tmp_path / "spool")
    run = open_run(c, experiment="e", name="r")
    app.fail_next_metrics = True
    # fail-open: the failing metrics call is spooled, does not raise
    run.log({"loss": 1.0}, step=1)
    assert c.spool.pending(), "expected the failed write to be spooled"
    # replay succeeds now
    sent = c.flush()
    assert sent == 1
    assert not c.spool.pending()


def test_strict_write_raises(app, tmp_path):
    from tests.conftest import make_client

    c = make_client(app, fail_open=False, tmp_spool=tmp_path / "spool")
    run = open_run(c, experiment="e", name="r")
    app.fail_next_metrics = True
    with pytest.raises(errors.RosError):
        run.log({"loss": 1.0}, strict=True)


def test_ingest_push(client, app):
    out = client.ingest(
        project_slug="dockq-project",
        experiment_slug="dockq",
        run={"name": "r1", "source": "temporal", "external_id": "wf-1", "status": "running"},
        metrics=[{"kind": "model", "key": "loss", "value": 0.5, "step_index": 1}],
        strict=True,
    )
    assert out["name"] == "r1"
    # HMAC signature attached on the ingest path
    ingest_req = [r for r in app.requests if r.url.path == "/ingest/v1/runs"][0]
    assert json.loads(ingest_req.content)["project_slug"] == "dockq-project"
    assert ingest_req.headers.get("X-Signature", "").startswith("sha256=")
    assert ingest_req.headers["Authorization"] == "Bearer ros_ing_cafef00d"


def test_ingest_requires_project_and_removed_hypothesis_keyword(client):
    run = {"name": "r1", "source": "temporal", "external_id": "wf-1"}
    with pytest.raises(TypeError, match="project_slug"):
        client.ingest(experiment_slug="dockq", run=run)
    with pytest.raises(TypeError, match="experiment_hypothesis"):
        client.ingest(
            project_slug="dockq-project",
            experiment_slug="dockq",
            experiment_hypothesis="removed",
            run=run,
        )


def test_generated_contract_requires_projects_and_removes_ingest_hypothesis():
    experiment = ExperimentCreate.model_json_schema()
    ingest = IngestRunRequest.model_json_schema()
    assert "project_id" in experiment["required"]
    assert "project_slug" in ingest["required"]
    assert "experiment_hypothesis" not in ingest["properties"]


def test_ingest_validates_client_side(client, app):
    import pytest as _pytest

    # missing run.external_id -> the generated IngestRunRequest rejects it before
    # any HTTP call is made (no request recorded).
    before = len(app.requests)
    with _pytest.raises(Exception):
        client.ingest(
            project_slug="project",
            experiment_slug="e",
            run={"name": "r1", "source": "temporal"},  # no external_id
            strict=True,
        )
    assert len(app.requests) == before, "should fail before sending"


def test_error_mapping_409(app, tmp_path):
    """A taken slug RAISES now. It used to be swallowed: create posted, caught the
    409, and re-fetched the existing row — which is what made a typo silently
    attach to (or mint) the wrong identity instead of failing."""
    from tests.conftest import make_client

    c = make_client(app, tmp_spool=tmp_path / "spool")
    app.experiment_conflict_id = "e-9"
    with pytest.raises(errors.ConflictError) as caught:
        c.create_experiment("dup", "Dup", hypothesis="h", project_id="p")
    assert caught.value.existing_id == "e-9"


# -- v0.4 fold-in Phase 1 -----------------------------------------------------
def test_artifact_presign_upload(client, app, tmp_path):
    run = open_run(client, experiment="e", name="r")
    f = tmp_path / "ckpt.bin"
    f.write_bytes(b"weights")
    client.fail_open = False  # strict: real upload path
    result = run.log_artifact("ckpt.bin", path=str(f), strict=True)
    assert result["status"] == "complete"
    # presign -> PUT to r2 -> confirm
    paths = [r.url.path for r in app.requests]
    assert any(p.endswith("/artifacts/uploads") for p in paths)
    assert app.puts, "expected a PUT of the bytes to the presigned URL"
    assert any(p.endswith("/confirm") for p in paths)


def test_artifact_presign_upload_sends_server_signed_headers(client, app, tmp_path):
    run = open_run(client, experiment="e", name="r")
    f = tmp_path / "ckpt.bin"
    f.write_bytes(b"weights")
    app.upload_headers = {"x-amz-checksum-sha256": "checksum"}
    client.fail_open = False

    run.log_artifact("ckpt.bin", path=str(f), strict=True)

    assert app.put_headers[-1]["x-amz-checksum-sha256"] == "checksum"


def test_artifact_reference_still_metadata_only(client, app):
    run = open_run(client, experiment="e", name="r")
    run.log_artifact("final.sif", uri="r2://bucket/final.sif", kind="artifact")
    body = json.loads(app.requests[-1].content)
    assert body["uri"] == "r2://bucket/final.sif"
    assert body["is_reference"] is True


def test_add_edge(client, app):
    client.fail_open = False
    run = open_run(client, experiment="e", name="train")
    other = open_run(client, experiment="e", name="eval")
    client.add_edge(
        source_type="run", source_id=run.id, relation="evaluates_on",
        target_type="run", target_id=other.id,
    )
    edges = run.edges()
    assert edges and edges[0]["relation"] == "evaluates_on"


def test_experiment_version_create_and_list(client, app):
    client.fail_open = False
    project = client.create_project("dockq-project")
    exp = client.create_experiment(
        "dockq", "DockQ", hypothesis="h", project_id=project["id"]
    )
    v = client.experiment_version(exp["id"], label="launch-1")
    assert v["version"] == 1
    assert client.list_experiment_versions(exp["id"])[0]["label"] == "launch-1"


def test_ingest_execution_record_and_foreign_keys_passthrough(client, app):
    out = client.ingest(
        project_slug="dockq-project",
        experiment_slug="dockq",
        run={"name": "r1", "source": "temporal", "external_id": "wf-1",
             "status": "running", "foreign_keys": {"wandb_run_id": "abc"}},
        execution_record={"code": {"git": {"commit": "x"}}, "deps": {"py": "3.12"}},
        metrics=[{"kind": "hardware", "key": "gpu_temp", "value": 88.0,
                  "dimensions": {"device": 3}}],
        strict=True,
    )
    assert out["name"] == "r1"
    body = json.loads([r for r in app.requests if r.url.path == "/ingest/v1/runs"][-1].content)
    assert body["run"]["foreign_keys"] == {"wandb_run_id": "abc"}
    assert body["execution_record"]["deps"] == {"py": "3.12"}
    assert body["metrics"][0]["dimensions"] == {"device": 3}


def test_run_exposes_short_id_and_foreign_keys(client, app):
    run = open_run(client, experiment="e", name="r")
    assert run.short_id and run.short_id.startswith("run-")
    assert run.foreign_keys == {}


# -- v0.4 fold-in Phase 2 -----------------------------------------------------
def test_snapshot_pins_real_env_ref_column(client, app, tmp_path):
    # snapshot() posts an execution record and pins runs.env_ref via RunPatch
    # (not metadata). Uses a throwaway git repo for the shadow-ref capture.
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "a.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    client.fail_open = False
    run = open_run(client, experiment="e", name="r")
    snap = run.snapshot(cwd=str(repo), include_env=False, include_gpu=False)
    assert snap["content_hash"]
    assert app.runs[run.id]["env_ref"] == snap["content_hash"]
    assert "env_ref" not in (app.runs[run.id].get("metadata") or {})


def test_snapshot_rejects_backend_that_drops_env_ref(client, app, tmp_path):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "a.txt").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    original_handler = app.handler

    def drop_env_ref(request):
        response = original_handler(request)
        if request.method == "PATCH" and request.url.path.startswith("/v1/runs/"):
            body = response.json()
            body["env_ref"] = None
            return httpx.Response(response.status_code, json=body)
        return response

    import httpx

    client.transport._client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(drop_env_ref))
    client.fail_open = False
    run = open_run(client, experiment="e", name="r")
    with pytest.raises(errors.CapabilityUnavailable, match="run.env_ref"):
        run.snapshot(cwd=str(repo), include_env=False, include_gpu=False, strict=True)


def test_artifact_upload_carries_kind_and_meta(client, app, tmp_path):
    """Harbor-ownership Phase 0: byte uploads are labeled like reference artifacts."""
    run = open_run(client, experiment="e", name="r")
    f = tmp_path / "trial.tar"
    f.write_bytes(b"sandbox-state")
    client.fail_open = False
    result = run.log_artifact(
        "trial-600",
        path=str(f),
        kind="harbor_trial",
        meta={"schema_version": "1.0", "trial": {"name": "swe__x"}},
        step_index=600,
        strict=True,
    )
    assert result["status"] == "complete"
    presign_body = json.loads(
        next(r for r in app.requests if r.url.path.endswith("/artifacts/uploads")).content
    )
    assert presign_body["kind"] == "harbor_trial"
    assert presign_body["meta"] == {"schema_version": "1.0", "trial": {"name": "swe__x"}}
    assert presign_body["step_index"] == 600
    stored = client.list_run_artifacts(run.id, kind="harbor_trial")
    assert [a["name"] for a in stored] == ["trial-600"]


def test_artifact_upload_default_kind_stays_file(client, app, tmp_path):
    """A plain upload omits kind (None on the wire) so restages preserve labels."""
    run = open_run(client, experiment="e", name="r")
    f = tmp_path / "ckpt.bin"
    f.write_bytes(b"weights")
    client.fail_open = False
    run.log_artifact("ckpt.bin", path=str(f), strict=True)
    presign_body = json.loads(
        next(r for r in app.requests if r.url.path.endswith("/artifacts/uploads")).content
    )
    assert "kind" not in presign_body  # exclude_none: absent, not "file"
    stored = client.list_run_artifacts(run.id)
    assert stored[0]["kind"] == "file"


def test_artifact_upload_fallback_keeps_kind_and_meta(client, app, tmp_path):
    """Fail-open fallback records the same label, not a bare 'file' reference."""
    run = open_run(client, experiment="e", name="r")
    f = tmp_path / "trial.tar"
    f.write_bytes(b"sandbox-state")
    app.fail_next_uploads = True
    with pytest.warns(UserWarning, match="recorded as a reference"):
        run.log_artifact(
            "trial-601", path=str(f), kind="harbor_trial", meta={"v": 1}, step_index=601
        )
    body = json.loads(app.requests[-1].content)
    assert body["kind"] == "harbor_trial"
    assert body["meta"]["v"] == 1
    assert body["meta"]["upload"] == "failed"
    assert body["is_reference"] is True


def test_list_run_artifacts_filters(client, app):
    """kind + inclusive step-window filters pass through as query params."""
    run = open_run(client, experiment="e", name="r")
    for step in (599, 600, 601):
        run.log_artifact(
            f"sandbox-{step}", uri=f"s3://lake/{step}", kind="sandbox_state", step_index=step
        )
    run.log_artifact("note", uri="s3://lake/note", kind="note")
    window = client.list_run_artifacts(run.id, kind="sandbox_state", step_from=599, step_to=601)
    assert sorted(a["name"] for a in window) == ["sandbox-599", "sandbox-600", "sandbox-601"]
    upper = client.list_run_artifacts(run.id, step_from=601)
    assert [a["name"] for a in upper] == ["sandbox-601"]
    request = app.requests[-1]
    assert request.url.params["step_from"] == "601"
    assert len(client.list_run_artifacts(run.id)) == 4


# -- regressions caught in pre-landing review ---------------------------------
def test_a_span_id_survives_copy_and_pickle(client, app):
    """Span ids were plain str before SpanHandle, and distributed training ships
    them across process boundaries (Ray, multiprocessing, checkpoint state).
    Reconstruction goes through __new__, which needs a live Run, so a handle has
    to degrade to its plain id rather than raise."""
    import copy
    import pickle

    run = open_run(client, experiment="e", name="r")
    span_id = run.span("rollout", name="x")
    assert copy.copy(span_id) == str(span_id)
    assert copy.deepcopy(span_id) == str(span_id)
    assert pickle.loads(pickle.dumps(span_id)) == str(span_id)
    assert type(pickle.loads(pickle.dumps(span_id))) is str


def test_the_two_call_close_does_not_rewrite_the_start_time(client, app):
    """`id=` means upsert, and stamping now() there would move the span's start to
    its close and collapse the duration to zero. Only a CREATE gets a start time;
    the close must send none, leaving the stored one alone."""
    run = open_run(client, experiment="e", name="r")
    span_id = run.span("rollout", name="x")
    assert _last_state(app, run, span_id)["started_at"] is not None, "create must stamp"

    run.span("rollout", id=span_id, status="completed", ended_at="2026-07-27T00:00:09Z")
    closed = _last_state(app, run, span_id)
    assert closed["status"] == "completed"
    assert closed["started_at"] is None, "the close fabricated a new start time"


def test_the_with_form_still_sends_its_real_start_time(client, app):
    """The counterpart: __exit__ re-sends the resolved start explicitly, so
    scoping a span must NOT lose the timestamp the fix stops re-stamping."""
    run = open_run(client, experiment="e", name="r")
    with run.span("rollout", name="x") as span:
        opened = _last_state(app, run, span)["started_at"]
    closed = _last_state(app, run, span)
    assert closed["started_at"] == opened
    assert closed["ended_at"] is not None


def test_a_spooled_metric_write_is_not_reported_as_confirmed(client, app):
    """connectors/harbor.py keys "confirmed" vs "spooled" off this return value,
    so a step record succeeding must not paper over numbers going to the spool."""
    run = open_run(client, experiment="e", name="r")
    app.fail_next_metrics = True
    assert run.log({"loss": 0.4, "phase": "eval"}, step=0) is None


def test_an_empty_log_does_not_burn_a_step(client, app):
    """`if metrics: run.log(metrics)` guards get written the other way round; an
    empty call must not shift the auto axis away from the loop index."""
    run = open_run(client, experiment="e", name="r")
    assert run.log({}) is None
    run.log({"loss": 0.5})
    assert _points(app)[0]["step_index"] == 0


def test_non_numeric_hardware_values_do_not_overwrite_the_model_step(client, app):
    """StepCreate has no `kind` — records are keyed on (run, step_index) alone —
    so a hardware value at hardware-step 0 would land on the model loop's step 0."""
    run = open_run(client, experiment="e", name="r")
    run.log({"loss": 0.5, "phase": "train"})          # model step 0, writes a record
    with pytest.warns(UserWarning, match="step records are keyed by step index"):
        run.log_hw({"driver": "535.x"})               # hardware step 0, must NOT merge
    assert app.steps[run.id][0]["attributes"] == {"phase": "train"}


def test_a_span_does_not_adopt_a_parent_from_a_different_run(client, app):
    """Spans are per-run, so parenting runB's span to runA's would write a
    dangling FK — a worse record than no parent at all."""
    run_a = open_run(client, experiment="e", name="a")
    run_b = open_run(client, experiment="e", name="b")
    with run_a.span("rollout", name="a-rollout"):
        stray = run_b.span("turn", name="b-turn")
    assert _last_state(app, run_b, stray)["parent_span_id"] is None


def test_span_attributes_never_raise_into_the_loop(client, app):
    """`attributes` is JSONB, so an unserialisable value blows up in model_dump()
    BEFORE the strict/spool boundary. In the `with` form the raise happens during
    unwinding and displaces the body's own exception as the visible failure — and
    the README promotes `span.attributes[...] = ...` as the primary idiom."""

    class Opaque:
        pass

    run = open_run(client, experiment="e", name="r")
    with pytest.warns(UserWarning, match="not JSON-serialisable"):
        direct = run.span("rollout", attributes={"o": Opaque()})
    assert isinstance(_last_state(app, run, direct)["attributes"]["o"], str)

    with pytest.warns(UserWarning, match="not JSON-serialisable"):
        with run.span("rollout") as scoped:
            scoped.attributes["o"] = Opaque()
    assert isinstance(_last_state(app, run, scoped)["attributes"]["o"], str)


def test_the_body_exception_survives_an_unserialisable_attribute(client, app):
    """The failure the caller needs to see is theirs, not a serialization error
    raised while closing the span on the way out."""
    run = open_run(client, experiment="e", name="r")
    with pytest.raises(ValueError, match="rollout diverged"):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            with run.span("rollout") as scoped:
                scoped.attributes["o"] = object()
                raise ValueError("rollout diverged")
    assert _last_state(app, run, scoped)["status"] == "failed"
