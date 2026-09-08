"""Exercise the actual v2 daemon path with the shared journal and no network."""

import hashlib
import json
import multiprocessing
import os
import signal
import time
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tap import main as tapmain
from tap.session_identity import validate_identity
from tap.session_journal import DeliveryPending, Journal
from tap.storage import Storage


class Wire:
    base_url = "http://synthetic.invalid"
    source = "claude_code"
    status = 202
    unsupported = False

    def __init__(self):
        self.accepted = {}

    def receipts(self, sid):
        if self.unsupported:
            raise DeliveryPending("protocol unavailable")
        return dict(
            protocol_version=2,
            customer_id="synthetic",
            source=self.source,
            session_id=sid,
            state="absent",
        )

    def post(self, body):
        if self.status != 202:
            return self.status, {"detail": "authorization pending"}
        data = json.loads(body)
        key = data["session_id"], data["batch_seq"]
        if key in self.accepted:
            assert self.accepted[key] == body
        self.accepted[key] = body
        receipt = {
            k: data[k]
            for k in (
                "batch_seq",
                "source_byte_end",
                "source_line_end",
                "event_end",
                "prefix_sha256",
            )
        }
        receipt.update(
            body_sha256=hashlib.sha256(body).hexdigest(), finalized=bool(data.get("finalize"))
        )
        return 202, dict(protocol_version=2, receipt=receipt)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_TRANSCRIPT_STATE_DIR", str(tmp_path / "v2"))
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("PROBE_BASE_URL", Wire.base_url)
    monkeypatch.setattr(tapmain, "_shutdown_requested", False)
    monkeypatch.setattr(tapmain.killswitch, "is_ingestion_enabled", lambda **kw: (True, None))
    monkeypatch.setattr(tapmain.reconcile, "find_gaps", lambda *args, **kw: [])

    def stop(_seconds):
        tapmain._shutdown_requested = True

    monkeypatch.setattr(tapmain.time, "sleep", stop)
    wire = Wire()
    monkeypatch.setattr(tapmain, "Wire", lambda *args: wire)
    sid = str(uuid4())
    path = tmp_path / f"{sid}.jsonl"
    events = [
        dict(type="user", sessionId=sid, message=dict(role="user", content=f"turn {i}"))
        for i in range(3)
    ]
    path.write_text("".join(json.dumps(e) + "\n" for e in events))
    config = SimpleNamespace(
        session_id=sid,
        transcript_path=path,
        cwd=tmp_path,
        token="synthetic",
        active_interval_s=1,
        idle_interval_s=1,
        shutdown_sentinel=tmp_path / "shutdown",
    )
    storage = Storage(tmp_path / "legacy.db")
    yield config, storage, wire
    storage.close()


def test_daemon_finishes_historical_reservation_and_appended_live_tail(setup):
    config, storage, wire = setup
    journal = Journal(wire.base_url, "synthetic", "claude_code")
    journal.ensure(
        config.session_id, config.transcript_path, wire.receipts(config.session_id), historical=True
    )
    original = journal.stage(config.session_id, cwd=str(config.cwd))
    with config.transcript_path.open("a") as handle:
        handle.write(
            json.dumps(
                dict(
                    type="user",
                    sessionId=config.session_id,
                    message=dict(role="user", content="tail"),
                )
            )
            + "\n"
        )
    journal.close()
    assert tapmain._run_durable_loop(config, storage) == 0
    assert next(iter(wire.accepted.values())) == original
    events = [
        event for body in wire.accepted.values() for event in json.loads(body).get("events", [])
    ]
    assert [e["line_no"] for e in events] == [0, 1, 2, 3]
    journal = Journal(wire.base_url, "synthetic", "claude_code")
    state = journal.get(config.session_id)
    assert state["finalized"] and state["source_byte_end"] == config.transcript_path.stat().st_size
    assert journal.pending(config.session_id) is None
    journal.close()


def test_401_keeps_pending_and_shutdown_intent(setup):
    config, storage, wire = setup
    wire.status = 401
    storage.set_meta("untouched", "legacy")
    assert tapmain._run_durable_loop(config, storage) == 0
    journal = Journal(wire.base_url, "synthetic", "claude_code")
    assert journal.pending(config.session_id)
    assert journal.get(config.session_id)["finalize_requested"]
    assert journal.get(config.session_id)["source_byte_end"] == 0
    assert storage.get_meta("untouched") == "legacy"
    journal.close()


def test_unavailable_protocol_never_stages_or_downgrades(setup):
    config, storage, wire = setup
    wire.unsupported = True
    assert tapmain._run_durable_loop(config, storage) == 0
    assert not wire.accepted
    journal = Journal(wire.base_url, "synthetic", "claude_code")
    assert journal.sessions() == []
    journal.close()


