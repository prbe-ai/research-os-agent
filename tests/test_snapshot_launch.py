"""Run.snapshot() writes the launch block and lockfile identity."""
from __future__ import annotations

import sys

import pytest

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


def test_same_tree_same_execution_record(client, tmp_path):
    """Identity is the code MANIFEST (design doc D1); git provenance -- a
    per-snapshot commit sha + run-id ref -- must NOT be hashed into the
    execution record, or two runs over an identical (non-git) tree would
    mint distinct execution records and never dedupe."""
    (tmp_path / "a.py").write_text("a\n")
    run1 = open_run(client, experiment="exp-dedupe-1")
    run2 = open_run(client, experiment="exp-dedupe-2")
    h1 = run1.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False)["content_hash"]
    h2 = run2.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False)["content_hash"]
    assert h1 == h2


def test_execute_auto_snapshots(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_AUTO_SNAPSHOT", "1")
    (tmp_path / "job.py").write_text("print('ok')\n")
    run = open_run(client, experiment="exp-exec")
    run.execute([sys.executable, "job.py"], cwd=str(tmp_path))
    row = client.run_bundle(run.id)["run"]
    launch = (row.get("metadata") or {}).get("launch")
    assert launch is not None
    assert launch["process"]["argv"] == [sys.executable, "job.py"]


def test_execute_auto_snapshot_opt_out(client, tmp_path):
    run = open_run(client, experiment="exp-exec-off")
    run.execute([sys.executable, "-c", "pass"], cwd=str(tmp_path))
    row = client.run_bundle(run.id)["run"]
    assert "launch" not in (row.get("metadata") or {})


def test_execute_survives_snapshot_failure(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_AUTO_SNAPSHOT", "1")
    run = open_run(client, experiment="exp-boom")
    monkeypatch.setattr(
        type(run), "snapshot",
        lambda self, **kw: (_ for _ in ()).throw(RuntimeError("capture exploded")),
    )
    with pytest.warns(UserWarning, match="auto-snapshot failed"):
        result = run.execute([sys.executable, "-c", "pass"], cwd=str(tmp_path))
    assert result.returncode == 0  # block claims, never runs


def test_client_run_auto_snapshots(client, monkeypatch, tmp_path):
    # chdir into a plain tmp dir: the open-time snapshot must not depend on the
    # HOST checkout (CI runners have no git identity, so shadow-committing the
    # repo itself fails there and the launch block silently never lands).
    (tmp_path / "a.py").write_text("a\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PROBE_AUTO_SNAPSHOT", "1")
    run = client.run(
        project="proj-auto",
        experiment="exp-auto-open",
        hypothesis="auto-snapshot opens",
    )
    row = client.run_bundle(run.id)["run"]
    assert "launch" in (row.get("metadata") or {})


def test_client_run_snapshot_param_overrides(client, monkeypatch):
    monkeypatch.setenv("PROBE_AUTO_SNAPSHOT", "1")
    run = client.run(
        project="proj-auto2",
        experiment="exp-no-snap",
        hypothesis="param wins",
        snapshot=False,
    )
    row = client.run_bundle(run.id)["run"]
    assert "launch" not in (row.get("metadata") or {})


def test_execute_span_attributes_are_scrubbed(client, tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_AUTO_SNAPSHOT", "1")
    run = open_run(client, experiment="exp-span-scrub")
    run.execute([sys.executable, "-c", "pass", "--api-key", "sk-live12345678"], cwd=str(tmp_path))
    spans = client.transport.get(f"/v1/runs/{run.id}/spans")
    process_spans = [s for s in spans if s.get("span_type") == "process"]
    assert process_spans
    for span in process_spans:
        assert "sk-live12345678" not in str(span.get("attributes"))
