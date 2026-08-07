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
    monkeypatch.setenv("PROBE_BASE_URL", "http://test")
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
        # The alias claim is about WIRING: flush must drain the --spool-dir
        # journal, unscoped (testing review: a stub ignoring `journal` could
        # not catch flush resolving the wrong directory).
        assert journal.dir == outbox_dir and run_ref is None
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


# -- run-scoped repair (parity F6) ---------------------------------------------


def test_outbox_retry_and_status_scope_to_a_run(wired_async, outbox_dir, capsys):
    journal = Journal(outbox_dir)
    journal.append_http("POST", "/v1/runs/r-1/badroute", {})
    journal.append_http("POST", "/v1/runs/r-2/badroute", {})
    cli_drain(wired_async, outbox_dir)  # dead-letters both
    capsys.readouterr()

    rc = cli.main(["--spool-dir", str(outbox_dir), "outbox", "status", "--run", "r-1"])
    assert rc == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["run"] == "r-1"
    assert summary["failed"] == 1, "the other run's dead letter must not count"

    rc = cli.main(["--spool-dir", str(outbox_dir), "outbox", "retry", "--run", "r-1"])
    assert rc == 0
    assert "requeued 1 op(s)" in capsys.readouterr().out
    assert [op["run_ref"] for _, op in Journal(outbox_dir).pending()] == ["r-1"]
    assert [op["run_ref"] for _, op in Journal(outbox_dir).failed()] == ["r-2"]


# -- producer accounting (parity F4) ------------------------------------------


def test_outbox_status_reports_producers(wired_async, outbox_dir, capsys):
    run_id = start_run(wired_async)
    cli.main(["--async", "log", run_id, "loss=1.0", "--step", "1"])
    capsys.readouterr()
    rc = cli.main(["--spool-dir", str(outbox_dir), "outbox", "status"])
    assert rc == 2  # pending op
    summary = json.loads(capsys.readouterr().out)
    (producer,) = summary["producers"]
    assert producer["last_sequence"] == 1
    assert producer["gaps"] == []
    (_, op), = Journal(outbox_dir).pending()
    assert op["producer_sequence"] == 1


# -- bounded finish (parity F3) ----------------------------------------------


def test_run_end_flush_timeout_defers_the_close(
    wired_async, outbox_dir, monkeypatch, capsys
):
    run_id = start_run(wired_async)
    cli.main(["--async", "log", run_id, "loss=1.0", "--step", "1"])
    monkeypatch.setattr(  # every barrier pass parks transiently
        "probe.sdk.journal.drain",
        lambda j, run_ref=None, **k: DrainReport(
            remaining=1, stopped_transient=True, errors=["net down"]
        ),
    )
    rc = cli.main(
        ["--spool-dir", str(outbox_dir), "run", "end", run_id, "--flush-timeout", "0.2"]
    )
    assert rc == 0
    assert "end queued" in capsys.readouterr().err
    ops = [op for _, op in Journal(outbox_dir).pending()]
    assert [op["method"] for op in ops] == ["POST", "PATCH"], (
        "the deferred close must sit BEHIND the run's queued data"
    )
    accounting = ops[-1]["body"]["summary"]["probe_finish"]
    assert accounting["deferred"] is True and accounting["pending_at_exit"] == 1
    assert wired_async.runs[run_id]["status"] != "completed"
    assert "draining" in wired_async.runs[run_id]["tags"], "beacon marked intent"
    # The network comes back: everything lands, in order, and the tag clears.
    assert cli_drain(wired_async, outbox_dir).clean
    row = wired_async.runs[run_id]
    assert row["status"] == "completed"
    assert "draining" not in row["tags"]
    assert row["summary"]["probe_finish"]["deferred"] is True
    assert wired_async.metric_points_posted[run_id]


