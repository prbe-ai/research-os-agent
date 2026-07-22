"""Retryable Miles -> Harbor connector export requests."""

from __future__ import annotations

import hashlib
import json
import tarfile
from pathlib import Path

import pytest

from probe import cli
from probe.connectors.harbor import stage_trial_export
from probe.connectors.harbor_export import consume_export_request, drain_export_requests
from tests.conftest import make_client


def _write_export(root: Path, run_id: str | None, *, bad_hash: bool = False) -> Path:
    trial = root / "trial"
    trial.mkdir(parents=True)
    files = {
        "config.json": b'{"task":{"name":"swe"}}',
        "lock.json": b"{}",
        "result.json": json.dumps(
            {"trial_name": root.name, "verifier_result": {"reward": 0.5}}
        ).encode(),
        ".native-state": b"opaque hidden fork state",
        "native-trajectory.bin": b"opaque fork bytes",
    }
    declarations = []
    for relative_path, data in files.items():
        (trial / relative_path).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        if bad_hash and relative_path == "result.json":
            digest = "0" * 64
        declarations.append(
            {
                "role": "other",
                "path": relative_path,
                "content_hash": digest,
                "size_bytes": len(data),
            }
        )
    (root / "capture-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "files": declarations,
                "capture": {
                    "completeness": {
                        "expected": [
                            {"path": name, "required": True, "state": "present"}
                            for name in ("config.json", "lock.json", "result.json")
                        ]
                    }
                },
            }
        )
    )
    (root / "trial.tar.gz").write_bytes(b"durable archive stays local")
    request = {
        "schema_version": "probe-harbor-export/1",
        "request_id": f"req-{root.name}",
        "status": "pending",
        "created_at": "2026-07-22T00:00:00Z",
        "attempts": 0,
        "last_error": None,
        "target": {"kind": "probe_run", "run_id": run_id},
        "connector": "probe.connectors.harbor.capture_trial",
        "arguments": {
            "trial_dir": "trial",
            "trial_dir_base": "descriptor_dir",
            "step_index": 600,
            "environment": {"type": "skypilot-fork"},
            "source_mode": "bridge-hook",
            "expand": False,
        },
        "correlation": {
            "external_key": f"miles:{root.name}:600:sample-0",
            "probe_run_id": run_id,
            "miles_run_id": "miles-job-7",
            "rollout_id": 600,
            "sample_id": "sample-0",
            "group_id": "mix-swe-70",
            "step_index": 600,
            "session_id": "session-9",
            "trial_id": root.name,
            "osmosis_mix_id": "customer-mix-a",
        },
        "capture_manifest": "capture-manifest.json",
        "archive": "trial.tar.gz",
    }
    path = root / "export-request.json"
    path.write_text(json.dumps(request))
    return path


def _write_native_trial(root: Path) -> Path:
    root.mkdir(parents=True)
    (root / "result.json").write_text(
        json.dumps(
            {
                "trial_name": "swe__sdk-owned",
                "task_name": "swe",
                "verifier_result": {
                    "rewards": {"reward": 0.75, "tests_passed": 1.0},
                },
            }
        )
    )
    (root / ".native-state").write_bytes(b"\x00opaque\xffstate")
    (root / "output").mkdir()
    (root / "output" / "patch.diff").write_text("+fixed\n")
    (root / "latest-result").symlink_to("result.json")
    return root


