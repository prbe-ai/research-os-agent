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
    queued = journal.append_upload(
        anchor="run", anchor_id="r-1", name="f.bin", src_path=str(src),
        run_ref="r-1", inline_hash=True,
    )
    orphan = journal.blobs_dir / ("b" * 64)
    orphan.write_bytes(b"orphan")
    assert journal.gc_blobs() == 1
    assert (journal.blobs_dir / queued["blob"]).exists()
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


def test_conflict_on_retry_counts_as_idempotent_delivery(tmp_path):
    """409-with-existing_id is claimed as our own earlier delivery ONLY when
    the op has a prior attempt (first-attempt policy lives in
    test_first_attempt_conflict_dead_letters_not_swallowed)."""
    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/runs/r-1/artifacts", {"name": "n"})
    path, op = journal.pending()[0]
    op["attempts"] = 1  # a previous attempt may have half-delivered
    path.write_text(__import__("json").dumps(op))

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
        run_ref=run_id, inline_hash=False,
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
    from probe.sdk import outbox_worker

    journal = journal_at(tmp_path)
    assert outbox_worker.maybe_spawn(str(journal.dir)) is False  # empty
    journal.append_http("POST", "/v1/x", {})
    journal.pause()
    assert outbox_worker.maybe_spawn(str(journal.dir)) is False  # paused
    journal.resume()
    journal.write_status(auth_blocked_since="2026-07-29T00:00:00Z")
    assert outbox_worker.maybe_spawn(str(journal.dir)) is False  # auth-blocked


def test_worker_loop_backs_off_then_exits_when_empty(tmp_path, monkeypatch):
    from probe.sdk import outbox_worker
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
    from probe.sdk import outbox_worker
    from probe.sdk.journal import DrainReport

    monkeypatch.setattr(
        "probe.sdk.journal.drain",
        lambda j, **k: DrainReport(remaining=2, auth_blocked=True, errors=["401"]),
    )
    assert outbox_worker.run(str(tmp_path / "outbox")) == 3


def test_gc_ignores_staging_dotfiles(tmp_path):
    """Review fix: append_upload stages to a dot-prefixed name and publishes
    it under the append lock, so gc can never reap a mid-enqueue blob."""
    journal = journal_at(tmp_path)
    journal._ensure()
    staging = journal.blobs_dir / ".staging-abc123"
    staging.write_bytes(b"mid-enqueue bytes")
    assert journal.gc_blobs() == 0
    assert staging.exists()


def test_drain_persists_deferred_hash_before_delivery(app, tmp_path):
    """Review fix: the 11A hash+rename must be written back to the op file
    BEFORE the upload attempt -- a crash after the rename must not strand the
    op pointing at a staging name that no longer exists."""
    run_id = seeded_run(app, tmp_path)
    journal = journal_at(tmp_path)
    src = tmp_path / "big.bin"
    src.write_bytes(b"weights " * 2048)
    journal.append_upload(
        anchor="run", anchor_id=run_id, name="big.bin", src_path=str(src),
        run_ref=run_id, inline_hash=False,
    )

    class HashesThenDies:
        class transport:  # noqa: N801 -- structural stub
            @staticmethod
            def request(*a, **k):
                raise errors.TransportError("net down")

        @staticmethod
        def upload_fingerprinted(*a, **k):
            raise errors.TransportError("net down mid-upload")

        @staticmethod
        def close():
            pass

    report = drain(journal, client_factory=lambda ctx: HashesThenDies())
    assert report.stopped_transient and report.remaining == 1
    (_, op), = journal.pending()
    digest = op["upload"]["blob"]
    assert digest is not None, "hash+rename must be persisted to the op file"
    assert (journal.blobs_dir / digest).exists()
    assert not any(p.name.startswith("incoming-") for p in journal.blobs_dir.iterdir())
    # And the recovered op delivers cleanly on the next drain.
    assert drain_with(app, journal).clean


# -- review-pass additions (testing + performance + security specialists) -----


def test_drain_without_factory_auth_blocks_on_unmatched_pin(tmp_path):
    """Production credential path (no client_factory): an op pinned to an
    endpoint no stored context matches must auth-block -- never borrow an
    ambient token issued for a different host (security review)."""
    journal = Journal(
        tmp_path / "outbox",
        context={"name": "ghost", "base_url": "http://elsewhere"},
    )
    journal.append_http("POST", "/v1/runs/r-1/metrics", {"points": []})
    report = drain(journal)
    assert report.auth_blocked and report.remaining == 1
    assert journal.failed() == []


