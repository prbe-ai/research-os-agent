"""The tap daemon restarts a new session's batch 0 on the other protocol when
the server refuses its protocol (protocol 3 not enabled -> 2; protocol 2
retired -> 3), in the same pass, over its real urllib Wire and a local server."""

from __future__ import annotations

import json
import logging

from tap.session_journal import Journal, Wire
from tests.test_session_deleted import (  # noqa: F401 - fixtures
    CUSTOMER,
    Server,
    _run_ticks,
    daemon,
    server,
)


def test_a_refused_protocol_3_batch_0_is_sent_on_protocol_2_in_the_same_pass(
    daemon, server, monkeypatch, caplog  # noqa: F811
):
    config, storage = daemon
    real_receipts, real_post = Wire.receipts, Wire.post
    refused = []

    def receipts(self, session_id, **kwargs):
        body = real_receipts(self, session_id, **kwargs)
        body["accepts"] = {"protocols": [2, 3], "fragment_versions": [1], "events": False}
        return body

    def post(self, body):
        if json.loads(body)["protocol_version"] == 3:  # the door's list changed meanwhile
            refused.append(body)
            return 409, {"detail": "protocol 3 not enabled"}
        return real_post(self, body)

    monkeypatch.setattr(Wire, "receipts", receipts)
    monkeypatch.setattr(Wire, "post", post)
    caplog.set_level(logging.INFO, logger="probe-research-tap")
    _run_ticks(monkeypatch, config, storage, ticks=1)
    assert len(refused) == 1 and json.loads(refused[0])["batch_seq"] == 0
    posts = server.posts(config.session_id)
    assert posts and posts[0]["batch_seq"] == 0 and {p["protocol_version"] for p in posts} == {2}
    assert [r for r in caplog.records if "restarts on protocol 2" in r.getMessage()]
    assert not [r for r in caplog.records if "retained for retry" in r.getMessage()]


def test_a_pending_protocol_2_batch_0_is_sent_on_protocol_3_once_2_is_retired(
    daemon, server, monkeypatch, caplog  # noqa: F811
):
    """Gate 2 (#2454): a session staged on protocol 2 before this tap was
    installed, never taken by the server, is refused for good at batch 0. The
    daemon drops that batch and sends the whole session on protocol 3, under
    the same stream id, in its first pass."""
    config, storage = daemon
    sid = config.session_id
    wire = Wire(server.url, "synthetic-token", "claude_code", timeout=5)
    before = Journal(server.url, CUSTOMER, "claude_code")  # no `accepts`: protocol 2
    before.ensure(sid, config.transcript_path, wire.receipts(sid), historical=False,
                  cwd=str(config.cwd))
    staged = json.loads(before.stage(sid, cwd=str(config.cwd)))
    assert staged["protocol_version"] == 2 and staged["batch_seq"] == 0
    before.close()

    server.retire_protocol2 = True
    server.accepts = {"protocols": [3], "fragment_versions": [1], "events": False}
    caplog.set_level(logging.INFO, logger="probe-research-tap")
    _run_ticks(monkeypatch, config, storage, ticks=1)
    posts = server.posts(sid)
    assert [(p["protocol_version"], p["batch_seq"]) for p in posts[:2]] == [(2, 0), (3, 0)]
    accepted = [json.loads(body) for (s, _), body in sorted(server.accepted.items()) if s == sid]
    assert accepted and {b["protocol_version"] for b in accepted} == {3}
    assert {b["stream_id"] for b in accepted} == {staged["stream_id"]}
    assert accepted[-1]["event_end"] == staged["event_end"] == 3
    assert len(accepted[0]["fragments"]) == 3 and server.streams[sid] == 3
    assert [r for r in caplog.records if "restarts on protocol 3" in r.getMessage()]
    assert not [r for r in caplog.records if "retained for retry" in r.getMessage()]
