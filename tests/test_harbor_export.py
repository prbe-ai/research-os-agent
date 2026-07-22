"""Retryable Miles -> Harbor connector export requests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from probe import cli
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


def test_consume_export_request_verifies_and_publishes_raw_trial(client, app, tmp_path):
    run = client.run(experiment="e", hypothesis="h", name="r")
    request = _write_export(tmp_path / "capture" / "trial-1", run.id)

    result = consume_export_request(client, request)

    assert result["status"] == "completed"
    assert result["attempts"] == 1
    assert result["result"]["capture"]["capture"]["state"] == "complete"
    assert json.loads(request.read_text())["status"] == "completed"
    assert (request.parent / "trial.tar.gz").read_bytes() == b"durable archive stays local"
    manifests = client.list_run_artifacts(run.id, kind="harbor_trial")
    source = manifests[0]["meta"]["source"]
    assert source["mode"] == "bridge-hook"
    assert source["context"]["miles_run_id"] == "miles-job-7"
    assert source["context"]["osmosis_mix_id"] == "customer-mix-a"
    file_entries = {item["path"]: item for item in manifests[0]["meta"]["files"]}
    assert file_entries[".native-state"]["uploaded"] is True
    evidence = client.list_run_artifacts(run.id, kind="harbor_capture_manifest")
    assert len(evidence) == 1
    assert result["result"]["producer_capture_manifest_artifact_id"] == evidence[0]["id"]

    request_count = len(app.requests)
    again = consume_export_request(client, request)
    assert again["status"] == "completed"
    assert len(app.requests) == request_count  # completed descriptors are idempotent no-ops


def test_bad_declared_hash_fails_before_upload_and_preserves_bundle(client, app, tmp_path):
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


def test_later_resolved_run_id_repairs_and_persists_offline_descriptor(client, tmp_path):
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
        cli, "Client", lambda **_kwargs: make_client(app, tmp_spool=tmp_path / "cli-spool")
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
        cli, "Client", lambda **_kwargs: make_client(app, tmp_spool=tmp_path / "watch-spool")
    )
    assert cli.main(
        ["trial", "watch", str(tmp_path / "captures"), "--once", "--run", run.id]
    ) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["counts"] == {"completed": 1, "failed": 0, "skipped": 0}
    assert json.loads(request.read_text())["status"] == "completed"