def test_missing_staged_bytes_dead_letter_not_livelock(app, tmp_path):
    """ValidationError without an HTTP status must classify permanent: a
    forever-transient local error would park the drainer at the backoff cap
    for eternity (performance review)."""
    run_id = seeded_run(app, tmp_path)
    journal = journal_at(tmp_path)
    src = tmp_path / "gone.bin"
    src.write_bytes(b"bytes")
    queued = journal.append_upload(
        anchor="run", anchor_id=run_id, name="gone.bin", src_path=str(src),
        run_ref=run_id, inline_hash=True,
    )
    (journal.blobs_dir / queued["blob"]).unlink()
    src.unlink()
    report = drain_with(app, journal)
    assert report.dead_lettered == 1 and not report.stopped_transient
    (_, op), = journal.failed()
    assert "gone" in op["last_error"]


def test_unknown_op_kind_dead_letters(app, tmp_path):
    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/x", {})
    path, op = journal.pending()[0]
    op["kind"] = "from-the-future"
    path.write_text(__import__("json").dumps(op))
    report = drain_with(app, journal)
    assert report.dead_lettered == 1 and report.remaining == 0


def test_last_error_redacts_presigned_urls(tmp_path):
    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/x", {})

    class LeakyTransport:
        def request(self, *a, **k):
            raise errors.ValidationError(
                "PUT https://r2.example/put/abc?X-Amz-Signature=SECRET123: rejected",
                status=422,
            )

    class LeakyClient:
        transport = LeakyTransport()

        def close(self):
            pass

    drain(journal, client_factory=lambda ctx: LeakyClient())
    (_, op), = journal.failed()
    assert "SECRET123" not in op["last_error"]
    assert "<redacted>" in op["last_error"]
    status = Journal.read_status(journal.dir)
    assert "SECRET123" not in (status.get("last_error") or "")


