"""A pending batch that today's redaction refuses must not strand its session.

Pending bytes are immutable because the server may already hold them, and
every newer daemon refuses to replay a body its own scrubber would change. So
a batch staged under older rules sat in the journal forever and its session
stopped shipping: 16 of 20 pending bodies on one machine. `reconcile_pending`
asks the server about that one batch_seq and either adopts its receipt or,
when nothing accepted it, rebuilds it under current redaction. And when the
server's stream no longer lines up with the journal at all, it drops the batch
and continues that stream, once its bytes are proven to be the file's own.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tap import session_journal as sj
from tap.session_journal import DeliveryPending, Journal, ReconciliationRequired, UnsafePending

TOKEN = "probe_pat_" + "0123456789abcdef" * 2
_CURSORS = ("batch_seq", "source_byte_end", "source_line_end", "event_end", "prefix_sha256")


class Server:
    """Protocol 2 as `kb/session_receipts.py` serves it: receipts per batch_seq."""

    base_url = "http://synthetic.invalid"
    source = "claude_code"

    def __init__(self):
        self.stream = None
        self.receipts_by_seq: dict[int, dict] = {}
        self.posts: list[bytes] = []

    def accept(self, body: bytes) -> dict:
        data = json.loads(body)
        receipt = {k: data[k] for k in _CURSORS}
        receipt.update(
            body_sha256=hashlib.sha256(body).hexdigest(), finalized=bool(data.get("finalize"))
        )
        self.receipts_by_seq[data["batch_seq"]] = receipt
        self.stream = {
            "stream_id": data["stream_id"],
            "last_seq": data["batch_seq"],
            "source_byte_end": data["source_byte_end"],
            "source_line_end": data["source_line_end"],
            "event_end": data["event_end"],
            "prefix_sha256": data["prefix_sha256"],
            "finalized": bool(data.get("finalize")),
            # Each accepted batch sets the stream's pin, as the engine does.
            "snapshot_byte_end": data.get("snapshot_byte_end"),
            "snapshot_sha256": data.get("snapshot_sha256"),
        }
        return receipt

    def receipts(self, sid, *, after=-1, limit=None):
        body = dict(protocol_version=2, customer_id="synthetic", source=self.source, session_id=sid)
        if self.stream is None:
            return body | {"state": "absent", "receipts": []}
        rows = [r for seq, r in sorted(self.receipts_by_seq.items()) if seq > after]
        return body | {
            "state": "ready",
            "stream": dict(self.stream),
            "receipts": rows[:limit] if limit else rows,
        }

    def post(self, body: bytes):
        self.posts.append(body)
        data = json.loads(body)
        seq = data["batch_seq"]
        if self.stream is not None and data["stream_id"] != self.stream["stream_id"]:
            return 409, {"detail": "session owned by another stream; reconcile receipts first"}
        previous = self.receipts_by_seq.get(seq)
        if previous is not None:
            if previous["body_sha256"] != hashlib.sha256(body).hexdigest():
                return 409, {"detail": "batch identity already accepted with different content"}
            return 202, {"protocol_version": 2, "status": "duplicate", "receipt": previous}
        if self.stream is None and seq != 0:
            return 409, {"detail": "unknown stream; first sequence must be zero"}
        if self.stream is not None and (
            seq != self.stream["last_seq"] + 1
            or any(
                data[f"{unit}_start"] != self.stream[f"{unit}_end"]
                for unit in ("source_byte", "source_line", "event")
            )
        ):
            return 409, {"detail": "batch does not continue the acknowledged source/event cursor"}
        return 202, {"protocol_version": 2, "status": "accepted", "receipt": self.accept(body)}


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_TRANSCRIPT_STATE_DIR", str(tmp_path / "v2"))
    sid = str(uuid4())
    path = tmp_path / f"{sid}.jsonl"
    lines = [
        dict(type="user", sessionId=sid, cwd="/work", message=dict(role="user", content="hi")),
        dict(
            type="user",
            sessionId=sid,
            cwd="/work",
            message=dict(role="user", content=f"my token is {TOKEN}"),
        ),
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    server = Server()
    journal = Journal(server.base_url, "synthetic", server.source)
    journal.ensure(sid, path, server.receipts(sid), historical=False, cwd="/work")
    yield sid, journal, server, monkeypatch
    journal.close()


def _stage_under_old_rules(sid, journal, monkeypatch) -> bytes:
    """Stage the way a daemon whose scrubber missed TOKEN did."""
    with monkeypatch.context() as patch:
        patch.setattr(sj, "_scrub_content", lambda payload: payload)
        body = journal.stage(sid, cwd="/work")
    assert TOKEN.encode() in body
    with pytest.raises(UnsafePending):
        journal.stage(sid, cwd="/work")
    return body


_RANGE = (
    "batch_seq",
    "source_byte_start",
    "source_byte_end",
    "source_line_end",
    "event_end",
    "prefix_sha256",
)


def test_a_batch_the_server_never_saw_is_re_redacted_in_place(session):
    """Same reservation, same batch_seq and cursors: only the content changes."""
    sid, journal, server, monkeypatch = session
    old = json.loads(_stage_under_old_rules(sid, journal, monkeypatch))
    assert "re-redacted batch 0 in place" in journal.reconcile_pending(sid, server)
    body = journal.stage(sid, cwd="/work")
    new = json.loads(body)
    assert {k: new[k] for k in _RANGE} == {k: old[k] for k in _RANGE}
    assert TOKEN.encode() not in body and b"<redacted:probe-token>" in body
    assert journal.deliver(sid, server)
    assert journal.get(sid)["last_seq"] == 0 and journal.pending(sid) is None
    assert all(TOKEN.encode() not in sent for sent in server.posts)


def test_an_older_daemon_delivering_the_original_meanwhile_is_adopted(session):
    """The race a drop-and-rebuild lost: an older daemon on the same machine
    still sends the original while the transcript keeps growing. Re-redacting
    in place keeps the range, so whichever copy lands, the other is adopted."""
    sid, journal, server, monkeypatch = session
    original = _stage_under_old_rules(sid, journal, monkeypatch)
    path = Path(journal.get(sid)["path"])
    with path.open("a") as handle:
        handle.write(
            json.dumps(
                dict(
                    type="user",
                    sessionId=sid,
                    cwd="/work",
                    message=dict(role="user", content="later"),
                )
            )
            + "\n"
        )
    journal.reconcile_pending(sid, server)
    server.accept(original)  # the older daemon's copy lands first
    with pytest.raises(ReconciliationRequired) as refused:
        journal.deliver(sid, server)
    assert refused.value.status_code == 409 and sj.recoverable(refused.value)
    assert "adopted" in journal.reconcile_pending(sid, server)
    assert journal.get(sid)["last_seq"] == 0 and journal.pending(sid) is None
    body = journal.stage(sid, cwd="/work")
    assert json.loads(body)["batch_seq"] == 1 and b"later" in body
    assert journal.deliver(sid, server)


def test_a_batch_the_server_already_accepted_is_adopted_not_resent(session):
    """A lost response: the server holds this exact batch. Adopt its receipt
    and move on, without replaying a credential it already redacted."""
    sid, journal, server, monkeypatch = session
    body = _stage_under_old_rules(sid, journal, monkeypatch)
    server.accept(body)
    assert "adopted" in journal.reconcile_pending(sid, server)
    state = journal.get(sid)
    assert state["last_seq"] == 0 and journal.pending(sid) is None
    assert state["source_byte_end"] == json.loads(body)["source_byte_end"]
    assert server.posts == []


def test_a_receipt_for_the_same_range_under_another_scrub_is_adopted(session):
    """409 "already accepted with different content": another producer shipped
    the same source range scrubbed differently. Same cursors, same range."""
    sid, journal, server, monkeypatch = session
    old = _stage_under_old_rules(sid, journal, monkeypatch)
    journal.reconcile_pending(sid, server)
    journal.stage(sid, cwd="/work")
    server.accept(old)  # an old producer delivered its copy in between
    with pytest.raises(ReconciliationRequired) as refused:
        journal.deliver(sid, server)
    assert sj.recoverable(refused.value)
    assert "adopted" in journal.reconcile_pending(sid, server)
    assert journal.get(sid)["last_seq"] == 0 and journal.pending(sid) is None


def test_a_receipt_for_a_different_range_is_never_adopted(session):
    """Not as this batch's acknowledgment: the journal goes on from where the
    server's stream ends, never from the receipt's cursors."""
    sid, journal, server, monkeypatch = session
    body = _stage_under_old_rules(sid, journal, monkeypatch)
    receipt = server.accept(body)
    receipt["source_byte_end"] += 1
    assert "continuing the server's stream" in journal.reconcile_pending(sid, server)
    state = journal.get(sid)
    assert journal.pending(sid) is None and state["last_seq"] == 0
    assert state["source_byte_end"] == server.stream["source_byte_end"] != receipt["source_byte_end"]


def _write(path: Path, sid: str, content: str) -> None:
    line = dict(type="user", sessionId=sid, cwd="/work", message=dict(role="user", content=content))
    with path.open("a") as handle:
        handle.write(json.dumps(line) + "\n")


@pytest.fixture
def other(tmp_path):
    """A second journal for the same session: another tap on this machine
    that kept its own state (the 2026-10-17..19 race on one laptop)."""
    server_url, source = Server.base_url, Server.source
    journal = Journal(server_url, "synthetic", source, directory=tmp_path / "other")
    yield journal
    journal.close()


def test_a_batch_refused_for_another_stream_continues_that_stream(session, other):
    """409 "session owned by another stream": another journal started the
    session first. Go on from the end of ITS stream, keeping what this machine
    decided about the session (its title, an asked-for finalize)."""
    sid, journal, server, _ = session
    path = Path(journal.get(sid)["path"])
    other.ensure(sid, path, server.receipts(sid), historical=False, cwd="/work")
    assert other.stage(sid, cwd="/work") and other.deliver(sid, server)
    theirs = server.stream["stream_id"]
    assert theirs != journal.get(sid)["stream_id"]
    _write(path, sid, "later")
    journal.update(sid, custom_title="tokenizer sweep", finalize_requested=True)
    refused_body = journal.stage(sid, cwd="/work")
    with pytest.raises(ReconciliationRequired) as refused:
        journal.deliver(sid, server)
    assert refused.value.status_code == 409 and sj.recoverable(refused.value)
    outcome = journal.reconcile_pending(sid, server)
    assert outcome == "dropped pending batch 0; continuing the server's stream from batch 1"
    state = journal.get(sid)
    assert state["stream_id"] == theirs and state["last_seq"] == 0
    assert state["custom_title"] == "tokenizer sweep" and state["finalize_requested"] is True
    assert journal.pending(sid) is None
    body = journal.stage(sid, cwd="/work", finalize=True)
    assert json.loads(body)["batch_seq"] == 1 and b"later" in body and b"later" in refused_body
    assert journal.deliver(sid, server)
    assert server.stream["source_byte_end"] == path.stat().st_size
    assert sorted(server.receipts_by_seq) == [0, 1]


def test_a_batch_refused_for_another_range_continues_the_stream(session, other):
    """409 "batch identity already accepted with different content", for a
    batch the server holds over ANOTHER range of the same stream: the receipt
    cannot be adopted, so its stream is continued from where it ends."""
    sid, journal, server, _ = session
    path = Path(journal.get(sid)["path"])
    assert journal.stage(sid, cwd="/work") and journal.deliver(sid, server)
    _write(path, sid, "third")
    journal.stage(sid, cwd="/work")  # our batch 1: the third line
    other.ensure(sid, path, server.receipts(sid), historical=False, cwd="/work")
    _write(path, sid, "fourth")
    assert other.stage(sid, cwd="/work") and other.deliver(sid, server)  # theirs: third + fourth
    with pytest.raises(ReconciliationRequired, match="different content"):
        journal.deliver(sid, server)
    assert "from batch 2" in journal.reconcile_pending(sid, server)
    state = journal.get(sid)
    assert state["last_seq"] == 1 and state["source_byte_end"] == path.stat().st_size
    assert journal.stage(sid, cwd="/work") is None  # the server holds every byte
    _write(path, sid, "fifth")
    assert json.loads(journal.stage(sid, cwd="/work"))["batch_seq"] == 2
    assert journal.deliver(sid, server)


def test_a_stream_over_another_source_is_never_continued(session, other, tmp_path):
    """The server's bytes are not this file's first bytes: nothing proves its
    stream is this transcript, so the batch is kept and waits out its 409."""
    sid, journal, server, _ = session
    elsewhere = tmp_path / "elsewhere.jsonl"
    _write(elsewhere, sid, "a different first line")
    other.ensure(sid, elsewhere, server.receipts(sid), historical=False, cwd="/work")
    assert other.stage(sid, cwd="/work") and other.deliver(sid, server)
    body = journal.stage(sid, cwd="/work")
    before = journal.get(sid)
    with pytest.raises(ReconciliationRequired, match="another stream"):
        journal.deliver(sid, server)
    with pytest.raises(ReconciliationRequired, match="more than this source|another source prefix") as kept:
        journal.reconcile_pending(sid, server)
    # A 409 the batch waits out (`defer_conflict`), never a retry every tick.
    assert kept.value.status_code == 409 and kept.value.body_sha256
    assert journal.pending(sid) == body
    assert {k: v for k, v in journal.get(sid).items() if k != "error"} == {
        k: v for k, v in before.items() if k != "error"
    }


def test_a_rebuild_that_refuses_leaves_the_journal_as_it_was(session, other, monkeypatch):
    sid, journal, server, _ = session
    path = Path(journal.get(sid)["path"])
    other.ensure(sid, path, server.receipts(sid), historical=False, cwd="/work")
    assert other.stage(sid, cwd="/work") and other.deliver(sid, server)
    body = journal.stage(sid, cwd="/work")
    with pytest.raises(ReconciliationRequired):
        journal.deliver(sid, server)
    before = journal.get(sid)

    def refuse(*_args, **_kwargs):
        raise DeliveryPending("not enough free disk space for the transcript snapshot")

    monkeypatch.setattr(sj, "_record_terms", refuse)
    with pytest.raises(DeliveryPending):
        journal.reconcile_pending(sid, server)
    assert journal.pending(sid) == body and journal.get(sid) == before


def test_a_clean_batch_with_no_receipt_is_left_alone(session):
    """Nothing to settle: the caller keeps its own, more specific error."""
    sid, journal, server, _ = session
    body = journal.stage(sid, cwd="/work")
    assert journal.reconcile_pending(sid, server) is None
    assert journal.pending(sid) == body


def test_redaction_that_never_settles_is_refused_not_looped(session):
    sid, journal, server, monkeypatch = session
    body = _stage_under_old_rules(sid, journal, monkeypatch)
    monkeypatch.setattr(
        sj, "_scrub_content", lambda payload: payload | {"cwd": payload.get("cwd", "") + "x"}
    )
    with pytest.raises(ReconciliationRequired, match="does not settle"):
        journal.reconcile_pending(sid, server)
    assert journal.pending(sid) == body


def test_a_staged_body_always_passes_its_own_safety_check():
    """One pass used not to be stable: `<redacted:probe-token>` contains the
    word "token" and anchored the next value, so a daemon could refuse a body
    it had just staged. Markers no longer anchor anything."""
    uuid = "8a3c1f5e-2b7d-4e90-9c61-0f4d2a7b5e13"
    config = (
        "{'contexts': {'default': {'token': '" + TOKEN + "', 'workspace': {'id': '" + uuid + "'}}}}"
    )
    payload = {"events": [{"line_no": 0, "raw": {"stdout": config}}], "cwd": "/work"}
    once, _ = sj.redact_event({"events": payload["events"], "cwd": "/work"})
    assert sj.redact_event(once)[0] == once
    body = sj.canonical_payload(sj._scrub_content(payload))
    sj._require_safe_pending(body)
    assert TOKEN.encode() not in body and uuid.encode() in body


def test_a_line_the_sanitizer_cannot_handle_costs_that_line_not_the_session(session):
    sid, journal, _server, monkeypatch = session
    real = sj._sanitizer("claude_code")

    def trips_on_the_token(event):
        if TOKEN in json.dumps(event):
            raise TypeError("a shape this sanitizer never saw")
        return real(event)

    monkeypatch.setattr(sj, "_sanitizer", lambda source: trips_on_the_token)
    body = json.loads(journal.stage(sid, cwd="/work"))
    assert body["source_line_end"] == 2, "the cursor moves past the line"
    assert [e["raw"]["message"]["content"] for e in body["events"]] == ["hi"]


def test_a_line_nested_past_the_redactors_reach_is_dropped_not_fatal(tmp_path, monkeypatch):
    # The journal's own guard, whatever a sanitizer lets through: an identity
    # sanitizer hands the redactor the deep value as is.
    monkeypatch.setattr(sj, "_sanitizer", lambda source: lambda event: event)
    monkeypatch.setenv("PROBE_TRANSCRIPT_STATE_DIR", str(tmp_path / "v2"))
    sid = str(uuid4())
    # Past the 64-level cap, well inside every Python's own json/recursion
    # limits (3.11 cannot even encode ~1000 levels to build the fixture).
    deep: object = "x"
    for _ in range(100):
        deep = [deep]
    lines = [
        dict(type="user", sessionId=sid, cwd="/work", message=dict(role="user", content="first")),
        dict(type="user", sessionId=sid, cwd="/work", uuid=deep,
             message=dict(role="user", content="deep")),
        dict(type="user", sessionId=sid, cwd="/work", message=dict(role="user", content="after")),
    ]
    path = tmp_path / f"{sid}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))
    server = Server()
    journal = Journal(server.base_url, "synthetic", server.source)
    try:
        journal.ensure(sid, path, server.receipts(sid), historical=False, cwd="/work")
        body = json.loads(journal.stage(sid, cwd="/work"))
    finally:
        journal.close()
    assert body["source_line_end"] == 3
    assert [e["raw"]["message"]["content"] for e in body["events"]] == ["first", "after"]


# ---------------------------------------------------------------------------
# A 409 that reconciliation leaves standing: how long the batch waits.
# ---------------------------------------------------------------------------


def _journal_clock(monkeypatch) -> list[float]:
    clock = [1_800_000_000.0]
    monkeypatch.setattr(sj, "time", SimpleNamespace(time=lambda: clock[0]))
    return clock


def test_a_lasting_conflict_waits_longer_each_time_for_that_body_only(session):
    """A minute, doubling to an hour; a second drainer deferring the same
    refusal does not double it again. A new body (here the same batch
    re-redacted) starts over, and acceptance ends the wait."""
    sid, journal, server, monkeypatch = session
    monkeypatch.setattr(sj, "CONFLICT_RETRY_JITTER", 0.0)
    clock = _journal_clock(monkeypatch)
    assert journal.defer_conflict(sid) == 0  # nothing pending: nothing to hold back
    _stage_under_old_rules(sid, journal, monkeypatch)
    assert journal.conflict_retry_in(sid) == 0
    waits = []
    for _ in range(8):
        waits.append(journal.defer_conflict(sid))
        assert journal.defer_conflict(sid) == waits[-1]  # another drainer, same refusal
        assert journal.conflict_retry_in(sid) == waits[-1]
        clock[0] += waits[-1]
        assert journal.conflict_retry_in(sid) == 0
    assert waits == [60, 120, 240, 480, 960, 1920, 3600, 3600]
    assert journal.get(sid)["conflict_wait"]["refusals"] == 8

    journal.defer_conflict(sid)
    assert "re-redacted" in journal.reconcile_pending(sid, server)
    assert journal.conflict_retry_in(sid) == 0  # another body: its own clock
    assert journal.defer_conflict(sid) == 60
    clock[0] += 60
    assert journal.deliver(sid, server)
    assert "conflict_wait" not in journal.get(sid)


def test_a_refusal_of_a_batch_another_drainer_settled_puts_no_wait_on_the_next(session):
    """Between this drainer's 409 and its deferral, another may have settled
    the refused body and staged the next one: that one has earned no wait."""
    sid, journal, _server, _monkeypatch = session
    body = journal.stage(sid, cwd="/work")
    assert journal.defer_conflict(sid, "0" * 64) == 0
    assert "conflict_wait" not in journal.get(sid)
    assert journal.defer_conflict(sid, hashlib.sha256(body).hexdigest()) > 0


def test_a_clock_set_back_does_not_strand_the_batch_behind_its_wait(session):
    """No wait is longer than the cap, so one further away than that was
    recorded under a clock that has since been set back: it is over."""
    sid, journal, _server, monkeypatch = session
    clock = _journal_clock(monkeypatch)
    journal.stage(sid, cwd="/work")
    journal.defer_conflict(sid)
    clock[0] -= 86400
    assert journal.conflict_retry_in(sid) == 0
    assert 0 < journal.defer_conflict(sid) <= sj.CONFLICT_RETRY_MAX_SECONDS
    assert 0 < journal.conflict_retry_in(sid) <= sj.CONFLICT_RETRY_MAX_SECONDS


def test_each_wait_is_jittered_below_its_step_and_never_past_the_cap():
    for refusals in range(-1, 40):
        step = min(
            sj.CONFLICT_RETRY_MAX_SECONDS,
            sj.CONFLICT_RETRY_MIN_SECONDS * 2 ** min(max(refusals, 1) - 1, 16),
        )
        for _ in range(50):
            wait = sj.conflict_retry_seconds(refusals)
            assert (1 - sj.CONFLICT_RETRY_JITTER) * step <= wait <= step
    assert len({sj.conflict_retry_seconds(5) for _ in range(50)}) > 1


def _raced(journal, other, server, sid) -> str:
    """Another journal starts the session's stream first; ours staged a batch
    on its own stream. Returns the other stream's id."""
    path = Path(journal.get(sid)["path"])
    other.ensure(sid, path, server.receipts(sid), historical=False, cwd="/work")
    assert other.stage(sid, cwd="/work") and other.deliver(sid, server)
    _write(path, sid, "later")
    journal.stage(sid, cwd="/work")
    return server.stream["stream_id"]