def _other_source(config, *, cwd="/synthetic/other", native=True):
    sid = str(uuid4())
    path = config.transcript_path.parent / f"{sid}.jsonl"
    event = dict(type="user", message=dict(role="user", content="other session"))
    if native:
        event["sessionId"] = sid
    if cwd:
        event["cwd"] = cwd
    path.write_text(json.dumps(event) + "\n")
    return sid, path


@pytest.mark.parametrize("poison_first_four", [False, True])
def test_reconcile_admits_fifth_and_sixth_after_reserved_or_poison_prefix(
    setup, monkeypatch, poison_first_four
):
    config, storage, wire = setup
    gaps = []
    for index in range(6):
        sid, path = _other_source(config, native=not (poison_first_four and index < 4))
        gaps.append(
            tapmain.reconcile.Gap(
                path,
                sid,
                "/incorrect/storage/directory",
                path.stat().st_size,
                0,
                0,
                False,
                int(time.time()),
            )
        )
    monkeypatch.setattr(tapmain.reconcile, "find_gaps", lambda *a, **kw: gaps)
    monkeypatch.setattr(tapmain.reconcile, "RECONCILE_EVERY_TICKS", 1)
    sleeps = 0

    def stop_after_three(_):
        nonlocal sleeps
        sleeps += 1
        tapmain._shutdown_requested = sleeps >= 3

    monkeypatch.setattr(tapmain.time, "sleep", stop_after_three)
    assert tapmain._run_durable_loop(config, storage) == 0
    journal = Journal(wire.base_url, "synthetic", wire.source)
    for gap in gaps[-2:]:
        assert journal.get(gap.session_id)["source_byte_end"] == gap.size
        assert journal.get(gap.session_id)["cwd"] == "/synthetic/other"
    journal.close()


@pytest.mark.parametrize("historical", [False, True])
@pytest.mark.parametrize("pending", [False, True])
def test_shared_drain_honors_each_sources_disabled_folder_and_retains_work(
    setup, historical, pending
):
    config, storage, wire = setup
    sid, path = _other_source(config, cwd="/disabled/project")
    journal = Journal(wire.base_url, "synthetic", wire.source)
    journal.ensure(sid, path, wire.receipts(sid), historical=historical, cwd="/disabled/project")
    original = journal.stage(sid, cwd="/disabled/project") if pending else None
    journal.close()
    disabled = tapmain.cfg.disabled_paths_file()
    disabled.parent.mkdir(parents=True, exist_ok=True)
    disabled.write_text("/disabled\n")
    assert tapmain._run_durable_loop(config, storage) == 0
    assert not any(key[0] == sid for key in wire.accepted)
    journal = Journal(wire.base_url, "synthetic", wire.source)
    assert journal.pending(sid) == original
    assert journal.get(sid)["source_byte_end"] == 0
    assert "capture disabled" in journal.get(sid)["error"]
    journal.close()
    disabled.unlink()
    tapmain._shutdown_requested = False
    assert tapmain._run_durable_loop(config, storage) == 0
    journal = Journal(wire.base_url, "synthetic", wire.source)
    assert journal.get(sid)["source_byte_end"] == path.stat().st_size
    assert journal.pending(sid) is None
    journal.close()


def test_disable_between_staging_and_delivery_retains_immutable_pending(setup, monkeypatch):
    config, storage, wire = setup
    sid, path = _other_source(config)
    journal = Journal(wire.base_url, "synthetic", wire.source)
    journal.ensure(sid, path, wire.receipts(sid), historical=False, cwd="/synthetic/other")
    journal.close()
    disabled = tapmain.cfg.disabled_paths_file()
    disabled.parent.mkdir(parents=True, exist_ok=True)
    stage = Journal.stage

    def disable_after_stage(self, session_id, **kwargs):
        result = stage(self, session_id, **kwargs)
        if session_id == sid:
            disabled.write_text("/synthetic/other\n")
        return result

    monkeypatch.setattr(Journal, "stage", disable_after_stage)
    assert tapmain._run_durable_loop(config, storage) == 0
    journal = Journal(wire.base_url, "synthetic", wire.source)
    assert journal.pending(sid)
    assert journal.get(sid)["source_byte_end"] == 0
    assert not any(key[0] == sid for key in wire.accepted)
    journal.close()