def test_run_end_flush_timeout_closes_normally_when_it_drains_in_time(
    wired_async, outbox_dir, monkeypatch, capsys
):
    run_id = start_run(wired_async)
    cli.main(["--async", "log", run_id, "loss=1.0", "--step", "1"])
    fake = make_client(wired_async)
    monkeypatch.setattr(  # a drain that can actually reach the fake app
        "probe.sdk.journal.drain",
        lambda j, run_ref=None, **k: drain(
            j, run_ref=run_ref, client_factory=lambda ctx: fake
        ),
    )
    rc = cli.main(
        ["--spool-dir", str(outbox_dir), "run", "end", run_id, "--flush-timeout", "5"]
    )
    fake.close()
    assert rc == 0
    row = wired_async.runs[run_id]
    assert row["status"] == "completed"
    assert "probe_finish" not in (row.get("summary") or {}), (
        "an in-time bounded close is an ordinary close"
    )


def test_run_end_flush_timeout_still_refuses_dead_letters(
    wired_async, outbox_dir, capsys
):
    run_id = start_run(wired_async)
    journal = Journal(outbox_dir)
    journal.append_http("POST", f"/v1/runs/{run_id}/badroute", {})
    assert not cli_drain(wired_async, outbox_dir).clean  # dead-letters it
    rc = cli.main(
        ["--spool-dir", str(outbox_dir), "run", "end", run_id, "--flush-timeout", "0.2"]
    )
    assert rc == 2
    assert "NOT closed" in capsys.readouterr().err


# -- review-pass additions (testing specialist) -------------------------------


def test_banner_surfaces_dead_letters_and_rekicks(wired_async, outbox_dir, capsys):
    journal = Journal(outbox_dir)
    journal.append_http("POST", "/v1/runs/poisoned/badroute", {})
    cli_drain(wired_async, outbox_dir)  # dead-letters it
    journal.append_http("POST", "/v1/runs/r-1/metrics", {"points": []})
    kicks = len(wired_async.spawned)
    cli.main(["--spool-dir", str(outbox_dir), "project", "list"])
    err = capsys.readouterr().err
    assert "outbox:" in err and "dead-lettered" in err
    assert len(wired_async.spawned) > kicks, "pending + healthy must re-kick"


def test_async_span_add_queues_and_prints_id(wired_async, outbox_dir, capsys):
    run_id = start_run(wired_async)
    before = len(wired_async.requests)
    assert cli.main(["--async", "span", "add", run_id, "--type", "tool_call"]) == 0
    span_id = capsys.readouterr().out.strip().splitlines()[-1]
    assert len(wired_async.requests) == before
    (_, op), = Journal(outbox_dir).pending()
    assert op["run_ref"] == run_id and span_id in json.dumps(op["body"])
    assert cli_drain(wired_async, outbox_dir).clean


def test_async_big_file_defers_hash_and_ping(wired_async, outbox_dir, tmp_path, monkeypatch, capsys):
    run_id = start_run(wired_async)
    monkeypatch.setattr("probe.sdk.journal.INLINE_HASH_MAX_BYTES", 8)
    source = tmp_path / "huge.bin"
    source.write_bytes(b"way more than eight bytes")
    before = len(wired_async.requests)
    assert cli.main(["--async", "artifact", "add", run_id, str(source)]) == 0
    assert "intent deferred to drain" in capsys.readouterr().out
    assert len(wired_async.requests) == before, "no ping for big files (11A)"
    (_, op), = Journal(outbox_dir).pending()
    assert op["upload"]["blob"] is None
    assert cli_drain(wired_async, outbox_dir).clean
    assert any(a["status"] == "complete" for a in wired_async.artifacts[run_id])


def test_run_end_passes_run_scope_and_ignores_other_runs(wired_async, outbox_dir, monkeypatch, capsys):
    run_id = start_run(wired_async)
    journal = Journal(outbox_dir)
    journal.append_http("POST", "/v1/runs/other-run/badroute", {})
    cli_drain(wired_async, outbox_dir)  # dead-letters the OTHER run's op
    seen_scope: list = []

    def scoped_drain(j, run_ref=None, **kw):
        seen_scope.append(run_ref)
        return DrainReport()

    monkeypatch.setattr("probe.sdk.journal.drain", scoped_drain)
    rc = cli.main(["--spool-dir", str(outbox_dir), "run", "end", run_id])
    assert rc == 0, "another run's dead letter must not block this run's close"
    # First call is the T3-A barrier (run-scoped); Run.finish()'s own flush
    # then drains unscoped, which is fine — the verdict was already computed.
    assert seen_scope[0] == run_id, "barrier drain must be run-scoped (T3-A)"
    assert wired_async.runs[run_id]["status"] == "completed"