def test_an_ended_session_another_stream_holds_is_continued_by_its_orphan_pass(session, other):
    """An ended session's journal meets the other stream in `ensure` (the
    orphan pass, or a stopping pass), before any batch is posted: no 409 ever
    comes back to reconcile, so `ensure` continues the stream itself."""
    sid, journal, server, _ = session
    theirs = _raced(journal, other, server, sid)
    assert journal.pin_orphan(sid, server.receipts(sid))
    state = journal.get(sid)
    assert state["stream_id"] == theirs and state["live_enabled"] is True
    assert journal.pending(sid) is None and state["snapshot_path"]
    body = journal.stage(sid, cwd="/work", finalize=True)
    assert json.loads(body)["batch_seq"] == 1 and b"later" in body
    assert journal.deliver(sid, server)
    final = journal.stage(sid, cwd="/work", finalize=True)
    assert json.loads(final)["finalize"] and journal.deliver(sid, server)
    assert server.stream["finalized"] and server.stream["source_byte_end"] == Path(state["path"]).stat().st_size


@pytest.mark.parametrize("copy_lost", [False, True])
def test_a_rebuilt_history_freezes_complete_lines_only(session, other, tmp_path, copy_lost):
    """An imported session's frozen history is kept when it still covers the
    server's stream; a lost copy is frozen again, complete lines only, so a
    line still being written never stalls the session."""
    _sid, journal, server, _ = session
    sid = str(uuid4())
    path = tmp_path / f"{sid}.jsonl"
    _write(path, sid, "imported turn")
    journal.ensure(sid, path, server.receipts(sid), historical=True, cwd="/work")
    other.ensure(sid, path, server.receipts(sid), historical=False, cwd="/work")
    assert other.stage(sid, cwd="/work") and other.deliver(sid, server)
    body = journal.stage(sid, cwd="/work")
    with path.open("a") as handle:
        handle.write('{"type": "user", "sessionId": "' + sid + '", "mess')  # half a line
    if copy_lost:
        Path(journal.get(sid)["snapshot_path"]).unlink()
    with pytest.raises(ReconciliationRequired, match="another stream"):
        journal.deliver(sid, server)
    assert "continuing the server's stream" in journal.reconcile_pending(sid, server)
    state = journal.get(sid)
    assert journal.pending(sid) is None and body
    assert state["historical_end"] == server.stream["source_byte_end"]  # the half line is not history
    # The server holds all of the history: only its finalize is left to send.
    final = journal.stage(sid, cwd="/work")
    assert json.loads(final)["finalize"] and b"mess" not in final
    assert json.loads(final)["source_byte_start"] == server.stream["source_byte_end"]
    assert journal.deliver(sid, server)


