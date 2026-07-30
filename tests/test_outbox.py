"""The async outbox: snapshot_file, journal, classifier, drain, worker, CLI.

Covers the eng-review test matrix (2026-07-29): enqueue paths, FIFO drain with
the phase-aware failure policy (permanent -> DLQ + continue; transient ->
stop-and-wait; auth -> halt with items untouched; 409-with-existing_id ->
idempotent success), run-scoped barriers, legacy spool import, and the three
regression guards (sync path unchanged, transient still stops, flush ≡ drain).

Fixtures set BOTH PROBE_CONFIG_PATH and XDG_CONFIG_HOME (recorded pitfall:
the tap and the SDK resolve the config differently; setting only one lets a
test write the developer's real config).
"""

from __future__ import annotations

import os
import stat

import pytest

from probe.sdk import errors
from probe.sdk.durable import snapshot_file
from probe.sdk.journal import AuthBlocked  # noqa: F401 -- part of the public surface
from probe.sdk.journal import Journal, classify, drain, run_ref_for_path
from probe.sdk.spool import Spool

from tests.conftest import make_client


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(tmp_path / "config" / "probe.json"))
    monkeypatch.delenv("PROBE_TOKEN", raising=False)
    monkeypatch.delenv("PROBE_ASYNC", raising=False)


def journal_at(tmp_path, **kwargs) -> Journal:
    return Journal(tmp_path / "outbox", **kwargs)


# -- snapshot_file -----------------------------------------------------------


def test_snapshot_is_immutable_against_source_mutation(tmp_path):
    source = tmp_path / "ckpt.bin"
    source.write_bytes(b"epoch-1 weights")
    dst = tmp_path / "snap.bin"
    snapshot_file(source, dst)
    source.write_bytes(b"epoch-2 weights overwrite")
    assert dst.read_bytes() == b"epoch-1 weights"
    assert stat.S_IMODE(dst.stat().st_mode) == 0o600


def test_snapshot_missing_source_raises_and_leaves_no_debris(tmp_path):
    with pytest.raises(FileNotFoundError):
        snapshot_file(tmp_path / "nope.bin", tmp_path / "snap.bin")
    leftovers = [p for p in tmp_path.iterdir()]
    assert leftovers == []


# -- classifier --------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "verdict"),
    [
        (errors.TransportError("net down"), "transient"),
        (errors.ServerError("boom", status=503), "transient"),
        (errors.RosError("slow down", status=429), "transient"),
        (errors.RosError("timeout", status=408), "transient"),
        (errors.ValidationError("bad payload", status=422), "permanent"),
        (errors.NotFoundError("gone", status=404), "permanent"),
        (errors.AuthError("expired", status=401), "auth"),
        (errors.ScopeError("forbidden", status=403), "auth"),
        (errors.ConflictError("dup", detail={"existing_id": "abc"}), "idempotent"),
        (errors.ConflictError("lifecycle", detail="run is deleted"), "permanent"),
        (RuntimeError("our own bug"), "transient"),
    ],
)
def test_classify(exc, verdict):
    assert classify(exc) == verdict


def test_run_ref_extraction():
    assert run_ref_for_path("/v1/runs/r-1/metrics") == "r-1"
    assert run_ref_for_path("/v1/runs/r-1") == "r-1"
    assert run_ref_for_path("/v1/experiments/e-1/artifacts") is None


# -- journal mechanics -------------------------------------------------------


def test_append_orders_ops_and_hardens_permissions(tmp_path):
    journal = journal_at(tmp_path, context={"name": "ctx", "base_url": "http://test"})
    first = journal.append_http("POST", "/v1/runs/r-1/metrics", {"n": 1})
    second = journal.append_http("POST", "/v1/runs/r-1/metrics", {"n": 2})
    ops = [op for _, op in journal.pending()]
    assert [op["op_id"] for op in ops] == [first, second]
    assert ops[0]["run_ref"] == "r-1"
    assert ops[0]["context"] == {"name": "ctx", "base_url": "http://test"}
    assert stat.S_IMODE(journal.ops_dir.stat().st_mode) == 0o700
    path, _ = journal.pending()[0]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    status = Journal.read_status(journal.dir)
    assert status["pending"] == 2 and status["failed"] == 0


def test_journal_never_stores_credentials(tmp_path, app):
    client = make_client(app, tmp_spool=tmp_path / "outbox", async_writes=True)
    client.write("POST", "/v1/runs/r-1/metrics", {"points": []})
    everything = "".join(
        p.read_text() for p in (tmp_path / "outbox").rglob("*") if p.is_file()
    )
    assert "ros_pat_" not in everything and "ros_ing_" not in everything


