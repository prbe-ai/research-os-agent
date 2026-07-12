"""SDK behavior against the fake v3 API."""

from __future__ import annotations

import json

import pytest

from ros import errors


def test_run_high_level_creates_experiment_and_run(client, app):
    run = client.run(experiment="dockq-sweep", hypothesis="temp 0.7 wins", name="run-1")
    assert run.id in app.runs
    # one POST /v1/experiments, one POST .../runs
    posts = [(r.method, r.url.path) for r in app.requests]
    assert ("POST", "/v1/experiments") in posts
    assert any(p[0] == "POST" and p[1].endswith("/runs") for p in posts)


def test_ensure_experiment_conflict_fetches_existing(client, app):
    app.experiment_conflict_id = "existing-123"
    exp = client.ensure_experiment("dockq", "DockQ", "h")
    assert exp["id"] == "existing-123"


def test_log_metrics(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    run.log({"loss": 0.42, "dockq": 0.71}, step=42)
    assert app.metrics_inserted == 2
    body = json.loads(app.requests[-1].content)
    assert body["points"][0]["step_index"] == 42


def test_log_hw_sends_real_dimensions(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    run.log_hw({"gpu_temp": 88.0}, device=3, host="n1")
    body = json.loads(app.requests[-1].content)
    point = body["points"][0]
    assert point["key"] == "gpu_temp"  # key is clean; dims are first-class now
    assert point["kind"] == "hardware"
    assert point["dimensions"] == {"device": 3, "host": "n1"}


def test_log_dimensions_passthrough(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    run.log({"loss": 0.1}, step=1, dimensions={"rank": 0})
    body = json.loads(app.requests[-1].content)
    assert body["points"][0]["dimensions"] == {"rank": 0}


def test_span_generates_uuid_and_posts(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    span_id = run.span("rollout", name="rollout-0", step_index=1)
    assert app.spans_upserted == 1
    body = json.loads(app.requests[-1].content)
    assert body["spans"][0]["id"] == span_id
    assert body["spans"][0]["span_type"] == "rollout"


def test_link_merges_foreign_keys_into_metadata(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    run.link(wandb_run_id="abc", s3_prefix="s3://x/y")
    row = app.runs[run.id]
    assert row["metadata"]["foreign_keys"] == {"wandb_run_id": "abc", "s3_prefix": "s3://x/y"}


def test_artifact_with_uri(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    run.log_artifact("final.sif", uri="r2://bucket/final.sif", kind="artifact")
    body = json.loads(app.requests[-1].content)
    assert body["uri"] == "r2://bucket/final.sif"
    assert body["name"] == "final.sif"


def test_finish_sets_status_and_ended_at(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    run.finish("completed")
    row = app.runs[run.id]
    assert row["status"] == "completed"
    assert row["ended_at"] is not None


def test_context_manager_marks_failed_on_exception(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    with pytest.raises(ValueError):
        with run:
            raise ValueError("boom")
    assert app.runs[run.id]["status"] == "failed"


def test_fail_open_spools_on_error_then_flush(app, tmp_path):
    from tests.conftest import make_client

    c = make_client(app, tmp_spool=tmp_path / "spool")
    run = c.run(experiment="e", hypothesis="h", name="r")
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
    run = c.run(experiment="e", hypothesis="h", name="r")
    app.fail_next_metrics = True
    with pytest.raises(errors.RosError):
        run.log({"loss": 1.0}, strict=True)


def test_ingest_push(client, app):
    out = client.ingest(
        experiment_slug="dockq",
        experiment_hypothesis="h",
        run={"name": "r1", "source": "temporal", "external_id": "wf-1", "status": "running"},
        metrics=[{"kind": "model", "key": "loss", "value": 0.5, "step_index": 1}],
        strict=True,
    )
    assert out["name"] == "r1"
    # HMAC signature attached on the ingest path
    ingest_req = [r for r in app.requests if r.url.path == "/ingest/v1/runs"][0]
    assert ingest_req.headers.get("X-Signature", "").startswith("sha256=")
    assert ingest_req.headers["Authorization"] == "Bearer ros_ing_cafef00d"


def test_ingest_validates_client_side(client, app):
    import pytest as _pytest

    # missing run.external_id -> the generated IngestRunRequest rejects it before
    # any HTTP call is made (no request recorded).
    before = len(app.requests)
    with _pytest.raises(Exception):
        client.ingest(
            experiment_slug="e",
            run={"name": "r1", "source": "temporal"},  # no external_id
            strict=True,
        )
    assert len(app.requests) == before, "should fail before sending"


def test_error_mapping_409(app, tmp_path):
    from tests.conftest import make_client

    c = make_client(app, tmp_spool=tmp_path / "spool")
    app.experiment_conflict_id = None
    # force a 409 with existing_id by posting the same slug via a conflict knob
    app.experiment_conflict_id = "e-9"
    exp = c.ensure_experiment("dup", "Dup", "h")
    assert exp["id"] == "e-9"


# -- v0.4 fold-in Phase 1 -----------------------------------------------------
def test_artifact_presign_upload(client, app, tmp_path):
    run = client.run(experiment="e", hypothesis="h", name="r")
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


def test_artifact_reference_still_metadata_only(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    run.log_artifact("final.sif", uri="r2://bucket/final.sif", kind="artifact")
    body = json.loads(app.requests[-1].content)
    assert body["uri"] == "r2://bucket/final.sif"
    assert body["is_reference"] is True


def test_asset_register_and_version(client, app):
    client.fail_open = False
    asset = client.assets.register("dockq-scorer", kind="script")
    assert asset["name"] == "dockq-scorer"
    v = client.assets.add_version(asset["id"], content_hash="a" * 64, label="v1")
    assert v["version"] == 1
    assert client.assets.versions(asset["id"])[0]["label"] == "v1"


def test_asset_resolve_match_and_no_match(client, app):
    client.fail_open = False
    asset = client.assets.register("eval-set", kind="dataset")
    client.assets.add_version(asset["id"], content_hash="b" * 64, label="v1")
    hit = client.assets.resolve("eval-set")
    assert hit["state"] == "match" and hit["selected"]["label"] == "v1"
    miss = client.assets.resolve("nope")
    assert miss["state"] == "no_match"


def test_add_edge(client, app):
    client.fail_open = False
    run = client.run(experiment="e", hypothesis="h", name="train")
    other = client.run(experiment="e", hypothesis="h", name="eval")
    client.add_edge(
        source_type="run", source_id=run.id, relation="evaluates_on",
        target_type="run", target_id=other.id,
    )
    edges = run.edges()
    assert edges and edges[0]["relation"] == "evaluates_on"


def test_experiment_version_create_and_list(client, app):
    client.fail_open = False
    exp = client.ensure_experiment("dockq", "DockQ", "h")
    v = client.experiment_version(exp["id"], label="launch-1")
    assert v["version"] == 1
    assert client.list_experiment_versions(exp["id"])[0]["label"] == "launch-1"


def test_ingest_execution_record_and_foreign_keys_passthrough(client, app):
    out = client.ingest(
        experiment_slug="dockq",
        experiment_hypothesis="h",
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
    run = client.run(experiment="e", hypothesis="h", name="r")
    assert run.short_id and run.short_id.startswith("run-")
    assert run.foreign_keys == {}
