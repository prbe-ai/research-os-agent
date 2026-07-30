"""CLI surface of the async outbox: --async / PROBE_ASYNC enqueue paths, the
intent ping, the outbox command family, the flush alias, and the run-end
barrier (sync = run-scoped drain first; async = ordered journal op).

Wiring mirrors test_cli.py: `cli.Client` is monkeypatched to hand back a
FakeApp-backed client, and the drainer spawn is stubbed out (the worker loop
has its own unit tests -- a CLI test must not fork real processes).
"""

from __future__ import annotations

import json

import pytest

from probe import cli
from probe.sdk.journal import DrainReport, Journal, drain

from tests.conftest import make_client, open_run


@pytest.fixture
def outbox_dir(tmp_path):
    return tmp_path / "outbox"


@pytest.fixture
def wired_async(app, outbox_dir, tmp_path, monkeypatch):
    """FakeApp-backed CLI with async credentials available and no real forks."""
    def factory(**kw):
        return make_client(
            app,
            tmp_spool=outbox_dir,
            async_writes=kw.get("async_writes", False),
        )

    monkeypatch.setattr(cli, "Client", factory)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(tmp_path / "config" / "probe.json"))
    monkeypatch.setenv("PROBE_TOKEN", "probe_pat_test")
    spawned: list[str | None] = []
    monkeypatch.setattr(
        "probe.cli.outbox_worker.maybe_spawn",
        lambda directory=None: spawned.append(directory) or False,
    )
    cli.main(["experiment", "create", "e", "--hypothesis", "h"])
    app.spawned = spawned
    return app


def start_run(app) -> str:
    client = make_client(app)
    run = open_run(client, experiment="e", name="r")
    client.close()
    return run.id


def cli_drain(app, outbox_dir, **kwargs) -> DrainReport:
    client = make_client(app)
    try:
        return drain(Journal(outbox_dir), client_factory=lambda ctx: client, **kwargs)
    finally:
        client.close()


# -- enqueue paths -----------------------------------------------------------


def test_sync_log_path_is_unchanged(wired_async, outbox_dir, capsys):
    """REGRESSION guard: without --async the write hits the network and the
    journal stays untouched -- byte-identical to the pre-outbox behavior."""
    run_id = start_run(wired_async)
    rc = cli.main(["log", run_id, "loss=0.5", "--step", "1"])
    assert rc == 0
    assert "logged 1 metric(s)" in capsys.readouterr().out
    assert wired_async.metric_points_posted[run_id]
    assert Journal(outbox_dir).pending() == []


def test_async_log_queues_and_touches_no_run_route(wired_async, outbox_dir, capsys):
    run_id = start_run(wired_async)
    before = len(wired_async.requests)
    rc = cli.main(["--async", "log", run_id, "loss=0.5", "--step", "1"])
    assert rc == 0
    assert "queued 1 metric(s)" in capsys.readouterr().out
    assert len(wired_async.requests) == before, (
        "async log must not call the API -- not even get_run (D20-1)"
    )
    (_, op), = Journal(outbox_dir).pending()
    assert op["kind"] == "http" and op["run_ref"] == run_id
    assert wired_async.spawned, "enqueue must kick the drainer"
    report = cli_drain(wired_async, outbox_dir)
    assert report.clean and wired_async.metric_points_posted[run_id]


def test_probe_async_env_enables_async(wired_async, outbox_dir, monkeypatch, capsys):
    run_id = start_run(wired_async)
    monkeypatch.setenv("PROBE_ASYNC", "1")
    assert cli.main(["log", run_id, "loss=1.0", "--step", "1"]) == 0
    assert "queued" in capsys.readouterr().out
    assert len(Journal(outbox_dir).pending()) == 1