def test_shared_drain_migrates_native_cwd_but_never_guesses_missing_cwd(setup):
    config, storage, wire = setup
    native_sid, native_path = _other_source(config)
    unknown_sid, unknown_path = _other_source(config, cwd=None)
    journal = Journal(wire.base_url, "synthetic", wire.source)
    for sid, path in ((native_sid, native_path), (unknown_sid, unknown_path)):
        journal.ensure(sid, path, wire.receipts(sid), historical=False)
    journal.close()
    assert tapmain._run_durable_loop(config, storage) == 0
    journal = Journal(wire.base_url, "synthetic", wire.source)
    assert journal.get(native_sid)["cwd"] == "/synthetic/other"
    assert journal.get(native_sid)["source_byte_end"] == native_path.stat().st_size
    assert "working directory is unverified" in journal.get(unknown_sid)["error"]
    assert journal.get(unknown_sid)["source_byte_end"] == 0
    assert not any(key[0] == unknown_sid for key in wire.accepted)
    journal.close()


def _own_and_stage_in_child(sid, path, ready, partial=b""):
    wire = Wire()
    journal = Journal(wire.base_url, "synthetic", wire.source)
    assert journal.claim_live(sid)
    journal.ensure(
        sid,
        Path(path),
        wire.receipts(sid),
        historical=False,
        provenance=validate_identity(Path(path), wire.source, sid),
        cwd="/synthetic/other",
    )
    if partial:
        with Path(path).open("ab") as handle:
            handle.write(partial)
            handle.flush()
            os.fsync(handle.fileno())
    journal.stage(sid, cwd="/synthetic/other")
    ready.send(True)
    # The parent kills us with SIGKILL while the live owner lock is held.
    ready.recv()


