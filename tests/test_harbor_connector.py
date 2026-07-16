"""Harbor trial capture (connectors.harbor) — Phase 1 of the ownership plan."""

from __future__ import annotations

import json

import pytest

from probe.cli import main as _cli_main  # noqa: F401 - ensures CLI imports cleanly
from probe import cli
from probe.connectors.harbor import MANIFEST_KIND, capture_trial, parse_trial, role_for
from tests.conftest import make_client


# -- fixture: an Osmosis-shaped trial directory --------------------------------
def _write_trial(root, *, with_result: bool = True):
    root.mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"task": {"name": "swe-fix"}}))
    if with_result:
        (root / "result.json").write_text(json.dumps({
            "trial_name": "swe-fix__bwrhe3y",
            "task_name": "swe-fix",
            "task_checksum": "sha256:feed",
            "agent_info": {"name": "miles-agent", "version": "0.3",
                           "model_info": {"name": "qwen3", "provider": "sglang"}},
            "verifier_result": {"reward": 0.75},
            "started_at": "2026-07-15T01:00:00Z",
            "finished_at": "2026-07-15T01:05:00Z",
            "agent_execution": {"started_at": "2026-07-15T01:01:00Z",
                                "finished_at": "2026-07-15T01:04:00Z"},
        }))
    (root / "trajectory.json").write_text(json.dumps({"schema": "atif@1", "steps": []}))
    (root / "logs" / "agent" / "command-0").mkdir(parents=True)
    (root / "logs" / "agent" / "command-0" / "stdout.txt").write_text("ran the thing\n")
    (root / "logs" / "verifier").mkdir()
    (root / "logs" / "verifier" / "test-console-output.txt").write_text("3 passed\n")
    (root / "output").mkdir()
    (root / "output" / "report.pdf").write_bytes(b"%PDF-fake")
    (root / "fork-specific.bin").write_bytes(b"\x00private fork artifact")
    return root


# -- parsing --------------------------------------------------------------------
def test_parse_trial_reads_the_contract(tmp_path):
    trial = parse_trial(_write_trial(tmp_path / "t"))
    assert trial.name == "swe-fix__bwrhe3y"
    assert trial.task_name == "swe-fix"
    assert trial.reward == 0.75
    assert trial.trajectory_format == "atif@1"
    assert trial.phases["agent_execution"]["started_at"] == "2026-07-15T01:01:00Z"
    assert len(trial.files) == 7


def test_parse_trial_tolerates_a_bare_fork_dir(tmp_path):
    root = tmp_path / "forked"
    root.mkdir()
    (root / "whatever.log").write_text("no contract at all")
    trial = parse_trial(root)
    assert trial.name == "forked"  # falls back to the directory name
    assert trial.reward is None and trial.result is None
    assert [f.name for f in trial.files] == ["whatever.log"]


def test_parse_trial_reward_json_beats_result(tmp_path):
    root = _write_trial(tmp_path / "t")
    (root / "reward.json").write_text(json.dumps({"reward": 0.9}))
    assert parse_trial(root).reward == 0.9


def test_parse_trial_missing_dir_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        parse_trial(tmp_path / "absent")


def test_role_mapping_is_fork_tolerant():
    assert role_for("result.json") == "result"
    assert role_for("logs/agent/command-0/stdout.txt") == "agent_log"
    assert role_for("logs/verifier/ctrf.json") == "verifier"
    assert role_for("verifier/reward.txt") == "verifier"
    assert role_for("output/report.pdf") == "output"
    assert role_for("fork-specific.bin") == "other"
    assert role_for("nested/config.json") == "other"  # only top-level contract files