def test_outbox_watch_once_and_retry_unknown_and_failed_only_status(
    wired_async, outbox_dir, monkeypatch, capsys
):
    journal = Journal(outbox_dir)
    journal.append_http("POST", "/v1/runs/poisoned/badroute", {})
    cli_drain(wired_async, outbox_dir)
    capsys.readouterr()
    # failed-only queue (pending == 0) still exits 2
    assert cli.main(["--spool-dir", str(outbox_dir), "outbox", "status"]) == 2
    # retry with a bogus op id: exit 1, nothing requeued
    rc = cli.main(["--spool-dir", str(outbox_dir), "outbox", "retry", "nope"])
    assert rc == 1
    assert "requeued 0" in capsys.readouterr().out
    # watch --once drains once and returns
    monkeypatch.setattr(
        "probe.sdk.journal.drain", lambda j, **kw: DrainReport(delivered=0)
    )
    assert cli.main(["--spool-dir", str(outbox_dir), "outbox", "watch", "--once"]) == 0


def test_run_end_refuses_while_paused(wired_async, outbox_dir, capsys):
    """Codex: a paused journal skipped the drain but run end still closed the
    run -- the barrier is about the RESULT (nothing of this run still queued)."""
    run_id = start_run(wired_async)
    cli.main(["--async", "log", run_id, "loss=1.0", "--step", "1"])
    assert cli.main(["--spool-dir", str(outbox_dir), "outbox", "pause"]) == 0
    capsys.readouterr()
    rc = cli.main(["--spool-dir", str(outbox_dir), "run", "end", run_id])
    assert rc == 2
    assert "NOT closed" in capsys.readouterr().err
    assert wired_async.runs[run_id]["status"] != "completed"
    cli.main(["--spool-dir", str(outbox_dir), "outbox", "resume"])


# -- bulk import (`artifact add --from-manifest`) -----------------------------
#
# The whole verb is a cost argument: one process, one anchor resolution, N rows
# journalled. So these tests assert the COSTS, not only that rows land -- a
# manifest that enqueued everything correctly while still resolving per row
# would be the feature failing at the only thing it exists to do.


def manifest(tmp_path, *rows, name="manifest.jsonl"):
    path = tmp_path / name
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return str(path)


def seed_project(app, slug):
    cli.main(["project", "create", slug])
    return next(p["id"] for p in app.projects.values() if p["slug"] == slug)


def anchored(app, project_id):
    return app.artifacts.get(f"project:{project_id}", [])


def test_manifest_enqueues_every_row_in_one_process(wired_async, outbox_dir, tmp_path, capsys):
    project_id = seed_project(wired_async, "p")
    files = []
    for i in range(3):
        f = tmp_path / f"shard-{i}.bin"
        f.write_bytes(b"rows " * 64)
        files.append(f)
    path = manifest(
        tmp_path,
        {"path": str(files[0]), "notes": "first shard"},
        {"path": str(files[1]), "name": "renamed.bin"},
        {"path": str(files[2])},
    )
    capsys.readouterr()

    rc = cli.main(["artifact", "add", "--from-manifest", path, "--project", "p"])

    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["rows"] == 3
    assert summary["enqueued"] == 3
    assert summary["failed"] == 0
    assert summary["failures"] == []
    assert len(Journal(outbox_dir).pending()) == 3
    assert wired_async.spawned, "a manifest must kick the drainer once it has queued"

    assert cli_drain(wired_async, outbox_dir).clean
    landed = {a["name"] for a in anchored(wired_async, project_id)}
    assert landed == {"shard-0.bin", "renamed.bin", "shard-2.bin"}


