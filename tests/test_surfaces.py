"""Boundaries between experiment upload, hook adapters, and asset operations."""

from __future__ import annotations

import sys

import pytest

from probe.sdk.client import Anchor
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


def test_a_note_lands_on_a_project_before_any_run_exists(client, app):
    """The gap this closes: planning, investigation and architecture decisions all
    happen BEFORE the first run, and a note that required one had nowhere to go.

    Asserted through the round trip, not the request: the note IS its `meta`, so a
    write that reached the right route with the payload dropped would look identical
    from the call site.
    """
    project = client.create_project("planning")

    result = client.notes.add(
        project["id"],
        "decision",
        "GKE, not DOKS",
        anchor=Anchor.PROJECT,
        evidence_refs=["docs/findings.md#5"],
    )

    assert result["kind"] == "note"
    assert result["project_id"] == project["id"]
    [note] = client.notes.list(project["id"], anchor=Anchor.PROJECT)
    assert note["statement"] == "GKE, not DOKS"
    assert note["evidence_refs"] == ["docs/findings.md#5"]
    assert note["authority"] == "agent_summarized"


def test_a_note_refuses_a_file_anchor_by_name(client):
    """Workspace and Shared carry no `meta`, so the note would arrive stripped. The
    server cannot say that usefully -- it sees a field it does not declare -- so the
    refusal names the anchor and lists the ones that work."""
    with pytest.raises(ValueError, match="file anchor"):
        client.notes.add("x", "decision", "s", anchor=Anchor.WORKSPACE)
    with pytest.raises(ValueError, match="unknown anchor"):
        client.notes.add("x", "decision", "s", anchor="sideways")


def test_a_superseded_decision_reads_as_superseded_not_as_a_contradiction(client):
    """`--supersedes` was stored and read by NOTHING, so a reversed decision came
    back beside the one that replaced it and the record contradicted itself."""
    project = client.create_project("reversal")
    first = client.notes.add(
        project["id"], "decision", "DOKS for the data plane", anchor=Anchor.PROJECT
    )
    client.notes.add(
        project["id"],
        "decision",
        "GKE — Harbor has no generic-K8s backend",
        anchor=Anchor.PROJECT,
        supersedes=first["meta"]["note_id"],
    )

    [current] = client.notes.list(project["id"], anchor=Anchor.PROJECT)
    assert current["statement"].startswith("GKE")
    assert "superseded_by" not in current

    both = client.notes.list(
        project["id"], anchor=Anchor.PROJECT, include_superseded=True
    )
    assert [n["statement"] for n in both] == [
        "DOKS for the data plane",
        "GKE — Harbor has no generic-K8s backend",
    ]
    assert both[0]["superseded_by"] == both[1]["note_id"]


def test_note_list_skips_artifacts_that_are_not_notes(client, app):
    """A `kind="note"` artifact without the note encoding is somebody else's row.
    Skipping it is the point: inventing a `statement` for it would put a fabricated
    claim in a decision journal."""
    project = client.create_project("mixed")
    client.notes.add(project["id"], "observation", "real", anchor=Anchor.PROJECT)
    app.artifacts[f"project:{project['id']}"].append(
        {"id": "x", "name": "stray", "kind": "note", "meta": {"free": "form"}}
    )
    app.artifacts[f"project:{project['id']}"].append(
        {"id": "y", "name": "ckpt", "kind": "file", "meta": {}}
    )

    notes = client.notes.list(project["id"], anchor=Anchor.PROJECT)
    assert [n["statement"] for n in notes] == ["real"]


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
    # `unverified`, not `complete`: nothing is absent, but nothing here proved the
    # recorded commit is retrievable either. `complete` is earned by verify=True.
    assert report["state"] == "unverified"
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
