"""Boundaries between experiment upload, hook adapters, and asset operations."""

from __future__ import annotations

import sys

import pytest

from probe import errors


def test_research_note_is_normal_experiment_upload(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
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
    run = client.run(experiment="e", hypothesis="h", name="r")
    # client.events is the read surface (backend lifecycle log); no write method.
    assert not hasattr(client.events, "add")
    assert client.events.for_run(run.id) == []


def test_hook_session_surface_attaches_checkpoints_and_detaches(client, app, tmp_path):
    run = client.run(experiment="e", hypothesis="h", name="r")
    transcript = tmp_path / "session.jsonl"
    transcript.write_text('authorization: Bearer ros_pat_secretvalue\n{"message":"ok"}\n')

    client.sessions.attach(
        run.id,
        "session-1",
        transcript_path=str(transcript),
        cwd=str(tmp_path),
        strict=True,
    )
    checkpoint = client.sessions.checkpoint(
        run.id,
        "session-1",
        transcript_path=str(transcript),
        reason="pre_compact",
        strict=True,
    )
    client.sessions.detach(run.id, "session-1", strict=True)

    session = app.runs[run.id]["metadata"]["agent"]["sessions"][0]
    assert session["state"] == "detached"
    assert session["transcript_available"] is True
    assert "transcript_path" not in session
    assert checkpoint["portable"] is False
    artifact = next(a for a in app.artifacts[run.id] if a["kind"] == "transcript_segment")
    assert artifact["meta"]["redacted"] is True
    assert artifact["meta"]["portable"] is False


def test_asset_registry_registers_and_versions(client, app):
    client.fail_open = False
    asset = client.assets.register("dockq-scorer", kind="script")
    version = client.assets.add_version(asset["id"], content_hash="c" * 64, label="v1")
    assert version["asset_id"] == asset["id"]
    assert version["version"] == 1
    # the aspirational fork/propose/promote-candidate surface was dropped (registry only);
    # materialize (download a pinned version) is supported in Phase 2.
    assert not any(hasattr(client.assets, m) for m in ("fork", "propose"))
    assert hasattr(client.assets, "materialize")


def test_experiment_version_replaces_run_promote(client, app):
    client.fail_open = False
    exp = client.ensure_experiment("dockq", "DockQ", "h")
    version = client.experiment_version(exp["id"], label="launch")
    assert version["version"] == 1
    # run-level promote is gone (promotion_tier rejected upstream)
    assert not hasattr(client, "promote")


def test_execute_propagates_run_id(client, app, tmp_path):
    run = client.run(experiment="e", hypothesis="h", name="r")
    output = tmp_path / "run-id.txt"
    result = run.execute(
        [
            sys.executable,
            "-c",
            f"import os, pathlib; pathlib.Path({str(output)!r}).write_text(os.environ['ROS_RUN_ID'])",
        ]
    )
    assert result.returncode == 0
    assert output.read_text() == run.id
    assert app.spans_upserted == 2


def test_run_check_distinguishes_local_reference_from_portable(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    # env_ref (execution record) present -> launch capture is satisfied.
    run._data["metadata"] = {"env_ref": "sha256:abc"}
    app.runs[run.id]["metadata"] = run._data["metadata"]
    run.log_artifact("code-snapshot", uri="git:refs/probe/snapshots/x#abc", kind="code_snapshot")
    run.log_artifact("local-output", kind="file", is_reference=True)
    report = client.check_run(run.id)
    assert report["state"] == "incomplete"
    assert "portable_artifact_bytes" in report["missing"]
    assert "execution_record" not in report["missing"]
    assert "promotion_manifest_available" not in report
