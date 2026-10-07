"""The tap daemon restarts a session on protocol 2 when its protocol-3 batch 0
is refused, in the same pass, over its real urllib Wire and a local server."""

from __future__ import annotations

import json
import logging

from tap.session_journal import Wire
from tests.test_session_deleted import Server, _run_ticks, daemon, server  # noqa: F401 - fixtures


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