def test_a_session_refused_again_soon_after_continuing_waits(session, other, monkeypatch):
    """Two journals continuing one stream would trade refusals forever: the
    one refused again within the cooldown waits out its 409 instead."""
    sid, journal, server, _ = session
    clock = _journal_clock(monkeypatch)
    _raced(journal, other, server, sid)
    with pytest.raises(ReconciliationRequired):
        journal.deliver(sid, server)
    journal.reconcile_pending(sid, server)
    path = Path(journal.get(sid)["path"])
    _write(path, sid, "raced again")
    journal.stage(sid, cwd="/work")  # ours: batch 1
    other.reconcile_pending(sid, server) if other.pending(sid) else None
    _write(path, sid, "and again")
    other.stage(sid, cwd="/work")
    assert other.deliver(sid, server)  # theirs takes batch 1 over a different range
    with pytest.raises(ReconciliationRequired):
        journal.deliver(sid, server)
    before = journal.get(sid)
    with pytest.raises(ReconciliationRequired, match="racing") as waited:
        journal.reconcile_pending(sid, server)
    assert waited.value.status_code == 409 and journal.get(sid) == before
    clock[0] += sj.REBASE_COOLDOWN_SECONDS + 1
    assert "continuing the server's stream" in journal.reconcile_pending(sid, server)


