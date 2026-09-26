"""A session deleted at the customer's request is FINAL for the tap.

The engine refuses a deleted session's bytes with 410
{"detail": {"reason": "session_deleted", ...}} on the upload door and answers
its receipts read with `state: "deleted"`; the research-os gateway passes both
through unchanged. Before this, the tap classified the refusal as retryable
and re-sent the session every tick for as long as its daemon ran.

Every test here talks to a real HTTP server on localhost through the tap's own
urllib clients (`Wire`, `httpclient.post_json`), so the exact requests and the
exact response bodies are what is under test, not a stand-in for them.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tap import httpclient, outbox
from tap import main as tapmain
from tap.session_journal import DeliveryPending, Journal, SessionDeleted, Wire
from tap.storage import Storage

CUSTOMER = "tenant-synthetic"


def _deleted_body(session_id: str) -> dict:
    """What the gateway forwards, byte for byte the engine's 410 body."""
    return {
        "detail": {
            "reason": "session_deleted",
            "message": "this session was deleted at the customer's request; do not resend it",
            "source": "claude_code",
            "session_id": session_id,
        }
    }


class Server:
    """Just enough of the ingest gateway: receipts reads and batch uploads.

    `deleted` sessions answer receipts `state: "deleted"` (or a 410 when
    `receipts_410`) and uploads 410; every other session is accepted with a
    receipt that matches what was sent.
    """

    def __init__(self):
        self.requests: list[tuple[str, str, dict, bytes]] = []
        self.deleted: set[str] = set()
        self.receipts_410 = False
        self.upload_status: int | None = None  # force an answer for uploads
        self.upload_body: dict | None = None
        self.accepted: dict[tuple[str, int], bytes] = {}
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _answer(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                server.requests.append(("GET", self.path, dict(self.headers), b""))
                parts = self.path.split("?")[0].split("/")
                # /ingest/v1/sessions/<source>/<sid>/receipts
                source, sid = parts[4], parts[5]
                if sid in server.deleted and server.receipts_410:
                    return self._answer(410, _deleted_body(sid))
                state = "deleted" if sid in server.deleted else "absent"
                self._answer(
                    200,
                    dict(
                        protocol_version=2,
                        customer_id=CUSTOMER,
                        source=source,
                        session_id=sid,
                        state=state,
                        receipts=[],
                        uploader_device_id="device-synthetic",
                    ),
                )

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                server.requests.append(("POST", self.path, dict(self.headers), body))
                data = json.loads(body)
                sid = data["session_id"]
                if server.upload_status is not None:
                    return self._answer(server.upload_status, server.upload_body or {})
                if sid in server.deleted:
                    return self._answer(410, _deleted_body(sid))
                if "protocol_version" not in data:
                    return self._answer(202, {"status": "accepted"})
                server.accepted[sid, data["batch_seq"]] = body
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
                    body_sha256=hashlib.sha256(body).hexdigest(),
                    finalized=bool(data.get("finalize")),
                )
                self._answer(202, dict(status="accepted", protocol_version=2, receipt=receipt))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def posts(self, sid: str | None = None) -> list[dict]:
        return [
            json.loads(body)
            for method, _path, _headers, body in self.requests
            if method == "POST" and (sid is None or json.loads(body)["session_id"] == sid)
        ]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def server():
    s = Server()
    yield s
    s.close()


def _transcript(directory: Path, sid: str, turns: int = 3) -> Path:
    path = directory / f"{sid}.jsonl"
    path.write_text(
        "".join(
            json.dumps(dict(type="user", sessionId=sid, message=dict(role="user", content=f"t{i}")))
            + "\n"
            for i in range(turns)
        )
    )
    return path


@pytest.fixture
def journal_env(tmp_path, monkeypatch, server):
    monkeypatch.setenv("PROBE_TRANSCRIPT_STATE_DIR", str(tmp_path / "v2"))
    wire = Wire(server.url, "synthetic-token", "claude_code", timeout=5)
    journal = Journal(server.url, CUSTOMER, "claude_code")
    yield tmp_path, wire, journal
    journal.close()


