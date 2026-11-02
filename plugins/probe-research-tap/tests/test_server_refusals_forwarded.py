"""A refusal the server's message registry wrote reaches the session's coding agent from BOTH tap
transports (R12, R23): the protocol-1 outbox (`httpclient` + `outbox`) and the protocol-2/3 `Wire`.

A real HTTP server on localhost answers through the tap's own urllib clients; the post goes to the
session's Probe mailbox files (the machine's Probe service is never asked here)."""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import pytest

from tap import outbox, server_messages
from tap.server_messages import mailbox as tap_mailbox
from tap.transcripts.journal import DeliveryPending, Wire
from tap.storage import Storage

#: The bodies the server pins (app/capture/clients/messages, #3895).
FLAT = {"detail": "Starting a run with no project needs a newer Probe CLI; the new one asks for `project=` "
                  "(or `PROBE_PROJECT`). Upgrade to probe-research 0.360.0 or later: `pip install -U probe-research`.",
        "code": "client_too_old", "min_version": "0.360.0", "message_id": "runs_unfiled_create",
        "action": "update", "severity": "blocking"}
NESTED = {"detail": {"code": "workspace_required", "message": "there is no workspace you can file into: create one "
                     "(`probe workspace create <slug> --use`) or ask for access to one.",
                     "message_id": "workspace_required_none_visible", "action": "none", "severity": "blocking"}}


class _Refusing:
    """Answers every request with `status` and `body`."""

    def __init__(self):
        self.status, self.body = 410, FLAT
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _answer(self):
                if self.headers.get("Content-Length"):
                    self.rfile.read(int(self.headers["Content-Length"]))
                data = json.dumps(server.body).encode()
                self.send_response(server.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _answer

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def refusing():
    server = _Refusing()
    yield server
    server.httpd.shutdown()
    server.httpd.server_close()


@pytest.fixture
def reads(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("PROBE_TAP_SOURCE", "claude_code")
    monkeypatch.setattr(tap_mailbox, "_post_via_service", lambda *_args: False)
    return tmp_path / "state" / "probe" / "reads"


def _told(reads, sid: str) -> list[dict]:
    folder = reads / "messages" / sid
    return [json.loads(path.read_text()) for path in sorted(folder.glob("*.json"))] if folder.is_dir() else []


def _batch(sid: str, seq: int) -> bytes:
    return json.dumps(dict(session_id=sid, batch_seq=seq, device_id="d", cwd="/tmp", events=[])).encode()


def test_a_refused_protocol_1_batch_reaches_the_agent_once(reads, refusing, tmp_path, caplog):
    storage = Storage(tmp_path / "legacy.db")
    sid = str(uuid4())
    for seq in range(2):
        outbox.enqueue(storage=storage, session_id=sid, batch_seq=seq, cwd="/tmp", body=_batch(sid, seq), now=0)
    with caplog.at_level(logging.WARNING, logger="probe-research-tap.outbox"):
        for _ in range(2):
            assert outbox.drain_once(storage=storage, token="t", base_url=refusing.url, session_id=sid)
    assert storage.outbox_row_count() == 0, "dropped as before"
    (told,) = _told(reads, sid)
    assert (told["kind"], told["id"], told["text"]) == ("server", "nruns_unfiled_create_0", FLAT["detail"])
    assert (told["severity"], told["audience"]) == ("blocking", "both")
    assert "server: 'Starting a run with no project" in caplog.text, "the log says the sentence, not raw bytes"


def test_a_refused_protocol_2_request_reaches_the_agent(reads, refusing):
    sid = str(uuid4())
    wire = Wire(refusing.url, "t", "claude_code", on_refusal=server_messages.forward_refusal)
    assert wire.post(_batch(sid, 0)) == (410, FLAT)
    refusing.status, refusing.body = 422, NESTED
    with pytest.raises(DeliveryPending):
        wire.receipts(sid)
    assert [told["text"] for told in _told(reads, sid)] == [FLAT["detail"], NESTED["detail"]["message"]]


def test_an_unregistered_refusal_tells_nobody_and_a_failing_forward_breaks_nothing(reads, refusing):
    sid = str(uuid4())
    refusing.status, refusing.body = 400, {"detail": "batch too large"}
    assert Wire(refusing.url, "t", "claude_code", on_refusal=server_messages.forward_refusal).post(
        _batch(sid, 0)) == (400, {"detail": "batch too large"})
    assert _told(reads, sid) == []

    def broken(*_args):
        raise RuntimeError("the mailbox is gone")

    refusing.status, refusing.body = 410, FLAT
    assert Wire(refusing.url, "t", "claude_code", on_refusal=broken).post(_batch(sid, 0)) == (410, FLAT)


def test_a_failed_post_is_tried_again_and_a_shown_copy_is_never_written_twice(reads, monkeypatch):
    sid = str(uuid4())
    message = server_messages.parse_server_message(410, FLAT)
    with monkeypatch.context() as patched:
        patched.setattr(tap_mailbox, "_write_here", lambda *_args: False)
        assert not server_messages.post_server_message(sid, message)
    # The service's copy landed after the answer was lost, and a hook already showed it.
    claimed = reads / "claimed" / sid
    claimed.mkdir(parents=True, exist_ok=True)
    (claimed / "00000000000000000000-nruns_unfiled_create_0.json").write_text("{}")
    assert server_messages.post_server_message(sid, message), "the failed post released its marker"
    assert _told(reads, sid) == [], "shown already: not written again"