def test_import_spool_folds_legacy_records_in_order(tmp_path):
    spool = Spool(tmp_path / "legacy")
    spool.append("POST", "/v1/runs/r-1/metrics", {"n": 1})
    spool.append("POST", "/v1/runs/r-1/metrics", {"n": 2})
    journal = journal_at(tmp_path)
    imported = journal.import_spool(spool)
    assert imported == 2
    assert [op["body"]["n"] for _, op in journal.pending()] == [1, 2]
    assert not spool.file.exists() and not spool.inflight_file.exists()


def test_retry_failed_requeues_dead_letters(tmp_path):
    journal = journal_at(tmp_path)
    op_id = journal.append_http("POST", "/v1/nope", {})
    path, op = journal.pending()[0]
    (journal.failed_dir).mkdir(parents=True, exist_ok=True)
    os.replace(path, journal.failed_dir / path.name)
    assert journal.pending() == []
    assert journal.retry_failed(op_id) == 1
    assert [o["op_id"] for _, o in journal.pending()] == [op_id]


def test_gc_blobs_keeps_referenced_bytes(tmp_path):
    journal = journal_at(tmp_path)
    src = tmp_path / "f.bin"
    src.write_bytes(b"bytes")
    journal.append_upload(
        anchor="run", anchor_id="r-1", name="f.bin", src_path=str(src),
        run_ref="r-1", blob="a" * 64,
    )
    orphan = journal.blobs_dir / ("b" * 64)
    orphan.write_bytes(b"orphan")
    assert journal.gc_blobs() == 1
    assert (journal.blobs_dir / ("a" * 64)).exists()
    assert not orphan.exists()


# -- drain -------------------------------------------------------------------


def drain_with(app, journal, **kwargs):
    client = make_client(app)
    try:
        return drain(journal, client_factory=lambda ctx: client, **kwargs)
    finally:
        client.close()


def seeded_run(app, tmp_path):
    from tests.conftest import open_run

    client = make_client(app)
    run = open_run(client, experiment="e", name="r")
    client.close()
    return run.id


def test_drain_delivers_http_ops_in_order(app, tmp_path):
    run_id = seeded_run(app, tmp_path)
    journal = journal_at(tmp_path)
    for n in (1, 2, 3):
        journal.append_http(
            "POST", f"/v1/runs/{run_id}/metrics",
            {"points": [{"key": "loss", "kind": "model", "value": float(n), "step_index": n}]},
        )
    report = drain_with(app, journal)
    assert report.delivered == 3 and report.clean
    steps = [p["step_index"] for p in app.metric_points_posted[run_id]]
    assert steps == [1, 2, 3]
    assert journal.pending() == []


def test_permanent_rejection_dead_letters_and_queue_flows(app, tmp_path):
    run_id = seeded_run(app, tmp_path)
    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/runs/poisoned/badroute", {})
    journal.append_http(
        "POST", f"/v1/runs/{run_id}/metrics",
        {"points": [{"key": "loss", "kind": "model", "value": 1.0, "step_index": 1}]},
    )
    report = drain_with(app, journal)
    assert report.dead_lettered == 1
    assert report.delivered == 1, "the op behind the poison pill must deliver"
    (failed_path, failed_op), = journal.failed()
    assert failed_op["attempts"] == 1
    assert "NotFoundError" in failed_op["last_error"]


def test_transient_failure_stops_in_place(app, tmp_path):
    """REGRESSION guard: a 503 must stop the ordered replay, exactly like the
    old spool's stop-on-first-failure, and must not dead-letter anything."""
    run_id = seeded_run(app, tmp_path)
    journal = journal_at(tmp_path)
    journal.append_http(
        "POST", f"/v1/runs/{run_id}/metrics",
        {"points": [{"key": "a", "kind": "model", "value": 1.0, "step_index": 1}]},
    )
    journal.append_http(
        "POST", f"/v1/runs/{run_id}/metrics",
        {"points": [{"key": "b", "kind": "model", "value": 2.0, "step_index": 2}]},
    )
    app.fail_next_metrics = True  # one 503, then healthy
    report = drain_with(app, journal)
    assert report.stopped_transient and report.delivered == 0
    assert report.remaining == 2 and journal.failed() == []
    report = drain_with(app, journal)
    assert report.delivered == 2 and report.clean


def test_auth_block_halts_and_keeps_everything(tmp_path):
    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/runs/r-1/metrics", {"points": []})
    journal.append_http("POST", "/v1/runs/r-1/metrics", {"points": []})

    class RevokedTransport:
        def request(self, *a, **k):
            raise errors.AuthError("revoked", status=401)

    class RevokedClient:
        transport = RevokedTransport()

        def close(self):
            pass

    report = drain(journal, client_factory=lambda ctx: RevokedClient())
    assert report.auth_blocked
    assert report.remaining == 2 and journal.failed() == []
    status = Journal.read_status(journal.dir)
    assert status["auth_blocked_since"]