def test_sdk_stages_and_owns_the_export_contract(client, tmp_path):
    run = client.run(experiment="e", hypothesis="h", name="r")
    source = _write_native_trial(tmp_path / "harbor" / "trial")

    exported = stage_trial_export(
        source,
        tmp_path / "captures" / "trial-17",
        run_id=run.id,
        step_index=17,
        environment={"type": "daytona", "sandbox_id": "sandbox-9"},
        correlation={
            "miles_run_id": "miles-7",
            "rollout_id": 17,
            "sample_id": "sample-0",
            "task_id": "swe-task-42",
        },
        context={"dataset_name": "swebench", "osmosis_mix_id": "mix-a"},
        expected_paths=["result.json", "output/patch.diff"],
        expand=False,
    )

    assert exported.durable_collection_complete is True
    assert exported.root == tmp_path / "captures" / "trial-17"
    assert exported.archive_path and exported.archive_path.is_file()
    assert not list(exported.root.parent.glob(".trial-17.*"))
    descriptor = json.loads(exported.request_path.read_text())
    assert descriptor["schema_version"] == "probe-harbor-export/1"
    assert descriptor["status"] == "pending"
    assert descriptor["target"] == {"kind": "probe_run", "run_id": run.id}
    assert descriptor["arguments"]["trial_dir"] == "trial"
    assert descriptor["arguments"]["step_index"] == 17
    assert descriptor["correlation"]["context"] == {
        "dataset_name": "swebench",
        "osmosis_mix_id": "mix-a",
    }
    manifest = json.loads(exported.capture_manifest_path.read_text())
    files = {item["path"]: item for item in manifest["files"]}
    assert files[".native-state"]["size_bytes"] == len(b"\x00opaque\xffstate")
    assert len(files[".native-state"]["content_hash"]) == 64
    assert manifest["capture"]["completeness"]["status"] == "complete"
    assert manifest["capture"]["ledger"]["schema_version"] == "probe.capture/v1"
    assert manifest["trial"]["task_id"] == "swe-task-42"
    assert manifest["verifier"] == {
        "reward": 0.75,
        "rewards": {"reward": 0.75, "tests_passed": 1.0},
    }
    assert manifest["capture"]["symlinks"] == [
        {"path": "latest-result", "target": "result.json"}
    ]
    assert (exported.staged_trial.trial_dir / "latest-result").is_symlink()
    with tarfile.open(exported.archive_path) as archive:
        symlink = archive.getmember("trial/latest-result")
        assert symlink.issym()
        assert symlink.linkname == "result.json"

    result = consume_export_request(client, exported.request_path)
    assert result["status"] == "completed"
    (harbor_manifest,) = client.list_run_artifacts(run.id, kind="harbor_trial")
    assert harbor_manifest["step_index"] == 17
    assert harbor_manifest["meta"]["source"]["context"]["miles_run_id"] == "miles-7"
    assert harbor_manifest["meta"]["source"]["context"]["context"] == {
        "dataset_name": "swebench",
        "osmosis_mix_id": "mix-a",
    }


def test_sdk_export_is_idempotent_and_rejects_a_changed_context(tmp_path):
    source = _write_native_trial(tmp_path / "harbor" / "trial")
    destination = tmp_path / "captures" / "trial"
    kwargs = {
        "step_index": 9,
        "correlation": {"rollout_id": 9, "sample_id": "s0"},
        "context": {"dataset": "swe"},
    }
    first = stage_trial_export(source, destination, **kwargs)
    before = first.request_path.read_bytes()

    second = stage_trial_export(source, destination, **kwargs)
    assert second.request_path.read_bytes() == before
    assert second.descriptor["request_id"] == first.descriptor["request_id"]

    with pytest.raises(FileExistsError, match="conflicting Harbor export bundle"):
        stage_trial_export(
            source,
            destination,
            step_index=9,
            correlation={"rollout_id": 9, "sample_id": "s0"},
            context={"dataset": "different"},
        )


def test_sdk_export_can_be_bound_to_a_run_after_offline_staging(client, tmp_path):
    source = _write_native_trial(tmp_path / "harbor" / "trial")
    exported = stage_trial_export(
        source,
        tmp_path / "captures" / "offline",
        step_index=4,
        correlation={"miles_run_id": "job-offline", "rollout_id": 4},
    )
    assert exported.descriptor["target"]["run_id"] is None

    run = client.run(experiment="e", hypothesis="h", name="r")
    completed = consume_export_request(client, exported.request_path, run_id=run.id)

    assert completed["status"] == "completed"
    persisted = json.loads(exported.request_path.read_text())
    assert persisted["target"]["run_id"] == run.id
    assert persisted["correlation"]["probe_run_id"] == run.id


