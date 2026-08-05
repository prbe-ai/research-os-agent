"""check_run: launch-slot verdicts and advisories."""
from __future__ import annotations

from tests.conftest import open_run


def _snap(run, tmp_path):
    (tmp_path / "a.py").write_text("a\n")
    run.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False)


def test_legacy_run_without_launch_is_advisory_not_incomplete(client, tmp_path):
    run = open_run(client, experiment="exp-legacy")
    _snap(run, tmp_path)
    # Simulate a pre-capture-core row: strip the launch block the snapshot wrote.
    client.transport.patch(f"/v1/runs/{run.id}", {"metadata": {}})
    result = client.check_run(run.id)
    assert "launch_context" in result["advisories"]
    assert not any(m.startswith("launch_") for m in result["missing"])
    assert result["state"] == "unverified"


def test_partial_launch_block_is_incomplete(client, tmp_path):
    run = open_run(client, experiment="exp-partial")
    _snap(run, tmp_path)
    row = client.run_bundle(run.id)["run"]
    broken = dict(row["metadata"])
    broken["launch"] = {k: v for k, v in broken["launch"].items() if k != "determinism"}
    client.transport.patch(f"/v1/runs/{run.id}", {"metadata": broken})
    result = client.check_run(run.id)
    assert "launch_determinism" in result["missing"]
    assert result["state"] == "incomplete"


def test_complete_launch_block_stays_unverified(client, tmp_path):
    run = open_run(client, experiment="exp-ok")
    _snap(run, tmp_path)
    result = client.check_run(run.id)
    assert not any(m.startswith("launch_") for m in result["missing"])
    assert result["state"] == "unverified"


def test_notes_and_inputs_decision_are_advisories(client, tmp_path):
    run = open_run(client, experiment="exp-adv")
    _snap(run, tmp_path)
    result = client.check_run(run.id)
    assert "inputs_decision" in result["advisories"]
    assert "notes" in result["advisories"]
    assert "inputs_decision" not in result["missing"]
