"""Run.snapshot() writes the launch block and lockfile identity."""
from __future__ import annotations

from tests.conftest import open_run


def test_snapshot_writes_launch_block(client, tmp_path):
    (tmp_path / "train.py").write_text("print('hi')\n")
    (tmp_path / "uv.lock").write_text("[lock]\n")
    run = open_run(client, experiment="exp-launch")
    run.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False,
                 argv=["python", "train.py", "--seed", "9"])
    row = client.run_bundle(run.id)["run"]
    launch = (row.get("metadata") or {}).get("launch")
    assert launch["schema"] == "probe.launch/1"
    assert launch["process"]["argv"] == ["python", "train.py", "--seed", "9"]
    seeds = {s["name"]: s for s in launch["determinism"]["seeds"]}
    assert seeds["seed"]["value"] == "9"
    assert row.get("env_ref")


def test_snapshot_merges_metadata_not_clobbers(client, tmp_path):
    (tmp_path / "a.py").write_text("a\n")
    run = open_run(client, experiment="exp-meta", metadata={"owner": "mahit"})
    run.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False)
    row = client.run_bundle(run.id)["run"]
    assert row["metadata"]["owner"] == "mahit"
    assert "launch" in row["metadata"]


def test_snapshot_deps_include_lockfile_hashes(client, tmp_path):
    (tmp_path / "uv.lock").write_text("[lock]\nversion = 1\n")
    run = open_run(client, experiment="exp-lock")
    snap = run.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False)
    locks = snap["deps"].get("lockfiles")
    assert locks and locks[0]["path"] == "uv.lock" and locks[0]["sha256"]