# --- the journal, through the real Wire ----------------------------------------------


def test_a_410_drops_the_pending_batch_and_nothing_is_sent_again(journal_env, server):
    tmp_path, wire, journal = journal_env
    sid = str(uuid4())
    path = _transcript(tmp_path, sid)
    journal.ensure(sid, path, wire.receipts(sid), historical=True)
    state = journal.get(sid)
    assert state["snapshot_path"] and Path(state["snapshot_path"]).exists()
    staged = journal.stage(sid, cwd=str(tmp_path))
    assert staged is not None
    server.deleted.add(sid)  # the deletion lands between staging and delivery

    with pytest.raises(SessionDeleted) as refused:
        journal.deliver(sid, wire)
    assert refused.value.session_id == sid and refused.value.retryable is False

    # The exact request the refusal answered: this batch, with the credential.
    (method, route, headers, body) = server.requests[-1]
    assert (method, route, body) == ("POST", "/ingest/v1/sessions/claude-code", staged)
    assert headers["Authorization"] == "Bearer synthetic-token"
    assert journal.pending(sid) is None
    state = journal.get(sid)
    assert state["deleted"] and state["finalized"] and state["historical_complete"]
    assert not state["live_enabled"] and state["snapshot_path"] is None
    assert list(journal.directory.glob("*.snapshot.jsonl")) == []
    assert path.exists(), "the researcher's own transcript is theirs; never touched"

    sent = len(server.requests)
    with pytest.raises(SessionDeleted):
        journal.stage(sid, cwd=str(tmp_path))
    with pytest.raises(SessionDeleted):
        journal.deliver(sid, wire, expected_body=staged)
    with pytest.raises(SessionDeleted):
        journal.ensure(sid, path, wire.receipts(sid), historical=False)
    assert [r[0] for r in server.requests[sent:]] == ["GET"]  # only the receipts read
    assert journal.mark_deleted(sid) is False  # idempotent: nothing left to change


def test_a_deleted_receipts_state_is_settled_before_anything_is_staged(journal_env, server):
    tmp_path, wire, journal = journal_env
    sid = str(uuid4())
    path = _transcript(tmp_path, sid)
    server.deleted.add(sid)
    remote = wire.receipts(sid)
    assert remote["state"] == "deleted"
    with pytest.raises(SessionDeleted):
        journal.ensure(sid, path, remote, historical=False, cwd=str(tmp_path))
    state = journal.get(sid)
    assert state["deleted"] and state["path"] == str(path)
    assert journal.pending(sid) is None
    assert list(journal.directory.glob("*.snapshot.jsonl")) == []
    assert server.posts() == []


def test_a_deleted_receipts_answer_settles_a_pending_batch_under_reconciliation(
    journal_env, server
):
    """`reconcile_pending` reads receipts for a stuck batch; `deleted` ends it."""
    tmp_path, wire, journal = journal_env
    sid = str(uuid4())
    path = _transcript(tmp_path, sid)
    journal.ensure(sid, path, wire.receipts(sid), historical=True)
    assert journal.stage(sid, cwd=str(tmp_path)) is not None
    server.deleted.add(sid)
    with pytest.raises(SessionDeleted):
        journal.reconcile_pending(sid, wire)
    assert journal.pending(sid) is None and journal.is_deleted(sid)
    assert server.posts() == []


def test_a_410_receipts_read_is_final_too(journal_env, server):
    _tmp_path, wire, _journal = journal_env
    sid = str(uuid4())
    server.deleted.add(sid)
    server.receipts_410 = True
    with pytest.raises(SessionDeleted) as refused:
        wire.receipts(sid)
    assert refused.value.session_id == sid


