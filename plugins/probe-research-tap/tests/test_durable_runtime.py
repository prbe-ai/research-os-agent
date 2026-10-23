"""Exercise the actual v2 daemon path with the shared journal and no network."""

import hashlib
import itertools
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
from tap import session_journal as tapmain_journal
from tap.session_identity import validate_identity
from tap.session_journal import DeliveryPending, Journal
from tap.storage import Storage

# Another session's folder: an absolute path on this system (`/synthetic/...`
# has no drive, so Windows reads it as relative and the tap retains its lines).
OTHER_CWD = "C:\\synthetic\\other" if os.name == "nt" else "/synthetic/other"


class Wire:
    base_url = "http://synthetic.invalid"
    source = "claude_code"
    status = 202
    unsupported = False

    def __init__(self):
        self.accepted = {}

    def receipts(self, sid, **_page):
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


def _hosted(monkeypatch):
    """Run every `_run_durable_loop` below as the Probe service runs it (plan A2,
    D21): on a thread of its own, under the session's environment
    (`tap.threadenv`), with the hosted `stop` and `pause` controls."""
    import threading

    from tap import threadenv

    monkeypatch.setattr(os, "environ", os.environ)  # undone after the test
    original = tapmain._run_durable_loop

    def hosted(config, storage):
        config.stop, config.pause = threading.Event(), threading.Event()
        env = dict(os.environ)
        out = {}
        db = Path(storage._conn.execute("PRAGMA database_list").fetchone()[2])

        def run():
            # The loop's own thread opens its storage, as `tap.main.main` does there.
            own = Storage(db)
            try:
                with threadenv.install().session(env):
                    out["code"] = original(config, own)
            finally:
                own.close()

        thread = threading.Thread(target=run, name="recorder-test")
        thread.start()
        thread.join(60)
        assert not thread.is_alive(), "the hosted loop did not return"
        return out["code"]

    monkeypatch.setattr(tapmain, "_run_durable_loop", hosted)


@pytest.fixture(params=["watch", "hosted"])
def setup(request, tmp_path, monkeypatch):
    if request.param == "hosted":
        _hosted(monkeypatch)
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


def _other_source(config, *, cwd=OTHER_CWD, native=True):
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
        assert journal.get(gap.session_id)["cwd"] == OTHER_CWD
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
    journal.ensure(sid, path, wire.receipts(sid), historical=False, cwd=OTHER_CWD)
    journal.close()
    disabled = tapmain.cfg.disabled_paths_file()
    disabled.parent.mkdir(parents=True, exist_ok=True)
    stage = Journal.stage

    def disable_after_stage(self, session_id, **kwargs):
        result = stage(self, session_id, **kwargs)
        if session_id == sid:
            disabled.write_text(OTHER_CWD + "\n")
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
    assert journal.get(native_sid)["cwd"] == OTHER_CWD
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
        cwd=OTHER_CWD,
    )
    if partial:
        with Path(path).open("ab") as handle:
            handle.write(partial)
            handle.flush()
            os.fsync(handle.fileno())
    journal.stage(sid, cwd=OTHER_CWD)
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
                            cwd=OTHER_CWD,
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
    journal.ensure(sid, path, wire.receipts(sid), historical=False, cwd=OTHER_CWD)
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
                cwd=OTHER_CWD,
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
    resumed.cwd = Path(OTHER_CWD)
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


# ---------------------------------------------------------------------------
# When is a session over? Its owner process says so, never silence.
# ---------------------------------------------------------------------------

Owner = tapmain.session_owner.OwnerState


def _owner_states(monkeypatch, states: dict[str, Owner], default: Owner = Owner.UNKNOWN):
    monkeypatch.setattr(
        tapmain.session_owner, "state", lambda sid: states.get(sid, default)
    )


def _stop_after(monkeypatch, sleeps: int):
    count = 0

    def sleep(_seconds):
        nonlocal count
        count += 1
        if count >= sleeps:
            tapmain._shutdown_requested = True

    monkeypatch.setattr(tapmain.time, "sleep", sleep)


