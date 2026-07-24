"""SDK behavior against the fake v3 API."""

from __future__ import annotations

import json

import pytest

from probe import errors


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


def test_link_writes_real_foreign_keys_column(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    run.link(wandb_run_id="abc", s3_prefix="s3://x/y")
    # real runs.foreign_keys column (not metadata), server-merged
    assert app.runs[run.id]["foreign_keys"] == {"wandb_run_id": "abc", "s3_prefix": "s3://x/y"}
    # a later link merges per-key new-wins (overwrite one, keep the rest)
    run.link(wandb_run_id="def")
    assert app.runs[run.id]["foreign_keys"] == {"wandb_run_id": "def", "s3_prefix": "s3://x/y"}
    assert "foreign_keys" not in (app.runs[run.id].get("metadata") or {})


def test_artifact_with_uri(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    run.log_artifact("final.sif", uri="r2://bucket/final.sif", kind="artifact")
    body = json.loads(app.requests[-1].content)
    assert body["uri"] == "r2://bucket/final.sif"
    assert body["name"] == "final.sif"


def test_artifact_path_reference_records_pointer_without_uploading(client, app, tmp_path):
    run = client.run(experiment="e", hypothesis="h", name="r")
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
    run = client.run(experiment="e", hypothesis="h", name="r")
    f = tmp_path / "big.bin"
    f.write_bytes(b"y" * 4096)
    run.log_artifact("big.bin", path=str(f), reference=True, hash_content=True)
    body = json.loads(app.requests[-1].content)
    assert body["is_reference"] is True
    assert len(body["content_hash"]) == 64  # sha256 hex
    assert body["size_bytes"] == 4096


def test_artifact_path_reference_missing_path_raises_unless_allowed(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    with pytest.raises(FileNotFoundError):
        run.log_artifact("gone.pt", path="/no/such/file.pt", reference=True)
    # allow_missing records it anyway (it may live on a mount/host this machine lacks).
    run.log_artifact("gone.pt", path="/mnt/shared/gone.pt", reference=True, allow_missing=True)
    body = json.loads(app.requests[-1].content)
    assert body["is_reference"] is True
    assert body["uri"] == "file:///mnt/shared/gone.pt"
    assert "size_bytes" not in body  # not stat-able here


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


def test_artifact_presign_upload_sends_server_signed_headers(client, app, tmp_path):
    run = client.run(experiment="e", hypothesis="h", name="r")
    f = tmp_path / "ckpt.bin"
    f.write_bytes(b"weights")
    app.upload_headers = {"x-amz-checksum-sha256": "checksum"}
    client.fail_open = False

    run.log_artifact("ckpt.bin", path=str(f), strict=True)

    assert app.put_headers[-1]["x-amz-checksum-sha256"] == "checksum"


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
    run = client.run(experiment="e", hypothesis="h", name="r")
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
    run = client.run(experiment="e", hypothesis="h", name="r")
    with pytest.raises(errors.CapabilityUnavailable, match="run.env_ref"):
        run.snapshot(cwd=str(repo), include_env=False, include_gpu=False, strict=True)


def test_asset_materialize_downloads_bytes(client, app, tmp_path):
    client.fail_open = False
    run = client.run(experiment="e", hypothesis="h", name="r")
    art = run.log_artifact("data.bin", uri="r2://b/data.bin", kind="artifact")
    asset = client.assets.register("eval-set", kind="dataset")
    client.assets.add_version(asset["id"], from_artifact_id=art["id"], label="v1")
    dest = tmp_path / "out.bin"
    result = client.assets.materialize("eval-set", str(dest))
    assert dest.read_bytes() == b"ASSET-BYTES"
    assert result["version"] == 1
    assert app.gets, "expected a presigned GET for the download"


def test_asset_materialize_requires_source_artifact(client, app, tmp_path):
    import pytest as _pytest

    client.fail_open = False
    asset = client.assets.register("hashonly", kind="dataset")
    client.assets.add_version(asset["id"], content_hash="a" * 64, label="v1")  # no source artifact
    with _pytest.raises(ValueError):
        client.assets.materialize("hashonly", str(tmp_path / "x"))


def test_artifact_upload_carries_kind_and_meta(client, app, tmp_path):
    """Harbor-ownership Phase 0: byte uploads are labeled like reference artifacts."""
    run = client.run(experiment="e", hypothesis="h", name="r")
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
    run = client.run(experiment="e", hypothesis="h", name="r")
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
    run = client.run(experiment="e", hypothesis="h", name="r")
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
    run = client.run(experiment="e", hypothesis="h", name="r")
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