@pytest.mark.parametrize(
    "body",
    [{"detail": "gone"}, {"detail": {"reason": "something_else"}}, {}],
    ids=["string-detail", "other-reason", "no-detail"],
)
def test_a_410_that_is_not_a_deletion_keeps_the_batch(journal_env, server, body):
    """Only the server's own statement may make the tap drop a session."""
    tmp_path, wire, journal = journal_env
    sid = str(uuid4())
    path = _transcript(tmp_path, sid)
    journal.ensure(sid, path, wire.receipts(sid), historical=True)
    staged = journal.stage(sid, cwd=str(tmp_path))
    server.upload_status, server.upload_body = 410, body
    with pytest.raises(DeliveryPending) as pending:
        journal.deliver(sid, wire)
    assert not isinstance(pending.value, SessionDeleted)
    assert journal.pending(sid) == staged and not journal.is_deleted(sid)


# --- the daemon loop, end to end over HTTP --------------------------------------------


@pytest.fixture
def daemon(tmp_path, monkeypatch, server):
    monkeypatch.setenv("PROBE_TRANSCRIPT_STATE_DIR", str(tmp_path / "v2"))
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("PROBE_BASE_URL", server.url)
    monkeypatch.setattr(tapmain, "_shutdown_requested", False)
    monkeypatch.setattr(tapmain.killswitch, "is_ingestion_enabled", lambda **kw: (True, None))
    monkeypatch.setattr(tapmain.reconcile, "find_gaps", lambda *args, **kw: [])
    sid = str(uuid4())
    path = _transcript(tmp_path, sid)
    config = SimpleNamespace(
        session_id=sid,
        transcript_path=path,
        cwd=tmp_path,
        token="synthetic-token",
        active_interval_s=1,
        idle_interval_s=1,
        shutdown_sentinel=tmp_path / "shutdown",
    )
    storage = Storage(tmp_path / "legacy.db")
    yield config, storage
    storage.close()


def _run_ticks(monkeypatch, config, storage, ticks: int) -> None:
    count = 0

    def sleep(_seconds):
        nonlocal count
        count += 1
        if count >= ticks:
            tapmain._shutdown_requested = True

    monkeypatch.setattr(tapmain.time, "sleep", sleep)
    assert tapmain._run_durable_loop(config, storage) == 0


def _deleted_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "deleted at your team's request" in r.getMessage()]


def test_the_daemon_sends_a_deleted_session_once_and_never_again(
    daemon, server, monkeypatch, caplog
):
    """The loop that used to be "retained for retry" every tick for good."""
    config, storage = daemon
    # The deletion lands after the daemon reserved the session: its receipts
    # read said `absent`, and its first upload is the one refused.
    real_stage = Journal.stage

    def stage_then_delete(self, *args, **kwargs):
        body = real_stage(self, *args, **kwargs)
        server.deleted.add(config.session_id)
        return body

    monkeypatch.setattr(Journal, "stage", stage_then_delete)
    caplog.set_level(logging.INFO, logger="probe-research-tap")
    _run_ticks(monkeypatch, config, storage, ticks=6)

    posts = server.posts(config.session_id)
    assert len(posts) == 1, "refused once, then never re-sent"
    assert len(_deleted_lines(caplog)) == 1
    assert not [r for r in caplog.records if "retained for retry" in r.getMessage()]
    journal = Journal(server.url, CUSTOMER, "claude_code")
    assert journal.is_deleted(config.session_id) and journal.pending(config.session_id) is None
    journal.close()


