"""A Kimi Code wire rewritten in place under the daemon's cursor.

Kimi rewrites `wire.jsonl` on resume when the file's `protocol_version` is
older than its own. Bytes the server already accepted change, at the same
size or not, so neither the journal's cursor nor the file size can be trusted
for that session again. Until the server can take a replacement stream, the
tap must stop that session, never re-upload it, and say so ONCE, naming the
session and the cause. Real Kimi Code 2.1.1 wire, a real local HTTP server.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tap import main as tapmain
from tap.session_journal import Journal, ReconciliationRequired, SourceRewritten
from tap.storage import Storage
from tests.test_session_deleted import CUSTOMER, Server

FIXTURE_SESSION = "9b40c4c4-d58a-4ed7-9372-cfecf8469b05"


@pytest.fixture
def server():
    s = Server()
    yield s
    s.close()


@pytest.fixture
def daemon(tmp_path, monkeypatch, server, kimi_fixtures):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "kimi_code")
    monkeypatch.setenv("PROBE_TRANSCRIPT_STATE_DIR", str(tmp_path / "v2"))
    monkeypatch.setenv("PROBE_KIMI_TAP_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("PROBE_BASE_URL", server.url)
    monkeypatch.setattr(tapmain, "_shutdown_requested", False)
    monkeypatch.setattr(tapmain.killswitch, "is_ingestion_enabled", lambda **kw: (True, None))
    monkeypatch.setattr(tapmain.reconcile, "find_gaps", lambda *args, **kw: [])
    sid = str(uuid4())
    src = kimi_fixtures / "sessions" / "wd_proj_846899122802" / f"session_{FIXTURE_SESSION}"
    session_dir = tmp_path / "sessions" / "wd_proj_x" / f"session_{sid}"
    shutil.copytree(src, session_dir)
    state = json.loads((session_dir / "state.json").read_text())
    state["id"] = f"session_{sid}"
    (session_dir / "state.json").write_text(json.dumps(state))
    config = SimpleNamespace(
        session_id=sid,
        transcript_path=session_dir / "agents" / "main" / "wire.jsonl",
        cwd=tmp_path,
        token="synthetic-token",
        active_interval_s=1,
        idle_interval_s=1,
        shutdown_sentinel=tmp_path / "shutdown",
    )
    storage = Storage(tmp_path / "legacy.db")
    yield config, storage
    storage.close()


def _rewrite_protocol_version(wire: Path) -> None:
    """What Kimi's migration does to a resumed older file, at EQUAL size: the
    case a size check cannot see."""
    raw = wire.read_bytes()
    rewritten = raw.replace(b'"protocol_version":"1.5"', b'"protocol_version":"1.6"', 1)
    assert rewritten != raw and len(rewritten) == len(raw)
    wire.write_bytes(rewritten)


def _run(monkeypatch, config, storage, *, ticks: int, on_first_sleep) -> None:
    count = 0

    def sleep(_seconds):
        nonlocal count
        count += 1
        if count == 1:
            on_first_sleep()
        if count >= ticks:
            tapmain._shutdown_requested = True

    monkeypatch.setattr(tapmain.time, "sleep", sleep)
    assert tapmain._run_durable_loop(config, storage) == 0


def _rewrite_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "rewritten in place" in r.getMessage()]


def test_a_rewritten_wire_stops_its_session_says_so_once_and_never_resends(
    daemon, server, monkeypatch, caplog
):
    config, storage = daemon
    caplog.set_level(logging.INFO, logger="probe-research-tap")
    _run(monkeypatch, config, storage, ticks=6,
         on_first_sleep=lambda: _rewrite_protocol_version(config.transcript_path))

    posts = server.posts(config.session_id)
    assert len(posts) == 1 and posts[0]["batch_seq"] == 0, "only the upload before the rewrite"
    lines = _rewrite_lines(caplog)
    assert len(lines) == 1, lines
    assert config.session_id in lines[0] and str(config.transcript_path) in lines[0]
    assert not [r for r in caplog.records if "retained for retry" in r.getMessage()]

    # A restarted daemon refuses it again, and stays quiet about it.
    caplog.clear()
    monkeypatch.setattr(tapmain, "_shutdown_requested", False)
    _run(monkeypatch, config, storage, ticks=3, on_first_sleep=lambda: None)
    assert len(server.posts(config.session_id)) == 1
    assert _rewrite_lines(caplog) == []


def test_the_journal_names_a_rewrite_as_its_own_refusal(daemon, server, tmp_path) -> None:
    """Equal size or shorter, the journal refuses with SourceRewritten, which
    every older caller still catches as ReconciliationRequired."""
    config, _storage = daemon
    from tap.session_journal import Wire

    wire = Wire(server.url, "synthetic-token", "kimi_code", timeout=5)
    journal = Journal(server.url, CUSTOMER, "kimi_code")
    try:
        sid = config.session_id
        journal.ensure(sid, config.transcript_path, wire.receipts(sid), historical=False, cwd=str(tmp_path))
        journal.stage(sid, cwd=str(tmp_path))
        journal.deliver(sid, wire)
        _rewrite_protocol_version(config.transcript_path)
        with pytest.raises(SourceRewritten):
            journal.stage(sid, cwd=str(tmp_path))
        config.transcript_path.write_bytes(config.transcript_path.read_bytes()[:100])
        with pytest.raises(SourceRewritten) as shorter:
            journal.stage(sid, cwd=str(tmp_path))
        assert isinstance(shorter.value, ReconciliationRequired)
        assert len(server.posts(sid)) == 1
    finally:
        journal.close()