def test_a_rebuild_refused_after_its_snapshot_copy_leaves_the_journal_as_it_was(
    session, other, tmp_path, monkeypatch
):
    _sid, journal, server, _ = session
    sid = str(uuid4())
    path = tmp_path / f"{sid}.jsonl"
    _write(path, sid, "imported turn")
    journal.ensure(sid, path, server.receipts(sid), historical=True, cwd="/work")
    other.ensure(sid, path, server.receipts(sid), historical=False, cwd="/work")
    assert other.stage(sid, cwd="/work") and other.deliver(sid, server)
    body = journal.stage(sid, cwd="/work")
    Path(journal.get(sid)["snapshot_path"]).unlink()  # the rebuild copies a new one
    with pytest.raises(ReconciliationRequired):
        journal.deliver(sid, server)
    before = journal.get(sid)

    def refuse(*_args):
        raise ReconciliationRequired("historical prefix differs")

    monkeypatch.setattr(sj, "_validate_history", refuse)  # runs after the copy
    with pytest.raises(ReconciliationRequired, match="historical prefix differs"):
        journal.reconcile_pending(sid, server)
    assert journal.pending(sid) == body and journal.get(sid) == before


def test_a_nested_transaction_that_fails_undoes_only_its_own_writes(session):
    _sid, journal, _, _ = session
    with journal.transaction():
        journal.conn.execute("INSERT INTO pending VALUES('outer', x'00', 'outer')")
        with pytest.raises(RuntimeError):
            with journal.transaction():
                journal.conn.execute("INSERT INTO pending VALUES('inner', x'00', 'inner')")
                raise RuntimeError("inner refused")
    assert journal.pending("outer") is not None and journal.pending("inner") is None


