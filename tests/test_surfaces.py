"""Boundaries between experiment upload, hook adapters, and asset operations."""

from __future__ import annotations

import sys

import pytest

from ros import errors


def test_research_event_is_normal_experiment_upload(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r")
    result = client.events.add(
        run.id,
        "decision",
        "Use the official scorer",
        evidence_refs=["tool:91"],
        confidence=0.9,
    )
    assert result["kind"] == "research_event"
    event = app.artifacts[run.id][0]["meta"]
    assert event["kind"] == "decision"
    assert event["evidence_refs"] == ["tool:91"]


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


def test_asset_surface_reports_missing_backend_capability(client, app, tmp_path):
    source = tmp_path / "score.py"
    source.write_text("print('score')\n")
    with pytest.raises(errors.CapabilityUnavailable) as exc:
        client.assets.propose(
            str(source),
            run_id="run-1",
            kind="script",
            canonical_name="dockq-scorer",
            new_identity_reason="no compatible scorer exists",
        )
    assert exc.value.capability == "asset_registry"
    assert not client.spool.pending(), "unsupported registry calls must not be spooled forever"


def test_promotion_surface_requires_real_manifest_backend(client):
    with pytest.raises(errors.CapabilityUnavailable) as exc:
        client.promote("run-1", approval="Approved by researcher")
    assert exc.value.capability == "promotion_manifests"


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
    run._data["metadata"] = {"snapshot": {"git": {"commit": "abc"}}}
    app.runs[run.id]["metadata"] = run._data["metadata"]
    run.log_artifact("code-snapshot", uri="git:refs/ros/snapshots/x#abc", kind="code_snapshot")
    run.log_artifact("local-output", kind="file", is_reference=True)
    report = client.check_run(run.id)
    assert report["state"] == "incomplete"
    assert "portable_artifact_bytes" in report["missing"]
    assert report["promotion_manifest_available"] is False
