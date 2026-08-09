"""The reproduce PULL surface: SDK client + MCP source passthroughs over the
research-os `/reproduce` endpoints.

The client is deliberately thin here — the server (research-os
`app/read_models/reproduce.py`) is the one place that assembles a run's execution
record, launch context, code snapshot, inputs, lockfiles, lineage and span envs
into one read. These tests pin the passthrough and the two contracts that matter
to a coworker: a legacy run (no capture) reproduces with an honest `incomplete`
verdict rather than an error, and `?version=N` reaches the backend.
"""

from __future__ import annotations

from probe.mcp.source import ResearchOSSource


def _seed_run(client, app):
    """A minimal run with no capture — the legacy shape the endpoint must degrade on."""
    project = client.create_project("folding")
    client.create_experiment("p", "p", hypothesis="h", project_id=project["id"])
    run = client.run(project="folding", experiment="p", name="r1")
    return run.id, app.runs[run.id]["experiment_id"]


def test_run_reproduce_returns_server_record(client, app):
    rid, _ = _seed_run(client, app)
    rec = client.run_reproduce(rid)
    assert rec["run"]["id"] == rid
    assert rec["restore_command"].startswith("probe snapshot-restore")
    assert rec["completeness"]["state"] in {"incomplete", "unverified"}


def test_run_reproduce_legacy_run_is_incomplete_not_error(client, app):
    rid, _ = _seed_run(client, app)  # no env_ref, no launch
    rec = client.run_reproduce(rid)
    assert rec["completeness"]["state"] == "incomplete"
    assert "execution_record" in rec["completeness"]["missing"]
    assert "launch_context" in rec["completeness"]["advisories"]


def test_experiment_reproduce_lists_run_summaries(client, app):
    rid, eid = _seed_run(client, app)
    rec = client.experiment_reproduce(eid)
    assert rec["completeness"]["runs_total"] == 1
    assert rec["experiment"]["id"] == eid
    assert rec["runs"][0]["reproduce_url"] == f"/v1/runs/{rid}/reproduce"


def test_experiment_reproduce_forwards_version(client, app):
    _, eid = _seed_run(client, app)
    rec = client.experiment_reproduce(eid, version=2)
    assert rec["resolved_version"] == 2


def test_source_reproduce_passthrough(client, app):
    rid, eid = _seed_run(client, app)
    src = ResearchOSSource(client)
    assert src.reproduce(rid)["run"]["id"] == rid
    assert src.experiment_reproduce(eid)["completeness"]["runs_total"] == 1
    assert src.experiment_reproduce(eid, version=1)["resolved_version"] == 1