def test_a_gone_owner_finalizes_the_session(setup, monkeypatch):
    """The agent was SIGKILLed: no SessionEnd will come. One tick later the
    daemon finalizes, instead of waiting out a quiet timer."""
    config, storage, wire = setup
    _owner_states(monkeypatch, {config.session_id: Owner.GONE})
    _stop_after(monkeypatch, 50)  # a safety net only: the owner must end it first
    assert tapmain._run_durable_loop(config, storage) == 0
    assert config.shutdown_sentinel.exists()
    batches = [json.loads(body) for body in wire.accepted.values()]
    assert batches[-1]["finalize"] is True
    journal = Journal(wire.base_url, "synthetic", "claude_code")
    assert journal.get(config.session_id)["finalized"] is True
    journal.close()


@pytest.mark.parametrize("owner", [Owner.ALIVE, Owner.UNKNOWN])
def test_a_quiet_session_is_never_ended_for_being_quiet(setup, monkeypatch, owner):
    """THE false ending. Claude Code holds no reader on its transcript, so the
    old rule ended every session paused for ten minutes: one was finalized 29
    times in three days, each ending mined again. Quiet for an hour, no reader
    ever seen, and still live."""
    config, storage, _ = setup
    _owner_states(monkeypatch, {config.session_id: owner})
    monkeypatch.setattr(tapmain, "ORPHAN_CHECK_EVERY_TICKS", 1)
    monkeypatch.setattr(tapmain, "_transcript_has_active_reader", lambda _path: False)
    old = time.time() - 3600
    os.utime(config.transcript_path, (old, old))
    _stop_after(monkeypatch, 5)
    assert tapmain._run_durable_loop(config, storage) == 0
    assert not config.shutdown_sentinel.exists(), "a live session was ended for being quiet"


def test_unknown_owner_still_ends_when_a_seen_reader_disappears(setup, monkeypatch):
    """The one deterministic half of the old lsof check survives for agents
    with no owner record: a reader that WAS there and is now gone."""
    config, storage, _ = setup
    _owner_states(monkeypatch, {})
    monkeypatch.setattr(tapmain, "ORPHAN_CHECK_EVERY_TICKS", 1)
    readers = iter([True, False])
    monkeypatch.setattr(tapmain, "_transcript_has_active_reader", lambda _path: next(readers))
    _stop_after(monkeypatch, 50)
    assert tapmain._run_durable_loop(config, storage) == 0
    assert config.shutdown_sentinel.exists()


@pytest.mark.parametrize(("owner", "pinned"), [(Owner.ALIVE, False), (Owner.GONE, True)])
def test_another_daemon_never_completes_a_session_whose_agent_is_alive(
    setup, monkeypatch, owner, pinned
):
    """A session whose daemon crashed out is still live while its agent runs:
    the next prompt respawns its daemon. Only a gone (or unknown) owner lets a
    sibling daemon complete its quiet prefix."""
    config, storage, wire = setup
    sid, path = _other_source(config)
    journal = Journal(wire.base_url, "synthetic", wire.source)
    journal.ensure(sid, path, wire.receipts(sid), historical=False, cwd=OTHER_CWD)
    journal.close()
    old = time.time() - tapmain.ORPHAN_QUIET_SECONDS - 1
    os.utime(path, (old, old))
    monkeypatch.setattr(tapmain.reconcile, "has_live_daemon", lambda _: False)
    _owner_states(monkeypatch, {sid: owner})
    assert tapmain._run_durable_loop(config, storage) == 0
    journal = Journal(wire.base_url, "synthetic", wire.source)
    assert journal.get(sid)["source_byte_end"] == path.stat().st_size
    assert journal.get(sid)["finalized"] is pinned
    journal.close()