def test_sigkill_orphan_finalizes_pinned_prefix_and_appended_tail_reopens(setup, monkeypatch):
    config, storage, wire = setup
    sid, path = _other_source(config)
    original_end = path.stat().st_size
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=_own_and_stage_in_child, args=(sid, str(path), child))
    process.start()
    try:
        assert parent.poll(10) and parent.recv()
        journal = Journal(wire.base_url, "synthetic", wire.source)
        pending = journal.pending(sid)
        assert pending
        assert journal.pin_orphan(sid, wire.receipts(sid)) is False
        journal.close()
        os.kill(process.pid, signal.SIGKILL)
        process.join(10)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
        child.close()
    old = time.time() - tapmain.ORPHAN_QUIET_SECONDS - 1
    os.utime(path, (old, old))
    monkeypatch.setattr(tapmain.reconcile, "has_live_daemon", lambda _: False)
    post = wire.post
    appended = False

    def append_after_pinned_finalize(body):
        nonlocal appended
        result = post(body)
        value = json.loads(body)
        if value["session_id"] == sid and value.get("finalize") and not appended:
            assert value["snapshot_byte_end"] == value["source_byte_end"] == original_end
            assert value["snapshot_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
            with path.open("a") as handle:
                handle.write(
                    json.dumps(
                        dict(
                            type="user",
                            sessionId=sid,
                            cwd="/synthetic/other",
                            message=dict(role="user", content="resumed"),
                        )
                    )
                    + "\n"
                )
            appended = True
        return result

    monkeypatch.setattr(wire, "post", append_after_pinned_finalize)
    assert tapmain._run_durable_loop(config, storage) == 0
    assert appended and wire.accepted[(sid, 0)] == pending
    journal = Journal(wire.base_url, "synthetic", wire.source)
    assert journal.get(sid)["finalized"] is False
    assert journal.get(sid)["source_byte_end"] == path.stat().st_size
    journal.close()
    # When the resumed tail is quiet too, it receives a new pinned completion.
    os.utime(path, (old, old))
    tapmain._shutdown_requested = False
    assert tapmain._run_durable_loop(config, storage) == 0
    journal = Journal(wire.base_url, "synthetic", wire.source)
    assert journal.get(sid)["finalized"] is True
    assert journal.pending(sid) is None
    journal.close()
    batches = [json.loads(body) for (session, _), body in wire.accepted.items() if session == sid]
    assert [batch["batch_seq"] for batch in batches] == list(range(len(batches)))
    assert [event["line_no"] for batch in batches for event in batch.get("events", [])] == [0, 1]


def test_live_wrapper_blocks_orphan_completion_even_without_owner_lock(setup, monkeypatch):
    config, storage, wire = setup
    sid, path = _other_source(config)
    journal = Journal(wire.base_url, "synthetic", wire.source)
    journal.ensure(sid, path, wire.receipts(sid), historical=False, cwd="/synthetic/other")
    journal.close()
    old = time.time() - tapmain.ORPHAN_QUIET_SECONDS - 1
    os.utime(path, (old, old))
    monkeypatch.setattr(tapmain.reconcile, "has_live_daemon", lambda _: True)
    assert tapmain._run_durable_loop(config, storage) == 0
    journal = Journal(wire.base_url, "synthetic", wire.source)
    assert journal.get(sid)["source_byte_end"] == path.stat().st_size
    assert not journal.get(sid)["finalized"]
    assert not journal.get(sid).get("snapshot_path")
    journal.close()


def test_sigkill_partial_record_pins_complete_prefix_then_restart_consumes_completed_tail(
    setup, monkeypatch
):
    config, storage, wire = setup
    sid, path = _other_source(config)
    original = path.read_bytes()
    # A partial record larger than the backwards-scanner chunk also checks
    # that source bytes and retained-event ordinals use distinct coordinates.
    second = (
        json.dumps(
            dict(
                type="user",
                sessionId=sid,
                cwd="/synthetic/other",
                message=dict(role="user", content="resumed " + "x" * 70000),
            )
        )
        + "\r\n"
    ).encode()
    partial, remainder = second[:-7], second[-7:]
    context = multiprocessing.get_context("fork")
    parent, child = context.Pipe()
    process = context.Process(target=_own_and_stage_in_child, args=(sid, str(path), child, partial))
    process.start()
    try:
        assert parent.poll(10) and parent.recv()
        journal = Journal(wire.base_url, "synthetic", wire.source)
        pending = journal.pending(sid)
        assert pending
        assert json.loads(pending)["source_byte_end"] == len(original)
        journal.close()
        os.kill(process.pid, signal.SIGKILL)
        process.join(10)
        assert process.exitcode == -signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill()
            process.join(10)
        parent.close()
        child.close()
    old = time.time() - tapmain.ORPHAN_QUIET_SECONDS - 1
    os.utime(path, (old, old))
    monkeypatch.setattr(tapmain.reconcile, "has_live_daemon", lambda _: False)
    assert tapmain._run_durable_loop(config, storage) == 0
    journal = Journal(wire.base_url, "synthetic", wire.source)
    state = journal.get(sid)
    assert state["finalized"] and state["historical_complete"]
    assert state["source_byte_end"] == state["historical_end"] == len(original)
    assert state["source_line_end"] == state["event_end"] == 1
    assert journal.pending(sid) is None
    assert wire.accepted[(sid, 0)] == pending
    finalized = json.loads(wire.accepted[(sid, 1)])
    assert finalized["finalize"]
    assert finalized["snapshot_byte_end"] == len(original)
    assert finalized["snapshot_sha256"] == hashlib.sha256(original).hexdigest()
    assert path.read_bytes() == original + partial
    journal.close()
    # Resume the SAME producer after it completes the interrupted JSON record.
    with path.open("ab") as handle:
        handle.write(remainder)
    resumed = SimpleNamespace(**vars(config))
    resumed.session_id, resumed.transcript_path = sid, path
    resumed.cwd = Path("/synthetic/other")
    resumed.shutdown_sentinel = path.parent / "resumed.shutdown"
    tapmain._shutdown_requested = False
    assert tapmain._run_durable_loop(resumed, storage) == 0
    batches = [json.loads(body) for (session, _), body in wire.accepted.items() if session == sid]
    assert [batch["batch_seq"] for batch in batches] == [0, 1, 2, 3]
    assert [event["line_no"] for batch in batches for event in batch.get("events", [])] == [0, 1]
    assert batches[2]["source_byte_start"] == len(original)
    assert batches[2]["source_byte_end"] == len(original + second)
    assert batches[3]["finalize"] and batches[3]["snapshot_byte_end"] == len(original + second)
    assert batches[3]["snapshot_sha256"] == hashlib.sha256(original + second).hexdigest()
    journal = Journal(wire.base_url, "synthetic", wire.source)
    assert journal.get(sid)["finalized"]
    assert journal.get(sid)["source_line_end"] == journal.get(sid)["event_end"] == 2
    assert journal.pending(sid) is None
    journal.close()


def test_current_daemon_shutdown_certifies_complete_records_before_partial_tail(setup):
    config, storage, wire = setup
    original = config.transcript_path.read_bytes()
    with config.transcript_path.open("ab") as handle:
        handle.write(b'{"type":"user","message":')
    assert tapmain._run_durable_loop(config, storage) == 0
    journal = Journal(wire.base_url, "synthetic", wire.source)
    state = journal.get(config.session_id)
    assert state["finalized"] and state["historical_complete"]
    assert state["source_byte_end"] == len(original)
    assert state["source_line_end"] == state["event_end"] == 3
    assert journal.pending(config.session_id) is None
    final = json.loads(wire.accepted[(config.session_id, 1)])
    assert final["snapshot_byte_end"] == len(original)
    assert final["snapshot_sha256"] == hashlib.sha256(original).hexdigest()
    journal.close()
