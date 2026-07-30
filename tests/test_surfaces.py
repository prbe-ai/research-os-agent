"""Boundaries between experiment upload, hook adapters, and asset operations."""

from __future__ import annotations

import sys
from tests.conftest import open_run




def test_research_note_is_normal_experiment_upload(client, app):
    run = open_run(client, experiment="e", name="r")
    result = client.notes.add(
        run.id,
        "decision",
        "Use the official scorer",
        evidence_refs=["tool:91"],
        confidence=0.9,
    )
    assert result["kind"] == "note"
    note = app.artifacts[run.id][0]["meta"]
    assert note["kind"] == "decision"
    assert note["evidence_refs"] == ["tool:91"]


def test_events_read_surface_is_read_only(client, app):
    run = open_run(client, experiment="e", name="r")
    # client.events is the read surface (backend lifecycle log); no write method.
    assert not hasattr(client.events, "add")
    assert client.events.for_run(run.id) == []


def test_experiment_version_replaces_run_promote(client, app):
    client.fail_open = False
    project = client.create_project("dockq-project")
    exp = client.create_experiment(
        "dockq", "DockQ", hypothesis="h", project_id=project["id"]
    )
    version = client.experiment_version(exp["id"], label="launch")
    assert version["version"] == 1
    # run-level promote is gone (promotion_tier rejected upstream)
    assert not hasattr(client, "promote")


def test_execute_propagates_run_id(client, app, tmp_path):
    run = open_run(client, experiment="e", name="r")
    output = tmp_path / "run-id.txt"
    result = run.execute(
        [
            sys.executable,
            "-c",
            f"import os, pathlib; pathlib.Path({str(output)!r}).write_text(os.environ['PROBE_RUN_ID'])",
        ]
    )
    assert result.returncode == 0
    assert output.read_text() == run.id
    assert app.spans_upserted == 2


def test_run_check_flags_failed_upload_not_intentional_reference(client, app):
    run = open_run(client, experiment="e", name="r")
    # env_ref (execution record) present -> launch capture is satisfied.
    run._data["metadata"] = {"env_ref": "sha256:abc"}
    app.runs[run.id]["metadata"] = run._data["metadata"]
    run.log_artifact("code-snapshot", uri="git:refs/probe/snapshots/x#abc", kind="code_snapshot")
    # An INTENTIONAL path reference names bytes that exist off-platform (a shared-volume
    # checkpoint the agent resolves locally) -> NOT a capture gap.
    run.log_artifact("ckpt.pt", path="/mnt/shared/ckpt.pt", reference=True, allow_missing=True)
    report = client.check_run(run.id)
    assert report["state"] == "complete"
    assert "portable_artifact_bytes" not in report["missing"]
    assert "execution_record" not in report["missing"]
    assert "promotion_manifest_available" not in report
    # A reference recorded because a managed upload FAILED (fail-open) IS a gap: its
    # bytes never reached R2. Distinguished by meta.upload, not uri presence.
    run.log_artifact(
        "dropped.pt", kind="file", is_reference=True,
        meta={"upload": "failed", "local_path": "/tmp/dropped.pt"},
    )
    failed = client.check_run(run.id)
    assert failed["state"] == "incomplete"
    assert "portable_artifact_bytes" in failed["missing"]
