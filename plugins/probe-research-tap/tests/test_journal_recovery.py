"""A pending batch that today's redaction refuses must not strand its session.

Pending bytes are immutable because the server may already hold them, and
every newer daemon refuses to replay a body its own scrubber would change. So
a batch staged under older rules sat in the journal forever and its session
stopped shipping: 16 of 20 pending bodies on one machine. `reconcile_pending`
asks the server about that one batch_seq and either adopts its receipt or,
when nothing accepted it, rebuilds it under current redaction.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

import pytest

from tap import session_journal as sj
from tap.session_journal import Journal, ReconciliationRequired, UnsafePending

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
            "snapshot_byte_end": None,
            "snapshot_sha256": None,
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
        seq = json.loads(body)["batch_seq"]
        previous = self.receipts_by_seq.get(seq)
        if previous is not None:
            if previous["body_sha256"] != hashlib.sha256(body).hexdigest():
                return 409, {"detail": "batch identity already accepted with different content"}
            return 202, {"protocol_version": 2, "status": "duplicate", "receipt": previous}
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
    sid, journal, server, monkeypatch = session
    body = _stage_under_old_rules(sid, journal, monkeypatch)
    receipt = server.accept(body)
    receipt["source_byte_end"] += 1
    with pytest.raises(ReconciliationRequired):
        journal.reconcile_pending(sid, server)
    assert journal.pending(sid) == body
    assert journal.get(sid)["last_seq"] == -1


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