def test_async_artifact_add_pings_intent_and_stages(wired_async, outbox_dir, tmp_path, capsys):
    run_id = start_run(wired_async)
    source = tmp_path / "model.bin"
    source.write_bytes(b"weights " * 512)
    rc = cli.main(["--async", "artifact", "add", run_id, str(source)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "queued upload" in out and "intent registered" in out
    # The capped ping presigned: the server already holds a pending row (1A).
    (pending_row,) = wired_async.artifacts[run_id]
    assert pending_row["status"] == "pending"
    (_, op), = Journal(outbox_dir).pending()
    assert op["upload"]["blob"] is not None, "small file hashes inline (11A)"
    assert op["upload"]["artifact_id"] == pending_row["id"]
    source.write_bytes(b"overwritten")
    report = cli_drain(wired_async, outbox_dir)
    assert report.clean
    # Drain re-presigns (never trusts the ping's row). The REAL server revives
    # the same row in place (uploads_router.py:222); the fake appends a second
    # one -- either way the upload must end complete under this name.
    assert any(
        a["name"] == "model.bin" and a["status"] == "complete"
        for a in wired_async.artifacts[run_id]
    )


def test_async_reference_add_is_a_pure_json_op(wired_async, outbox_dir, tmp_path, capsys):
    run_id = start_run(wired_async)
    source = tmp_path / "big.ckpt"
    source.write_bytes(b"x" * 128)
    rc = cli.main(["--async", "artifact", "add", run_id, str(source), "--reference"])
    assert rc == 0
    assert "queued reference" in capsys.readouterr().out
    journal = Journal(outbox_dir)
    (_, op), = journal.pending()
    assert op["kind"] == "http"
    assert not journal.blobs_dir.exists() or list(journal.blobs_dir.iterdir()) == []
    assert cli_drain(wired_async, outbox_dir).clean
    (artifact,) = wired_async.artifacts[run_id]
    assert artifact["is_reference"] is True


def test_async_requires_deliverable_credentials(wired_async, monkeypatch, capsys):
    monkeypatch.delenv("PROBE_TOKEN", raising=False)
    rc = cli.main(["--async", "log", "r-1", "loss=1.0"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "probe login" in err


# -- outbox command family ---------------------------------------------------


def test_outbox_status_reports_and_exits_two_when_pending(wired_async, outbox_dir, capsys):
    run_id = start_run(wired_async)
    assert cli.main(["--spool-dir", str(outbox_dir), "outbox", "status"]) == 0
    capsys.readouterr()
    cli.main(["--async", "log", run_id, "loss=1.0"])
    capsys.readouterr()
    rc = cli.main(["--spool-dir", str(outbox_dir), "outbox", "status", "--verbose"])
    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["pending"] == 1
    assert payload["ops"][0]["run_ref"] == run_id


def test_outbox_pause_blocks_drain_and_resume_rekicks(wired_async, outbox_dir, capsys):
    run_id = start_run(wired_async)
    cli.main(["--async", "log", run_id, "loss=1.0"])
    assert cli.main(["--spool-dir", str(outbox_dir), "outbox", "pause"]) == 0
    assert cli_drain(wired_async, outbox_dir).delivered == 0
    kicks_before = len(wired_async.spawned)
    assert cli.main(["--spool-dir", str(outbox_dir), "outbox", "resume"]) == 0
    assert len(wired_async.spawned) > kicks_before


def test_outbox_retry_requeues_dead_letters(wired_async, outbox_dir, capsys):
    journal = Journal(outbox_dir)
    journal.append_http("POST", "/v1/runs/poisoned/badroute", {})
    assert not cli_drain(wired_async, outbox_dir).clean
    assert len(journal.failed()) == 1
    assert cli.main(["--spool-dir", str(outbox_dir), "outbox", "retry"]) == 0
    assert "requeued 1" in capsys.readouterr().out
    assert len(journal.pending()) == 1 and journal.failed() == []


def test_flush_is_an_alias_of_outbox_drain(wired_async, outbox_dir, monkeypatch, capsys):
    """REGRESSION guard: `probe flush` must drain the journal exactly like
    `probe outbox drain` (it replaced the old spool-only replay)."""
    run_id = start_run(wired_async)
    cli.main(["--async", "log", run_id, "loss=1.0", "--step", "1"])
    capsys.readouterr()

    def factory_drain(journal, run_ref=None, **kw):
        return cli_drain(wired_async, outbox_dir, run_ref=run_ref)

    monkeypatch.setattr("probe.sdk.journal.drain", factory_drain)
    rc = cli.main(["--spool-dir", str(outbox_dir), "flush"])
    assert rc == 0
    assert "delivered 1" in capsys.readouterr().out
    assert wired_async.metric_points_posted[run_id]


# -- run end barriers --------------------------------------------------------


def test_run_end_sync_refuses_while_run_ops_undeliverable(
    wired_async, outbox_dir, monkeypatch, capsys
):
    run_id = start_run(wired_async)
    monkeypatch.setattr(
        "probe.sdk.journal.drain",
        lambda j, run_ref=None, **k: DrainReport(
            remaining=1, stopped_transient=True, errors=["net down"]
        ),
    )
    rc = cli.main(["--spool-dir", str(outbox_dir), "run", "end", run_id])
    assert rc == 2
    assert "NOT closed" in capsys.readouterr().err
    assert wired_async.runs[run_id]["status"] != "completed"


def test_run_end_sync_refuses_on_this_runs_dead_letters(
    wired_async, outbox_dir, monkeypatch, capsys
):
    run_id = start_run(wired_async)
    journal = Journal(outbox_dir)
    journal.append_http("POST", f"/v1/runs/{run_id}/badroute", {})
    assert not cli_drain(wired_async, outbox_dir).clean  # dead-letters it
    monkeypatch.setattr(
        "probe.sdk.journal.drain", lambda j, run_ref=None, **k: DrainReport()
    )
    rc = cli.main(["--spool-dir", str(outbox_dir), "run", "end", run_id])
    assert rc == 2
    assert "retry dead letters" in capsys.readouterr().err


def test_run_end_sync_closes_when_clean(wired_async, outbox_dir, monkeypatch, capsys):
    run_id = start_run(wired_async)
    monkeypatch.setattr(
        "probe.sdk.journal.drain", lambda j, run_ref=None, **k: DrainReport()
    )
    rc = cli.main(["--spool-dir", str(outbox_dir), "run", "end", run_id])
    assert rc == 0
    assert wired_async.runs[run_id]["status"] == "completed"


def test_run_end_async_is_ordered_behind_the_runs_data(
    wired_async, outbox_dir, capsys
):
    run_id = start_run(wired_async)
    cli.main(["--async", "log", run_id, "loss=1.0", "--step", "1"])
    rc = cli.main(["--async", "run", "end", run_id])
    assert rc == 0
    assert "queued end" in capsys.readouterr().out
    ops = [op for _, op in Journal(outbox_dir).pending()]
    assert [op["method"] for op in ops] == ["POST", "PATCH"], (
        "run_end must sit BEHIND the run's queued data (the ordering IS the barrier)"
    )
    assert wired_async.runs[run_id]["status"] != "completed"
    assert cli_drain(wired_async, outbox_dir).clean
    assert wired_async.runs[run_id]["status"] == "completed"
    assert wired_async.metric_points_posted[run_id]