def test_a_batch_older_redaction_let_through_no_longer_strands_its_session(setup, monkeypatch):
    """A pending body that today's scrubber would change was refused by every
    newer daemon, forever. The server never saw it, so the loop rebuilds it
    under current redaction and the session ships again."""
    config, storage, wire = setup
    token = "probe_pat_" + "0123456789abcdef" * 2
    with config.transcript_path.open("a") as handle:
        handle.write(
            json.dumps(
                dict(
                    type="user",
                    sessionId=config.session_id,
                    message=dict(role="user", content=f"token {token}"),
                )
            )
            + "\n"
        )
    journal = Journal(wire.base_url, "synthetic", "claude_code")
    journal.ensure(
        config.session_id, config.transcript_path, wire.receipts(config.session_id),
        historical=False, cwd=str(config.cwd),
    )
    with monkeypatch.context() as patch:
        patch.setattr(tapmain_journal, "_scrub_content", lambda payload: payload)
        assert token.encode() in journal.stage(config.session_id, cwd=str(config.cwd))
    journal.close()
    assert tapmain._run_durable_loop(config, storage) == 0
    assert wire.accepted, "the stranded session shipped nothing"
    assert all(token.encode() not in body for body in wire.accepted.values())
    journal = Journal(wire.base_url, "synthetic", "claude_code")
    assert journal.get(config.session_id)["finalized"] is True
    assert journal.pending(config.session_id) is None
    journal.close()


def test_stopping_drains_only_its_own_session(setup, monkeypatch):
    """SessionEnd waits for this pass. Another session's backlog is not the
    agent's exit and must not hold it for up to 15s."""
    config, storage, wire = setup
    sid, path = _other_source(config)
    journal = Journal(wire.base_url, "synthetic", wire.source)
    journal.ensure(sid, path, wire.receipts(sid), historical=False, cwd=OTHER_CWD)
    journal.close()
    tapmain._shutdown_requested = True
    assert tapmain._run_durable_loop(config, storage) == 0
    assert any(key[0] == config.session_id for key in wire.accepted)
    assert not any(key[0] == sid for key in wire.accepted)


@pytest.mark.parametrize(("owner", "ended"), [(Owner.UNKNOWN, True), (Owner.ALIVE, False)])
def test_an_unknown_owner_ends_after_the_servers_idle_window(setup, monkeypatch, owner, ended):
    """With no owner to watch (pi), a daemon would otherwise outlive a
    hard-killed agent forever. A day of quiet is the server sweep's own window."""
    config, storage, _ = setup
    _owner_states(monkeypatch, {config.session_id: owner})
    old = time.time() - tapmain.UNKNOWN_OWNER_QUIET_SECONDS - 1
    os.utime(config.transcript_path, (old, old))
    _stop_after(monkeypatch, 5)
    assert tapmain._run_durable_loop(config, storage) == 0
    assert config.shutdown_sentinel.exists() is ended


def test_a_live_owner_still_ends_when_its_reader_disappears(setup, monkeypatch):
    """Codex holds its rollout open; a new session in the same process closes
    the old one while the process lives on."""
    config, storage, _ = setup
    _owner_states(monkeypatch, {config.session_id: Owner.ALIVE})
    monkeypatch.setattr(tapmain, "ORPHAN_CHECK_EVERY_TICKS", 1)
    readers = iter([True, False])
    monkeypatch.setattr(tapmain, "_transcript_has_active_reader", lambda _path: next(readers))
    _stop_after(monkeypatch, 50)
    assert tapmain._run_durable_loop(config, storage) == 0
    assert config.shutdown_sentinel.exists()


def test_a_lasting_409_costs_one_receipts_lookup_per_batch(setup, monkeypatch):
    config, storage, wire = setup
    calls = []
    receipts = wire.receipts

    def counting(sid, **page):
        if page:
            calls.append(sid)
        return receipts(sid, **page)

    monkeypatch.setattr(wire, "receipts", counting)
    monkeypatch.setattr(
        wire, "post", lambda body: (409, {"detail": "session capture source is disconnected"})
    )
    _stop_after(monkeypatch, 5)
    assert tapmain._run_durable_loop(config, storage) == 0
    assert calls == [config.session_id]
    journal = Journal(wire.base_url, "synthetic", "claude_code")
    assert "disconnected" in journal.get(config.session_id)["error"]
    journal.close()


# ---------------------------------------------------------------------------
# A 409 reconciliation leaves standing waits, and only its own session waits.
# bucket-robotics, 2026-10-22: six sessions the engine kept refusing were
# re-POSTed (a 1.1 MB body among them) every ~10 s, for good.
# ---------------------------------------------------------------------------

REFUSAL = {"detail": "batch identity already accepted with different content"}


