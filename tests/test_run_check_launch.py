"""check_run: launch-slot verdicts and advisories."""
from __future__ import annotations

import pytest

from tests.conftest import make_client, open_run


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


# -- completion warning on finish() (never a gate) ---------------------------


def test_completed_finish_warns_when_capture_incomplete(client, monkeypatch):
    # snapshot=False on open: capture is on for finish()'s check, but this run
    # itself was never snapshotted -- otherwise client.run()'s own auto-snapshot
    # hook would capture it before finish() ever gets a say.
    run = open_run(client, experiment="exp-warn", snapshot=False)
    monkeypatch.setenv("PROBE_AUTO_SNAPSHOT", "1")
    with pytest.warns(UserWarning, match="may not be reproducible"):
        run.finish("completed")


def test_completed_finish_silent_when_captured(client, tmp_path, monkeypatch, recwarn):
    monkeypatch.setenv("PROBE_AUTO_SNAPSHOT", "1")
    run = open_run(client, experiment="exp-warn-ok")
    _snap(run, tmp_path)
    run.finish("completed")
    assert not [w for w in recwarn if "reproducible" in str(w.message)]


def test_failed_finish_never_warns(client, monkeypatch, recwarn):
    monkeypatch.setenv("PROBE_AUTO_SNAPSHOT", "1")
    run = open_run(client, experiment="exp-warn-fail")
    run.finish("failed")
    assert not [w for w in recwarn if "reproducible" in str(w.message)]


def test_opted_out_capture_never_warns(client, recwarn):
    # suite default PROBE_AUTO_SNAPSHOT=0: capture declined -> no nagging
    run = open_run(client, experiment="exp-warn-optout")
    run.finish("completed")
    assert not [w for w in recwarn if "reproducible" in str(w.message)]


def test_warning_waits_for_async_drain(app, tmp_path, monkeypatch, recwarn):
    """I1: check_run reads the run BUNDLE (a plain GET), never the journal.

    Under async_writes, snapshot()'s env_ref PATCH and code-snapshot artifact
    POST are journaled, not delivered -- so a warning probe that fired before
    finish() flushes would read a run with neither and warn on data that is
    durable on disk and about to land. The fix moves the check to after the
    drain; this pins that a fully-captured run stays silent even though every
    one of its writes went through the async path.
    """
    client = make_client(app, tmp_spool=tmp_path / "spool", async_writes=True)
    run = open_run(client, experiment="exp-warn-async")
    monkeypatch.setenv("PROBE_AUTO_SNAPSHOT", "1")
    (tmp_path / "a.py").write_text("a\n")
    run.snapshot(cwd=str(tmp_path), include_env=False, include_gpu=False)
    # Prove the writes really are only journaled, not yet server-visible --
    # otherwise this test would pass for the wrong reason.
    assert client.journal.pending(), "snapshot's writes must still be queued"
    bundle_before = client.run_bundle(run.id)
    assert not bundle_before["run"].get("env_ref")
    assert not any(a.get("kind") == "code_snapshot" for a in bundle_before["artifacts"])
    run.finish("completed")  # must not warn: finish() drains before checking
    assert not [w for w in recwarn if "reproducible" in str(w.message)]
    # The capture itself (env_ref + code-snapshot artifact) is now delivered --
    # that is what the check observed. The terminal status PATCH is a separate,
    # always-journaled write under async_writes (existing behavior, untouched
    # by this fix); a later drain delivers it.
    bundle_after = client.run_bundle(run.id)
    assert bundle_after["run"].get("env_ref")
    assert any(a.get("kind") == "code_snapshot" for a in bundle_after["artifacts"])
    client.flush()
    bundle_final = client.run_bundle(run.id)
    assert bundle_final["run"]["status"] == "completed"


def test_finish_survives_check_failure(client, monkeypatch):
    monkeypatch.setenv("PROBE_AUTO_SNAPSHOT", "1")
    run = open_run(client, experiment="exp-warn-neterr")
    monkeypatch.setattr(
        type(client),
        "check_run",
        lambda self, rid, **kw: (_ for _ in ()).throw(RuntimeError("net down")),
    )
    run.finish("completed")  # warning probe failure must never break close-out