def test_drain_lock_contention_reports_without_touching_queue(tmp_path):
    import fcntl

    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/x", {})
    journal._ensure()
    holder = open(journal.drain_lock, "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    try:
        report = drain(journal, wait_for_lock=False)
        assert report.remaining == 1 and report.delivered == 0
        assert any("another drain holds the lock" in e for e in report.errors)
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()


def test_maybe_spawn_spawns_detached_worker(tmp_path, monkeypatch):
    from probe.sdk import outbox_worker

    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/x", {})
    calls: list = []
    monkeypatch.setattr(
        outbox_worker.subprocess, "Popen", lambda argv, **kw: calls.append((argv, kw))
    )
    assert outbox_worker.maybe_spawn(str(journal.dir)) is True
    argv, kw = calls[0]
    assert argv[1:3] == ["-m", "probe.sdk.outbox_worker"]
    assert kw["start_new_session"] is True
    import stat as stat_module

    mode = stat_module.S_IMODE((journal.dir / "drainer.log").stat().st_mode)
    assert mode == 0o600


def test_worker_run_exits_4_when_paused(tmp_path):
    from probe.sdk import outbox_worker

    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/x", {})
    journal.pause()
    assert outbox_worker.run(str(journal.dir)) == 4


def test_harbor_clone_branch_retries_then_raises_on_mutation(tmp_path, monkeypatch):
    """The clone-first branch's mutation guard is testable on ANY filesystem by
    faking try_clone (testing review: the logic had never executed anywhere)."""
    import shutil as shutil_module

    from probe.connectors import harbor

    def fake_clone(src, dst):
        shutil_module.copyfile(src, dst)
        return True

    monkeypatch.setattr(harbor, "try_clone", fake_clone)
    source = tmp_path / "src.bin"
    source.write_bytes(b"stable contents")
    digest, size = harbor._copy_and_hash(source, tmp_path / "out.bin")
    assert size == len(b"stable contents")

    real_stat = harbor.Path.stat
    calls = {"n": 0}

    def mutating_stat(self, **kw):
        result = real_stat(self, **kw)
        if self == source:
            calls["n"] += 1
            if calls["n"] % 2 == 0:  # every after-stat sees a "new" file
                source.touch()
                return real_stat(self, **kw)
        return result

    # A private MonkeyPatch: calling the FIXTURE's undo() would also revert
    # the autouse env isolation and trip the conftest teardown guard.
    stat_patch = pytest.MonkeyPatch()
    stat_patch.setattr(harbor.Path, "stat", mutating_stat, raising=False)
    try:
        with pytest.raises(RuntimeError, match="source changed while staging"):
            harbor._copy_and_hash(source, tmp_path / "out2.bin")
    finally:
        stat_patch.undo()


# -- codex adversarial-pass regressions ---------------------------------------


def test_first_attempt_conflict_dead_letters_not_swallowed(tmp_path):
    """A 409-with-existing_id on a FIRST attempt is a genuine natural-key
    conflict -- treating it as idempotent success would silently discard the
    queued write (codex). Only a RETRY may claim its own earlier delivery."""
    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/runs/r-1/artifacts", {"name": "n"})

    class Conflicted:
        class transport:  # noqa: N801
            @staticmethod
            def request(*a, **k):
                raise errors.ConflictError("dup", detail={"existing_id": "a-1"})

        @staticmethod
        def close():
            pass

    report = drain(journal, client_factory=lambda ctx: Conflicted())
    assert report.dead_lettered == 1 and report.delivered == 0
    # The same conflict on an op that has already been attempted counts as
    # our own half-delivered retry.
    journal.retry_failed()
    report = drain(journal, client_factory=lambda ctx: Conflicted())
    assert report.delivered == 1 and report.dead_lettered == 0


def test_corrupt_op_files_are_quarantined_visibly(app, tmp_path):
    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/runs/r-1/metrics", {"points": []})
    journal._ensure()
    bad = journal.ops_dir / "00000000000000000000-corrupt.json"
    bad.write_text("{not json")
    journal.write_status()
    report = drain_with(app, journal)
    assert report.dead_lettered == 0  # quarantine is not a dead-letter event
    assert not bad.exists(), "corrupt op must leave ops/"
    assert (journal.failed_dir / bad.name).exists(), "…and stay visible in failed/"
    status = Journal.read_status(journal.dir)
    assert status["pending"] == 0, "status must agree with what drain can see"
    assert status["failed"] >= 1


def test_custom_journal_dir_never_steals_the_global_spool(tmp_path, monkeypatch):
    """Only the DEFAULT journal auto-imports the legacy spool: a Client with a
    custom spool_dir must not migrate (and delete) the machine's global
    pending writes into its private directory (codex)."""
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.delenv("PROBE_OUTBOX_DIR", raising=False)
    monkeypatch.delenv("PROBE_SPOOL_DIR", raising=False)
    legacy = Spool()  # resolves under the isolated XDG_STATE_HOME
    legacy.append("POST", "/v1/x", {"n": 1})
    custom = Journal(tmp_path / "custom")
    custom.append_http("POST", "/v1/y", {})
    assert legacy.file.exists(), "custom journal must not consume the global spool"
    assert len(custom.pending()) == 1
    default = Journal()  # the default dir DOES fold the legacy spool in
    default.append_http("POST", "/v1/z", {})
    assert not legacy.file.exists()
    assert [op["path"] for _, op in default.pending()] == ["/v1/x", "/v1/z"]


def test_clear_auth_block_reopens_spawning(tmp_path):
    from probe.sdk import outbox_worker

    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/x", {})
    journal.write_status(auth_blocked_since="2026-07-30T00:00:00Z")
    assert outbox_worker.maybe_spawn(str(journal.dir)) is False
    journal.clear_auth_block()
    calls: list = []
    mp = pytest.MonkeyPatch()
    mp.setattr(outbox_worker.subprocess, "Popen", lambda *a, **k: calls.append(a))
    try:
        assert outbox_worker.maybe_spawn(str(journal.dir)) is True
    finally:
        mp.undo()


def test_inline_hash_is_taken_from_the_snapshot(tmp_path):
    """TOCTOU guard: the recorded digest must describe the STAGED bytes, so a
    source rewrite between enqueue steps can never poison the content
    address (codex)."""
    import hashlib

    journal = journal_at(tmp_path)
    src = tmp_path / "f.bin"
    src.write_bytes(b"original contents")
    queued = journal.append_upload(
        anchor="run", anchor_id="r-1", name="f.bin", src_path=str(src),
        run_ref="r-1", inline_hash=True,
    )
    src.write_bytes(b"rewritten after enqueue")
    staged = (journal.blobs_dir / queued["blob"]).read_bytes()
    assert staged == b"original contents"
    assert queued["blob"] == hashlib.sha256(b"original contents").hexdigest()


# -- a queued petname reaches a UUID-typed route -----------------------------
#
# Enqueue does not read the run on purpose: `--async` exists so a write can be
# queued with no network. Every route the drainer replays EXCEPT
# `GET /v1/runs/{ref}` types its path param as a UUID, so
# `probe --async log tunneling-sambar-254 ...` queued cleanly, reported
# `failed: 0`, and dead-lettered on a 422 minutes later where nobody saw it.


class _PetnameBackend:
    """Accepts the UUID and 422s the petname, like the real routes."""

    RUN_ID = "0f8e1c26-1c2f-4d2f-9c1f-2b6d5a1e9c00"

    def __init__(self, *, resolvable=True):
        self.resolvable = resolvable
        self.paths: list[str] = []
        self.lookups: list[str] = []

    # -- transport
    @property
    def transport(self):
        return self

    def request(self, method, path, json_body=None):
        self.paths.append(path)
        if run_ref_for_path(path) not in (None, self.RUN_ID):
            raise errors.ValidationError(f"badly formed uuid in {path}", status=422)
        return {}

    # -- client
    def get_run(self, ref):
        self.lookups.append(ref)
        if not self.resolvable:
            raise errors.NotFoundError(f"no run {ref}")
        return {"id": self.RUN_ID, "short_id": ref}

    def close(self):
        pass


def test_a_queued_petname_is_resolved_and_delivered(tmp_path):
    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/runs/tunneling-sambar-254/metrics", {"points": []})

    backend = _PetnameBackend()
    report = drain(journal, client_factory=lambda ctx: backend)

    assert report.delivered == 1
    assert report.dead_lettered == 0
    assert backend.paths[-1] == f"/v1/runs/{_PetnameBackend.RUN_ID}/metrics"


def test_the_lookup_happens_only_after_the_422(tmp_path):
    """Not before it: the happy path must not pay a round trip, and a genuine
    body-validation 422 must not be hidden behind a lookup of our own."""
    journal = journal_at(tmp_path)
    journal.append_http("POST", f"/v1/runs/{_PetnameBackend.RUN_ID}/metrics", {"points": []})

    backend = _PetnameBackend()
    report = drain(journal, client_factory=lambda ctx: backend)

    assert report.delivered == 1
    assert backend.lookups == [], "a UUID path resolved something it already had"


def test_one_lookup_serves_every_op_for_the_same_run(tmp_path):
    journal = journal_at(tmp_path)
    for _ in range(4):
        journal.append_http("POST", "/v1/runs/tunneling-sambar-254/metrics", {"points": []})

    backend = _PetnameBackend()
    report = drain(journal, client_factory=lambda ctx: backend)

    assert report.delivered == 4
    assert backend.lookups == ["tunneling-sambar-254"], backend.lookups


def test_an_unresolvable_ref_dead_letters_on_the_servers_error(tmp_path):
    """Not on ours. The 422 the server gave is the better diagnosis, and a
    swallowed lookup failure would replace it with a 404 about a lookup the
    caller never asked for."""
    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/runs/no-such-petname/metrics", {"points": []})

    backend = _PetnameBackend(resolvable=False)
    report = drain(journal, client_factory=lambda ctx: backend)

    assert report.dead_lettered == 1
    assert "badly formed uuid" in report.errors[-1]


def test_the_queued_ref_survives_in_the_op_file(tmp_path):
    """The substitution is per-attempt. `run_ref` is the barrier-drain scoping
    key, so a queued petname has to keep matching a barrier armed on it."""
    journal = journal_at(tmp_path)
    journal.append_http("POST", "/v1/runs/tunneling-sambar-254/metrics", {"points": []})

    backend = _PetnameBackend()
    # A barrier scoped to the petname must still select the op.
    report = drain(journal, run_ref="tunneling-sambar-254", client_factory=lambda ctx: backend)

    assert report.delivered == 1