def _clock(monkeypatch, *, ticks: int, step: float = 60, each=None) -> list[float]:
    """The journal's clock, moved `step` seconds by each daemon tick's sleep
    (one sleep per tick at these tests' 1 s cadence: a minute per tick is the
    real active cadence), and a stop after `ticks` sleeps."""
    clock = [1_800_000_000.0]
    monkeypatch.setattr(tapmain_journal, "time", SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(tapmain, "_transcript_has_active_reader", lambda _path: None)
    sleeps = 0

    def sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        clock[0] += step
        if each is not None:
            each(clock[0])
        if sleeps >= ticks:
            tapmain._shutdown_requested = True

    monkeypatch.setattr(tapmain.time, "sleep", sleep)
    return clock


def _refuse(monkeypatch, wire, sid: str, clock: list[float]) -> list[tuple[str, float]]:
    """The engine answers 409 to every batch of `sid`; other sessions' go through.
    Returns every POST as (session, clock)."""
    posts = []
    accept = wire.post

    def post(body):
        session = json.loads(body)["session_id"]
        posts.append((session, clock[0]))
        return (409, REFUSAL) if session == sid else accept(body)

    monkeypatch.setattr(wire, "post", post)
    return posts


def test_a_lasting_409_is_sent_again_on_a_backoff_while_other_sessions_flow(setup, monkeypatch):
    """Six hours of minute ticks: the refused batch goes 11 times instead of 361,
    a minute after the first refusal, then two, four ... and hourly from then
    on. The other session's new lines go on the very tick after each is
    written, while the refused one waits."""
    config, storage, wire = setup
    other, other_path = _other_source(config)
    journal = Journal(wire.base_url, "synthetic", wire.source)
    journal.ensure(
        other, other_path, wire.receipts(other), historical=False, cwd=OTHER_CWD
    )
    journal.close()
    _owner_states(monkeypatch, {}, default=Owner.ALIVE)
    monkeypatch.setattr(tapmain_journal, "CONFLICT_RETRY_JITTER", 0.0)
    written = []

    def write_other(now):
        if len(written) < 7 and round(now - start) % (45 * 60) == 0:
            event = dict(
                type="user",
                sessionId=other,
                cwd=OTHER_CWD,
                message=dict(role="user", content=f"later {len(written)}"),
            )
            with other_path.open("a") as handle:
                handle.write(json.dumps(event) + "\n")
            written.append(now)

    clock = _clock(monkeypatch, ticks=6 * 60, each=write_other)
    start = clock[0]
    posts = _refuse(monkeypatch, wire, config.session_id, clock)
    assert tapmain._run_durable_loop(config, storage) == 0

    refused = [at for sid, at in posts if sid == config.session_id]
    gaps = [round(later - earlier) for earlier, later in itertools.pairwise(refused)]
    assert gaps[:6] == [60, 120, 240, 480, 960, 1920]
    assert gaps[6:] and set(gaps[6:]) == {tapmain_journal.CONFLICT_RETRY_MAX_SECONDS}
    assert len(refused) == 11  # 361 passes; every pass re-sent it before

    sent_other = {at for sid, at in posts if sid == other}
    assert len(written) == 7 and set(written) <= sent_other
    journal = Journal(wire.base_url, "synthetic", wire.source)
    assert journal.get(other)["source_byte_end"] == other_path.stat().st_size
    assert journal.get(config.session_id)["conflict_wait"]["refusals"] == 11
    journal.close()


def test_the_wait_outlives_a_restart_and_ends_once_the_batch_is_accepted(setup, monkeypatch):
    """Kept in the journal, not the process: a daemon (or Probe service
    recorder) started during the wait does not send the batch either. Once the
    server takes it, the wait is gone."""
    config, storage, wire = setup
    clock = _clock(monkeypatch, ticks=1, step=0)
    posts = _refuse(monkeypatch, wire, config.session_id, clock)

    def run():
        tapmain._shutdown_requested = False
        assert tapmain._run_durable_loop(config, storage) == 0
        journal = Journal(wire.base_url, "synthetic", wire.source)
        try:
            return journal.get(config.session_id).get("conflict_wait"), journal.conflict_retry_in(
                config.session_id
            )
        finally:
            journal.close()

    wait, left = run()  # refused, reconciled, still refused: it waits
    assert len(posts) == 1 and wait["refusals"] == 1
    assert 0.75 * 60 <= left <= 60
    clock[0] += left - 1
    wait, left = run()  # a new process, inside the wait: nothing sent
    assert len(posts) == 1 and wait["refusals"] == 1 and left == pytest.approx(1, abs=1e-3)
    clock[0] += 2
    wait, left = run()  # past it: sent once, refused again, waits longer
    assert len(posts) == 2 and wait["refusals"] == 2
    assert 0.75 * 120 <= left <= 120
    monkeypatch.delattr(wire, "post")  # the server takes it now
    clock[0] += left
    wait, left = run()
    assert wait is None and left == 0
    journal = Journal(wire.base_url, "synthetic", wire.source)
    assert journal.get(config.session_id)["last_seq"] >= 0
    journal.close()


def test_a_cooling_rebase_is_asked_again_after_each_wait(setup, monkeypatch):
    """A batch refused again soon after its journal continued the server's
    stream waits out the 409 (`RebaseCooling`), and this same daemon asks
    `reconcile_pending` again after each wait: the racing producer may be
    gone by then. Before, the first answer stood for the process's life."""
    config, storage, wire = setup
    monkeypatch.setattr(tapmain_journal, "CONFLICT_RETRY_JITTER", 0.0)
    clock = _clock(monkeypatch, ticks=30)
    posts = _refuse(monkeypatch, wire, config.session_id, clock)
    asked = []

    def cooling(self, sid, _wire):
        asked.append(clock[0])
        raise tapmain_journal.RebaseCooling(
            "racing",
            status_code=409,
            body_sha256=hashlib.sha256(self.pending(sid)).hexdigest(),
        )

    monkeypatch.setattr(tapmain_journal.Journal, "reconcile_pending", cooling)
    assert tapmain._run_durable_loop(config, storage) == 0
    refused = [at for sid, at in posts if sid == config.session_id]
    assert len(refused) >= 4 and asked == refused  # one ask per refusal, each after a wait
    gaps = [round(later - earlier) for earlier, later in itertools.pairwise(refused)]
    assert gaps[:3] == [60, 120, 240]


@pytest.mark.parametrize("status", [422, 503])
def test_other_refusals_keep_their_cadence_and_record_no_wait(setup, monkeypatch, status):
    """Only a 409 that reconciliation leaves standing waits. A 422 or a 5xx
    keeps the behaviour it had: tried again on the next tick."""
    config, storage, wire = setup
    posts = []
    monkeypatch.setattr(
        wire, "post", lambda body: (posts.append(body), (status, {"detail": "not now"}))[1]
    )
    _stop_after(monkeypatch, 5)
    assert tapmain._run_durable_loop(config, storage) == 0
    assert len(posts) >= 5  # one per tick, as before
    journal = Journal(wire.base_url, "synthetic", wire.source)
    assert "conflict_wait" not in journal.get(config.session_id)
    assert journal.conflict_retry_in(config.session_id) == 0
    journal.close()


# -- the daemon worker, handed to a service that supervises it (plan Q3) ---------


class _Link:
    """The service's `probe.service.workers.Link`, as the recorder's controls carry it."""

    def __init__(self):
        self.specs = []
        self.released = 0

    def supervise(self, spec):
        self.specs.append(spec)
        return True

    def release(self):
        self.released += 1


def _daemon_switch(monkeypatch, tmp_path, sid):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    state = tmp_path / "xdg" / "probe" / "sessions" / f"{sid}.state"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("daemon")


def test_the_worker_is_handed_over_once_the_recorder_owns_the_session(setup, monkeypatch, tmp_path):
    config, storage, _ = setup
    _daemon_switch(monkeypatch, tmp_path, config.session_id)
    config.workers = link = _Link()
    spawned = []
    monkeypatch.setattr(tapmain.Supervisor, "_spawn_argv", lambda self: spawned.append(1))
    assert tapmain._run_durable_loop(config, storage) == 0
    assert len(link.specs) == 1 and link.specs[0]["sid"] == config.session_id
    assert link.released == 1, "its loop's end releases it"
    assert spawned == [], "the tap spawned nothing itself"


def test_a_recorder_that_steps_back_for_another_hands_nothing_over(setup, monkeypatch, tmp_path):
    """Another recorder holds the session's live claim (D39): this one leaves
    at once and must start, or hand over, nothing -- each of its retries would
    otherwise churn the worker."""
    config, storage, _ = setup
    _daemon_switch(monkeypatch, tmp_path, config.session_id)
    config.workers = link = _Link()
    monkeypatch.setattr(Journal, "claim_live", lambda self, sid: False)
    assert tapmain._run_durable_loop(config, storage) == 0
    assert link.specs == [] and link.released == 0


def test_with_ingestion_off_the_worker_is_still_handed_over_a_pass_later(setup, monkeypatch, tmp_path):
    """No live claim comes while ingestion is off, but the recorder stays: its
    worker is handed over, as `tap watch` starts one."""
    config, storage, _ = setup
    _daemon_switch(monkeypatch, tmp_path, config.session_id)
    config.workers = link = _Link()
    monkeypatch.setattr(tapmain.killswitch, "is_ingestion_enabled", lambda **kw: (False, "off"))
    _stop_after(monkeypatch, 2)
    assert tapmain._run_durable_loop(config, storage) == 0
    assert len(link.specs) == 1


def test_a_hosted_recorder_publishes_the_journal_it_holds_the_claim_in(setup):
    """The Probe service reads a session's receipts from the journal its own
    recorder holds the live claim in, as published here (`WatchConfig.claimed`,
    `hosted.Controls.claim`): set once the claim is taken, cleared when the
    loop lets go of it. No lock is probed to find it."""
    config, storage, wire = setup
    published = []
    config.claimed = published.append
    assert tapmain._run_durable_loop(config, storage) == 0
    journal = Journal(wire.base_url, "synthetic", "claude_code")
    assert published == [str(journal.directory), None]
    journal.close()


def test_hosted_controls_hold_what_the_recorder_published():
    from tap import hosted

    controls = hosted.Controls()
    assert controls.claimed is None
    controls.claim("/journal/ns")
    assert controls.claimed == "/journal/ns"
    controls.claim(None)
    assert controls.claimed is None


@pytest.mark.parametrize("orphan", [False, True], ids=["live", "orphan"])
def test_a_recovered_reservation_whose_file_is_another_sessions_sends_nothing(setup, monkeypatch, orphan):
    """Codex on 33d27365e: another session's journal row with an empty
    acknowledged prefix (a reservation nothing was sent of yet) whose file now
    holds a different session's transcript. Recovering it (the shared drain,
    or pinning it as an orphan) skipped the native identity check, which only
    the recorder's own session got: it uploaded that transcript under the
    reserved id, and finalized it. Each recovered session's source proves it
    is that session's before anything of it is pinned, staged or sent."""
    config, storage, wire = setup
    sid, path = _other_source(config)
    path.write_text("")  # reserved before its first line
    journal = Journal(wire.base_url, "synthetic", wire.source)
    journal.ensure(sid, path, wire.receipts(sid), historical=False, cwd=OTHER_CWD)
    journal.close()
    foreign = str(uuid4())
    path.write_text("".join(json.dumps(dict(type="user", sessionId=foreign, cwd=OTHER_CWD,
                                            message=dict(role="user", content=f"not {sid} {i}"))) + "\n"
                            for i in range(2)))
    if orphan:
        old = time.time() - tapmain.ORPHAN_QUIET_SECONDS - 1
        os.utime(path, (old, old))
        monkeypatch.setattr(tapmain.reconcile, "has_live_daemon", lambda _: False)
    assert tapmain._run_durable_loop(config, storage) == 0
    assert not any(key[0] == sid for key in wire.accepted), "nothing of it left the machine"
    assert any(key[0] == config.session_id for key in wire.accepted), "the recorder's own session still goes"
    journal = Journal(wire.base_url, "synthetic", wire.source)
    try:
        state = journal.get(sid)
        assert state["source_byte_end"] == 0 and not state["finalized"] and journal.pending(sid) is None
        assert not state.get("snapshot_path"), "not pinned either"
    finally:
        journal.close()