def test_conflict_with_existing_id_counts_as_delivered(tmp_path):
    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/runs/r-1/artifacts", {"name": "n"})

    class ReplayedTransport:
        def request(self, *a, **k):
            raise errors.ConflictError("dup", detail={"existing_id": "a-1"})

    class ReplayedClient:
        transport = ReplayedTransport()

        def close(self):
            pass

    report = drain(journal, client_factory=lambda ctx: ReplayedClient())
    assert report.delivered == 1 and report.clean


def test_upload_op_stages_hashes_and_confirms(app, tmp_path):
    run_id = seeded_run(app, tmp_path)
    journal = journal_at(tmp_path)
    src = tmp_path / "model.bin"
    src.write_bytes(b"weights " * 1024)
    # blob=None exercises the 11A big-file path: hashing happens at drain.
    journal.append_upload(
        anchor="run", anchor_id=run_id, name="model.bin", src_path=str(src),
        run_ref=run_id, blob=None,
    )
    src.write_bytes(b"mutated after enqueue")  # must not affect the upload
    report = drain_with(app, journal)
    assert report.clean and report.delivered == 1
    (artifact,) = app.artifacts[run_id]
    assert artifact["status"] == "complete"
    assert artifact["size_bytes"] == len(b"weights " * 1024)
    assert list(journal.blobs_dir.iterdir()) == [], "blob must be GC'd after delivery"


def test_run_scoped_drain_leaves_other_runs(app, tmp_path):
    run_a = seeded_run(app, tmp_path)
    journal = journal_at(tmp_path)
    journal.append_http(
        "POST", f"/v1/runs/{run_a}/metrics",
        {"points": [{"key": "a", "kind": "model", "value": 1.0, "step_index": 1}]},
    )
    journal.append_http("POST", "/v1/runs/other-run/metrics", {"points": []})
    report = drain_with(app, journal, run_ref=run_a)
    assert report.delivered == 1
    assert [op["run_ref"] for _, op in journal.pending()] == ["other-run"]


def test_paused_journal_does_not_drain(app, tmp_path):
    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/runs/r-1/metrics", {"points": []})
    journal.pause()
    report = drain_with(app, journal)
    assert report.delivered == 0 and report.remaining == 1
    journal.resume()


# -- client integration ------------------------------------------------------


def test_async_client_journals_without_touching_the_network(app, tmp_path):
    client = make_client(app, tmp_spool=tmp_path / "outbox", async_writes=True)
    before = len(app.requests)
    client.write("POST", "/v1/runs/r-1/metrics", {"points": []})
    assert len(app.requests) == before, "async write must not touch the network"
    assert len(client.journal.pending()) == 1
    client.close()


def test_flush_delivers_journaled_writes(app, tmp_path):
    run_id = seeded_run(app, tmp_path)
    client = make_client(app, tmp_spool=tmp_path / "outbox", async_writes=True)
    client.write(
        "POST", f"/v1/runs/{run_id}/metrics",
        {"points": [{"key": "loss", "kind": "model", "value": 1.0, "step_index": 1}]},
    )
    assert client.flush() == 1
    assert client.journal.pending() == []
    client.close()


# -- worker ------------------------------------------------------------------


def test_maybe_spawn_declines_when_idle_or_paused(tmp_path, monkeypatch):
    from probe.cli import outbox_worker

    journal = journal_at(tmp_path)
    assert outbox_worker.maybe_spawn(str(journal.dir)) is False  # empty
    journal.append_http("POST", "/v1/x", {})
    journal.pause()
    assert outbox_worker.maybe_spawn(str(journal.dir)) is False  # paused
    journal.resume()
    journal.write_status(auth_blocked_since="2026-07-29T00:00:00Z")
    assert outbox_worker.maybe_spawn(str(journal.dir)) is False  # auth-blocked


def test_worker_loop_backs_off_then_exits_when_empty(tmp_path, monkeypatch):
    from probe.cli import outbox_worker
    from probe.sdk.journal import DrainReport

    reports = [
        DrainReport(delivered=0, remaining=1, stopped_transient=True, errors=["net"]),
        DrainReport(delivered=1, remaining=0),
    ]
    slept: list[float] = []
    monkeypatch.setattr("probe.sdk.journal.drain", lambda j, **k: reports.pop(0))
    monkeypatch.setattr(outbox_worker.time, "sleep", slept.append)
    assert outbox_worker.run(str(tmp_path / "outbox")) == 0
    assert slept == [2.0]


def test_worker_loop_exits_hard_on_auth_block(tmp_path, monkeypatch):
    from probe.cli import outbox_worker
    from probe.sdk.journal import DrainReport

    monkeypatch.setattr(
        "probe.sdk.journal.drain",
        lambda j, **k: DrainReport(remaining=2, auth_blocked=True, errors=["401"]),
    )
    assert outbox_worker.run(str(tmp_path / "outbox")) == 3