def test_manifest_reports_a_bad_row_and_still_enqueues_the_rest(
    wired_async, outbox_dir, tmp_path, capsys
):
    seed_project(wired_async, "p")
    good = tmp_path / "good.bin"
    good.write_bytes(b"ok")
    path = manifest(
        tmp_path,
        {"path": str(good)},
        {"notes": "no path at all"},
        {"path": str(tmp_path / "missing.bin")},
        {"path": str(good), "file": "typo'd key"},
        {"path": str(good), "name": "second.bin"},
    )
    capsys.readouterr()

    rc = cli.main(["artifact", "add", "--from-manifest", path, "--project", "p"])

    # Non-zero so an unattended caller notices -- but only AFTER the good rows
    # landed and the summary named the bad ones by line.
    assert rc == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["rows"] == 5
    assert summary["enqueued"] == 2
    assert summary["failed"] == 3
    assert [f["line"] for f in summary["failures"]] == [2, 3, 4]
    assert "needs" in summary["failures"][0]["error"]
    assert "not a regular file" in summary["failures"][1]["error"]
    assert "unknown key" in summary["failures"][2]["error"]
    assert len(Journal(outbox_dir).pending()) == 2


def test_manifest_resolves_each_anchor_once_not_once_per_row(
    wired_async, outbox_dir, tmp_path, capsys
):
    """The cost the verb exists to remove.

    200k rows under one project must not be 200k slug lookups. Counted against
    the fake's request log, so a regression that reintroduced per-row resolution
    shows up here as 6 instead of 2, rather than as a slow import nobody
    profiles."""
    seed_project(wired_async, "p")
    seed_project(wired_async, "second")
    src = tmp_path / "f.bin"
    src.write_bytes(b"bytes")
    rows = []
    for i in range(3):
        rows.append({"path": str(src), "name": f"a-{i}", "project": "p"})
        rows.append({"path": str(src), "name": f"b-{i}", "project": "second"})
    path = manifest(tmp_path, *rows)
    capsys.readouterr()

    before = len(wired_async.requests)
    assert cli.main(["artifact", "add", "--from-manifest", path]) == 0

    summary = json.loads(capsys.readouterr().out)
    assert summary["enqueued"] == 6
    assert summary["anchors_resolved"] == 2, "two distinct slugs, two cache entries"
    lookups = [
        r for r in wired_async.requests[before:]
        if r.url.path == "/v1/projects" and r.url.params.get("slug")
    ]
    assert len(lookups) == 2, (
        f"6 rows over 2 projects must cost 2 lookups, got {len(lookups)}"
    )


def test_manifest_bad_anchor_costs_one_lookup_and_fails_every_row_using_it(
    wired_async, outbox_dir, tmp_path, capsys
):
    src = tmp_path / "f.bin"
    src.write_bytes(b"bytes")
    path = manifest(
        tmp_path,
        *[{"path": str(src), "name": f"a-{i}", "project": "nope"} for i in range(4)],
    )
    capsys.readouterr()

    before = len(wired_async.requests)
    assert cli.main(["artifact", "add", "--from-manifest", path]) == 1

    summary = json.loads(capsys.readouterr().out)
    assert summary["enqueued"] == 0 and summary["failed"] == 4
    assert all("nope" in f["error"] for f in summary["failures"])
    lookups = [
        r for r in wired_async.requests[before:]
        if r.url.path == "/v1/projects" and r.url.params.get("slug") == "nope"
    ]
    assert len(lookups) == 1, (
        "a failing ref must be cached too -- otherwise the error path pays the "
        f"per-row cost the feature removes, got {len(lookups)}"
    )
    assert Journal(outbox_dir).pending() == []