def test_a_session_deleted_before_the_daemon_starts_is_never_uploaded(
    daemon, server, monkeypatch, caplog
):
    config, storage = daemon
    server.deleted.add(config.session_id)
    caplog.set_level(logging.INFO, logger="probe-research-tap")
    _run_ticks(monkeypatch, config, storage, ticks=4)
    assert server.posts() == []
    receipts_reads = [r for r in server.requests if r[0] == "GET"]
    # One read to learn it, none at the stop either: the stop does not
    # finalize a session that is gone.
    assert len(receipts_reads) == 1
    assert len(_deleted_lines(caplog)) == 1
    journal = Journal(server.url, CUSTOMER, "claude_code")
    assert journal.is_deleted(config.session_id)
    journal.close()

    # A restart needs one receipts read to open its journal (the journal is
    # per team), then finds the deletion already recorded: no upload, and no
    # second log line.
    caplog.clear()
    monkeypatch.setattr(tapmain, "_shutdown_requested", False)
    before = len(server.requests)
    _run_ticks(monkeypatch, config, storage, ticks=3)
    assert [r[0] for r in server.requests[before:]] == ["GET"]
    assert _deleted_lines(caplog) == []


def test_another_session_keeps_shipping_while_a_deleted_one_is_skipped(
    daemon, server, monkeypatch
):
    config, storage = daemon
    other = str(uuid4())
    other_path = _transcript(config.cwd, other, turns=2)
    journal = Journal(server.url, CUSTOMER, "claude_code")
    wire = Wire(server.url, "synthetic-token", "claude_code", timeout=5)
    journal.ensure(other, other_path, wire.receipts(other), historical=True, cwd=str(config.cwd))
    journal.close()
    server.deleted.add(config.session_id)
    _run_ticks(monkeypatch, config, storage, ticks=3)
    assert server.posts(config.session_id) == []
    assert server.posts(other), "the other session's history still ships"
    journal = Journal(server.url, CUSTOMER, "claude_code")
    assert journal.get(other)["finalized"] and not journal.is_deleted(other)
    journal.close()


# --- the legacy outbox ----------------------------------------------------------------


def _legacy_body(sid: str, seq: int) -> bytes:
    return json.dumps(
        dict(session_id=sid, batch_seq=seq, device_id="d", cwd="/tmp", events=[{"line_no": seq}])
    ).encode()


def test_legacy_outbox_drops_every_batch_of_a_deleted_session_and_spools_no_more(
    tmp_path, monkeypatch, server
):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "claude_code")
    storage = Storage(tmp_path / "legacy.db")
    try:
        sid, bystander = str(uuid4()), str(uuid4())
        for seq in range(3):
            outbox.enqueue(storage=storage, session_id=sid, batch_seq=seq, cwd="/tmp",
                           body=_legacy_body(sid, seq), now=0)
        outbox.enqueue(storage=storage, session_id=bystander, batch_seq=0, cwd="/tmp",
                       body=_legacy_body(bystander, 0), now=0)
        server.deleted.add(sid)

        assert outbox.drain_once(storage=storage, token="synthetic-token", base_url=server.url,
                                 session_id=sid)
        (method, route, headers, body) = server.requests[-1]
        assert (method, route, body) == ("POST", "/ingest/v1/sessions/claude-code",
                                         _legacy_body(sid, 0))
        assert headers["Authorization"] == "Bearer synthetic-token"
        # All three of its batches are gone after ONE refusal; the bystander stays.
        assert storage.outbox_row_count() == 1
        assert storage.max_batch_seq(sid) == -1 and storage.max_batch_seq(bystander) == 0
        assert not outbox.drain_once(storage=storage, token="synthetic-token",
                                     base_url=server.url, session_id=sid)
        outbox.enqueue(storage=storage, session_id=sid, batch_seq=3, cwd="/tmp",
                       body=_legacy_body(sid, 3), now=0)
        assert storage.max_batch_seq(sid) == -1, "a deleted session is never spooled again"
        assert len(server.posts(sid)) == 1
    finally:
        storage.close()


def test_legacy_outbox_410_without_the_deletion_reason_drops_only_that_batch(
    tmp_path, monkeypatch, server
):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "claude_code")
    storage = Storage(tmp_path / "legacy.db")
    try:
        sid = str(uuid4())
        for seq in range(2):
            outbox.enqueue(storage=storage, session_id=sid, batch_seq=seq, cwd="/tmp",
                           body=_legacy_body(sid, seq), now=0)
        server.upload_status, server.upload_body = 410, {"detail": "gone"}
        assert outbox.drain_once(storage=storage, token="t", base_url=server.url, session_id=sid)
        assert storage.max_batch_seq(sid) == 1  # POISON: this batch only, as before
        assert not storage.get_meta(outbox.deleted_session_key(sid))
    finally:
        storage.close()