def test_a_frozen_history_the_server_pinned_differently_is_frozen_again_at_its_pin(
    session, other, tmp_path
):
    """The server's stream carries ANOTHER journal's frozen history: ours is
    not kept (`ensure` would refuse the two pins), it is frozen again at the
    server's, and the rebuild goes through."""
    _sid, journal, server, _ = session
    sid = str(uuid4())
    path = tmp_path / f"{sid}.jsonl"
    _write(path, sid, "imported turn")
    other.ensure(sid, path, server.receipts(sid), historical=True, cwd="/work")
    _write(path, sid, "a later turn")
    journal.ensure(sid, path, server.receipts(sid), historical=True, cwd="/work")
    assert journal.get(sid)["historical_end"] > other.get(sid)["historical_end"]
    assert other.stage(sid, cwd="/work") and other.deliver(sid, server)
    pin = server.stream["snapshot_byte_end"]
    assert pin == other.get(sid)["historical_end"]
    journal.stage(sid, cwd="/work")
    with pytest.raises(ReconciliationRequired, match="another stream"):
        journal.deliver(sid, server)
    assert "continuing the server's stream" in journal.reconcile_pending(sid, server)
    assert journal.get(sid)["historical_end"] == pin


def test_a_missing_source_refuses_the_rebuild_as_a_409(session, other):
    sid, journal, server, _ = session
    _raced(journal, other, server, sid)
    with pytest.raises(ReconciliationRequired):
        journal.deliver(sid, server)
    Path(journal.get(sid)["path"]).unlink()
    with pytest.raises(ReconciliationRequired, match="unreadable") as kept:
        journal.reconcile_pending(sid, server)
    assert kept.value.status_code == 409 and journal.pending(sid)


def test_a_source_rewritten_inside_the_batch_stays_rewritten(session, other):
    """The rewritten-source rule outranks a rebuild: bytes changed inside what
    this journal staged are never re-uploaded from the changed file."""
    sid, journal, server, _ = session
    _raced(journal, other, server, sid)
    with pytest.raises(ReconciliationRequired):
        journal.deliver(sid, server)
    path = Path(journal.get(sid)["path"])
    path.write_bytes(path.read_bytes().replace(b"later", b"LATER"))
    body = journal.pending(sid)
    with pytest.raises(sj.SourceRewritten):
        journal.reconcile_pending(sid, server)
    assert journal.pending(sid) == body