def test_manifest_reference_rows_stage_zero_bytes(wired_async, outbox_dir, tmp_path, capsys):
    project_id = seed_project(wired_async, "p")
    small = tmp_path / "explicit.ckpt"
    small.write_bytes(b"x" * 64)
    big = tmp_path / "over-threshold.ckpt"
    big.write_bytes(b"y" * 4096)
    path = manifest(
        tmp_path,
        {"path": str(small), "reference": True, "notes": "named a reference"},
        # No `reference` key: the SIZE promotes it, which is the shape a
        # generated manifest of checkpoints has.
        {"path": str(big)},
    )
    capsys.readouterr()

    rc = cli.main([
        "artifact", "add", "--from-manifest", path, "--project", "p",
        "--reference-over", "1024",
    ])

    assert rc == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["references"] == 2 and summary["uploads"] == 0
    journal = Journal(outbox_dir)
    assert all(op["kind"] == "http" for _, op in journal.pending())
    assert not journal.blobs_dir.exists() or list(journal.blobs_dir.iterdir()) == [], (
        "a reference row records a path; it must never snapshot the bytes"
    )
    assert cli_drain(wired_async, outbox_dir).clean
    rows = anchored(wired_async, project_id)
    assert len(rows) == 2 and all(r["is_reference"] for r in rows)


def test_manifest_row_anchor_beats_the_command_line_default(
    wired_async, outbox_dir, tmp_path, capsys
):
    project_id = seed_project(wired_async, "p")
    run_id = start_run(wired_async)
    src = tmp_path / "f.bin"
    src.write_bytes(b"bytes")
    path = manifest(
        tmp_path,
        {"path": str(src), "name": "on-the-run", "run": run_id, "kind": "checkpoint"},
        {"path": str(src), "name": "on-the-project"},
    )
    capsys.readouterr()

    assert cli.main(["artifact", "add", "--from-manifest", path, "--project", "p"]) == 0
    assert json.loads(capsys.readouterr().out)["enqueued"] == 2
    assert cli_drain(wired_async, outbox_dir).clean

    assert [a["name"] for a in wired_async.artifacts[run_id]] == ["on-the-run"]
    assert [a["name"] for a in anchored(wired_async, project_id)] == ["on-the-project"]


def test_manifest_queues_without_the_async_flag(wired_async, outbox_dir, tmp_path, capsys):
    """--from-manifest is the bulk path, so it journals whether or not --async is
    set: the alternative is N synchronous uploads inside one process, which is
    the cost the verb exists to remove wearing a different hat."""
    seed_project(wired_async, "p")
    src = tmp_path / "f.bin"
    src.write_bytes(b"bytes")
    path = manifest(tmp_path, {"path": str(src)})
    capsys.readouterr()

    before = len(wired_async.requests)
    assert cli.main(["artifact", "add", "--from-manifest", path, "--project", "p"]) == 0

    assert len(Journal(outbox_dir).pending()) == 1
    posts = [r for r in wired_async.requests[before:] if r.method == "POST"]
    assert posts == [], "enqueue must not upload; the drainer delivers"


def test_manifest_rejects_per_row_flags_on_the_command_line(wired_async, tmp_path, capsys):
    path = manifest(tmp_path, {"path": str(tmp_path / "x")})
    assert cli.main(["artifact", "add", "--from-manifest", path, "--name", "x"]) != 0
    assert "per-ROW" in capsys.readouterr().err


def test_single_file_artifact_add_is_unchanged_by_the_manifest_path(
    wired_async, outbox_dir, tmp_path, capsys
):
    """REGRESSION guard for the refactor: `_artifact_add_async` was split so a
    manifest row could reuse its body, and the single-file path must behave
    identically -- same message, same intent ping, same staged blob."""
    run_id = start_run(wired_async)
    source = tmp_path / "model.bin"
    source.write_bytes(b"weights " * 512)

    assert cli.main(["--async", "artifact", "add", run_id, str(source)]) == 0

    out = capsys.readouterr().out
    assert "queued upload" in out and "intent registered" in out
    (pending_row,) = wired_async.artifacts[run_id]
    assert pending_row["status"] == "pending", "the capped intent ping still fires"
    (_, op), = Journal(outbox_dir).pending()
    assert op["upload"]["blob"] is not None