def test_consume_export_request_verifies_and_publishes_raw_trial(client, app, tmp_path):
    run = client.run(experiment="e", hypothesis="h", name="r")
    request = _write_export(tmp_path / "capture" / "trial-1", run.id)

    result = consume_export_request(client, request)

    assert result["status"] == "completed"
    assert result["attempts"] == 1
    assert result["result"]["capture"]["capture"]["state"] == "complete"
    assert json.loads(request.read_text())["status"] == "completed"
    assert (
        request.parent / "trial.tar.gz"
    ).read_bytes() == b"durable archive stays local"
    manifests = client.list_run_artifacts(run.id, kind="harbor_trial")
    source = manifests[0]["meta"]["source"]
    assert source["mode"] == "bridge-hook"
    assert source["context"]["miles_run_id"] == "miles-job-7"
    assert source["context"]["osmosis_mix_id"] == "customer-mix-a"
    file_entries = {item["path"]: item for item in manifests[0]["meta"]["files"]}
    assert file_entries[".native-state"]["uploaded"] is True
    evidence = client.list_run_artifacts(run.id, kind="harbor_capture_manifest")
    assert len(evidence) == 1
    assert (
        result["result"]["producer_capture_manifest_artifact_id"] == evidence[0]["id"]
    )

    request_count = len(app.requests)
    again = consume_export_request(client, request)
    assert again["status"] == "completed"
    assert (
        len(app.requests) == request_count
    )  # completed descriptors are idempotent no-ops


def test_bad_declared_hash_fails_before_upload_and_preserves_bundle(
    client, app, tmp_path
):
    run = client.run(experiment="e", hypothesis="h", name="r")
    request = _write_export(tmp_path / "capture" / "trial-bad", run.id, bad_hash=True)

    with pytest.raises(Exception, match="durable trial collection is partial"):
        consume_export_request(client, request)

    descriptor = json.loads(request.read_text())
    assert descriptor["status"] == "failed"
    assert descriptor["attempts"] == 1
    assert "hash mismatch" in descriptor["last_error"]
    assert (request.parent / "trial" / "result.json").is_file()
    assert (request.parent / "trial.tar.gz").is_file()
    assert not client.list_run_artifacts(run.id, kind="harbor_trial")


def test_drain_continues_after_one_failed_request(client, app, tmp_path):
    run = client.run(experiment="e", hypothesis="h", name="r")
    _write_export(tmp_path / "good", run.id)
    _write_export(tmp_path / "bad", run.id, bad_hash=True)

    result = drain_export_requests(client, tmp_path)

    assert result["counts"] == {"completed": 1, "failed": 1, "skipped": 0}
    assert result["failed"][0]["path"].endswith("bad/export-request.json")


def test_later_resolved_run_id_repairs_and_persists_offline_descriptor(
    client, tmp_path
):
    run = client.run(experiment="e", hypothesis="h", name="r")
    request = _write_export(tmp_path / "offline", None)

    result = consume_export_request(client, request, run_id=run.id)

    assert result["status"] == "completed"
    persisted = json.loads(request.read_text())
    assert persisted["target"]["run_id"] == run.id
    assert persisted["correlation"]["probe_run_id"] == run.id


def test_later_resolved_run_id_cannot_override_existing_target(client, tmp_path):
    first = client.run(experiment="e", hypothesis="h", name="first")
    second = client.run(experiment="e", hypothesis="h", name="second")
    request = _write_export(tmp_path / "mismatch", first.id)

    with pytest.raises(Exception, match="disagrees with descriptor run"):
        consume_export_request(client, request, run_id=second.id)


def test_cli_export_is_a_non_python_consumer(app, tmp_path, monkeypatch, capsys):
    client = make_client(app, tmp_spool=tmp_path / "spool")
    run = client.run(experiment="e", hypothesis="h", name="r")
    request = _write_export(tmp_path / "capture" / "trial-cli", run.id)

    monkeypatch.setattr(
        cli,
        "Client",
        lambda **_kwargs: make_client(app, tmp_spool=tmp_path / "cli-spool"),
    )
    assert cli.main(["trial", "export", str(request)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "completed"
    assert output["result"]["run_id"] == run.id


def test_cli_watch_once_drains_new_requests(app, tmp_path, monkeypatch, capsys):
    client = make_client(app, tmp_spool=tmp_path / "spool")
    run = client.run(experiment="e", hypothesis="h", name="r")
    request = _write_export(tmp_path / "captures" / "trial-watch", None)

    monkeypatch.setattr(
        cli,
        "Client",
        lambda **_kwargs: make_client(app, tmp_spool=tmp_path / "watch-spool"),
    )
    assert (
        cli.main(
            ["trial", "watch", str(tmp_path / "captures"), "--once", "--run", run.id]
        )
        == 0
    )

    output = json.loads(capsys.readouterr().out)
    assert output["counts"] == {"completed": 1, "failed": 0, "skipped": 0}
    assert json.loads(request.read_text())["status"] == "completed"