# -- capture ---------------------------------------------------------------------
def test_capture_trial_full(client, app, tmp_path):
    client.fail_open = False
    run = client.run(experiment="e", hypothesis="h", name="r")
    result = capture_trial(
        run, _write_trial(tmp_path / "t"), step_index=600,
        environment={"type": "skypilot-fork"}, strict=True,
    )
    # rollout span, reward at step 600
    assert app.spans_upserted == 1
    assert app.metrics_inserted == 1
    metric_body = json.loads(
        next(r for r in app.requests if r.url.path.endswith("/metrics")).content
    )
    assert metric_body["points"][0] == {
        "key": "reward", "kind": "model", "value": 0.75,
        "step_index": 600, "dimensions": {},
    }
    # every file uploaded, labeled, step-keyed
    assert len(result["files"]) == 7
    assert all(f["uploaded"] for f in result["files"])
    assert {f["role"] for f in result["files"]} == {
        "config", "result", "trajectory", "agent_log", "verifier", "output", "other",
    }
    # the manifest is queryable by the Phase-0 filters
    manifests = client.list_run_artifacts(run.id, kind=MANIFEST_KIND, step_from=600, step_to=600)
    assert len(manifests) == 1
    meta = manifests[0]["meta"]
    assert meta["schema_version"] == "1.0"
    assert meta["trial"]["name"] == "swe-fix__bwrhe3y"
    assert meta["verifier"] == {"reward": 0.75}
    assert meta["environment"] == {"type": "skypilot-fork"}
    assert meta["source"] == {"mode": "local", "rollout_id": 600}
    assert all(entry["artifact_id"] for entry in meta["files"])


def test_capture_trial_bare_fork_dir_still_captures(client, app, tmp_path):
    """A private fork with zero contract files is captured, not rejected."""
    client.fail_open = False
    run = client.run(experiment="e", hypothesis="h", name="r")
    root = tmp_path / "forked"
    root.mkdir()
    (root / "whatever.log").write_text("bytes")
    result = capture_trial(run, root, step_index=601, strict=True)
    assert app.metrics_inserted == 0  # no reward -> no metric
    assert result["reward"] is None
    manifests = client.list_run_artifacts(run.id, kind=MANIFEST_KIND)
    assert manifests[0]["meta"]["files"][0]["role"] == "other"
    assert manifests[0]["meta"]["trial"]["name"] == "forked"


def test_capture_trial_failed_trial_marks_span(client, app, tmp_path):
    client.fail_open = False
    run = client.run(experiment="e", hypothesis="h", name="r")
    root = tmp_path / "t"
    root.mkdir()
    (root / "result.json").write_text(json.dumps({
        "trial_name": "boom__1",
        "exception_info": {"exception_type": "OOM", "exception_message": "cuda oom"},
    }))
    capture_trial(run, root, step_index=7, strict=True)
    span_body = json.loads(
        next(r for r in app.requests if r.url.path.endswith("/spans")).content
    )
    assert span_body["spans"][0]["status"] == "failed"
    manifests = client.list_run_artifacts(run.id, kind=MANIFEST_KIND)
    assert manifests[0]["meta"]["exception"]["exception_type"] == "OOM"


def test_capture_trial_fail_open_marks_unuploaded(client, app, tmp_path):
    """Storage down mid-capture: the loop is not blocked, the manifest is honest."""
    run = client.run(experiment="e", hypothesis="h", name="r")
    root = tmp_path / "t"
    root.mkdir()
    (root / "only.txt").write_text("x")
    app.fail_next_uploads = True
    with pytest.warns(UserWarning, match="recorded as a reference"):
        result = capture_trial(run, root, step_index=5)
    (entry,) = result["files"]
    assert entry["uploaded"] is False  # fell back to a labeled reference


# -- CLI ---------------------------------------------------------------------------
@pytest.fixture
def wired(app, tmp_path, monkeypatch):
    def factory(**_kw):
        return make_client(app, tmp_spool=tmp_path / "spool")

    monkeypatch.setattr(cli, "Client", factory)
    return app


def test_cli_trial_add(wired, capsys, tmp_path):
    cli.main(["run", "start", "--experiment", "e", "--hypothesis", "h", "--name", "r"])
    run_id = capsys.readouterr().out.strip()
    trial = _write_trial(tmp_path / "t")
    rc = cli.main(["trial", "add", run_id, str(trial), "--step", "600", "--env-type", "skypilot-fork"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["trial"] == "swe-fix__bwrhe3y"
    assert out["reward"] == 0.75
    assert out["files"] == 7
    assert out["uploaded"] == 7
    assert out["manifest_artifact_id"]