def test_session_deleted_matches_only_the_servers_statement():
    ok = httpclient.Response(410, json.dumps(_deleted_body("s")).encode(),
                             httpclient.Classification.POISON)
    assert outbox.session_deleted(ok)
    for status, body in [(410, b"gone"), (409, json.dumps(_deleted_body("s")).encode()),
                         (410, b'{"detail": {"reason": "other"}}')]:
        other = httpclient.Response(status, body, httpclient.Classification.POISON)
        assert not outbox.session_deleted(other)


def test_settling_a_deletion_never_ends_the_daemon(daemon, server, monkeypatch, caplog):
    """`_settle_deleted` runs inside an `except` clause: a second error there
    would escape the loop. A locked journal is logged and retried, not fatal."""
    config, storage = daemon
    server.deleted.add(config.session_id)
    import sqlite3

    calls = {"n": 0}
    real = Journal.mark_deleted

    def flaky(self, session_id, path=None):
        calls["n"] += 1
        if calls["n"] == 2:  # the handler's re-mark, after `ensure` marked it
            raise sqlite3.OperationalError("database is locked")
        return real(self, session_id, path)

    monkeypatch.setattr(Journal, "mark_deleted", flaky)
    caplog.set_level(logging.INFO, logger="probe-research-tap")
    _run_ticks(monkeypatch, config, storage, ticks=3)  # returns normally
    assert calls["n"] >= 2
    assert server.posts() == []
    assert any("could not record its deletion yet" in r.getMessage() for r in caplog.records)


def test_a_410_whose_body_is_not_an_object_is_not_a_deletion():
    for body in (b"[1, 2]", b'"gone"', b"null"):
        resp = httpclient.Response(410, body, httpclient.Classification.POISON)
        assert not outbox.session_deleted(resp)


def test_a_deletion_naming_another_session_is_not_this_ones(journal_env, server):
    """A 410 `session_deleted` is a statement about the session it names: one
    naming a different session never drops this session's batch or snapshot."""
    tmp_path, wire, journal = journal_env
    sid = str(uuid4())
    path = _transcript(tmp_path, sid)
    journal.ensure(sid, path, wire.receipts(sid), historical=True)
    staged = journal.stage(sid, cwd=str(tmp_path))
    server.upload_status, server.upload_body = 410, _deleted_body(str(uuid4()))
    with pytest.raises(DeliveryPending) as pending:
        journal.deliver(sid, wire)
    assert not isinstance(pending.value, SessionDeleted)
    assert journal.pending(sid) == staged and not journal.is_deleted(sid)

    other = httpclient.Response(410, json.dumps(_deleted_body(str(uuid4()))).encode(),
                                httpclient.Classification.POISON)
    assert outbox.session_deleted(other) and not outbox.session_deleted(other, sid)


def test_legacy_outbox_ignores_a_deletion_naming_another_session(tmp_path, monkeypatch, server):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "claude_code")
    storage = Storage(tmp_path / "legacy.db")
    try:
        sid = str(uuid4())
        for seq in range(2):
            outbox.enqueue(storage=storage, session_id=sid, batch_seq=seq, cwd="/tmp",
                           body=_legacy_body(sid, seq), now=0)
        server.upload_status, server.upload_body = 410, _deleted_body(str(uuid4()))
        assert outbox.drain_once(storage=storage, token="t", base_url=server.url, session_id=sid)
        assert storage.max_batch_seq(sid) == 1  # POISON: this batch only
        assert not storage.get_meta(outbox.deleted_session_key(sid))
    finally:
        storage.close()
