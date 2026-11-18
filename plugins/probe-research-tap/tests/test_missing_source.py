"""A session whose own transcript file is gone stops being retried, says one line, and stays quiet.

The past-chat import uploads each chat from a staged copy and deletes the copy; the shared
journal's row still names it. Before this the tap logged "retained for retry: [Errno 2]" for
every such row on every tick, forever. Each test drives the real daemon loop against a local
HTTP server through the tap's own `Wire`, so the receipts reads counted here are the real ones.
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

from tap import main as tapmain
from tap.storage import Storage
from tap.transcripts.journal import SOURCE_GONE_AT, SOURCE_GONE_NOTE, Journal, Wire

CUSTOMER = "tenant-synthetic"
SOURCE = "claude_code"


class Server:
    """Receipts reads and batch uploads. `streams[sid]` is the stream a receipts read reports;
    `receipts_status[sid]` forces an error answer; `deleted` sessions read `state: deleted`, or
    410 when also in `deleted_410`."""

    def __init__(self):
        self.requests: list[tuple[str, str, bytes]] = []
        self.streams: dict[str, dict] = {}
        self.receipts_status: dict[str, int] = {}
        self.deleted: set[str] = set()
        self.deleted_410: set[str] = set()
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
                server.requests.append(("GET", self.path, b""))
                source, sid = self.path.split("?")[0].split("/")[4:6]
                if sid in server.deleted_410:
                    return self._answer(410, {"detail": {
                        "reason": "session_deleted", "source": source, "session_id": sid,
                    }})
                if sid in server.receipts_status:
                    return self._answer(server.receipts_status[sid], {"detail": "unavailable"})
                answer = dict(
                    protocol_version=2, customer_id=CUSTOMER, source=source, session_id=sid,
                    state="deleted" if sid in server.deleted else "absent", receipts=[],
                )
                if sid in server.streams:
                    answer.update(state="ready", stream=server.streams[sid])
                self._answer(200, answer)

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                server.requests.append(("POST", self.path, body))
                data = json.loads(body)
                receipt = {
                    k: data[k]
                    for k in ("batch_seq", "source_byte_end", "source_line_end", "event_end",
                              "prefix_sha256")
                }
                receipt.update(
                    body_sha256=hashlib.sha256(body).hexdigest(), finalized=bool(data.get("finalize"))
                )
                self._answer(202, dict(status="accepted", protocol_version=2, receipt=receipt))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def receipts_reads(self, sid: str) -> int:
        """Receipts reads for `sid` after the import's own (`_imported_row`)."""
        return sum(
            1 for method, path, _ in self.requests if method == "GET" and f"/{sid}/" in path
        ) - 1

    def posts(self, sid: str) -> list[dict]:
        return [
            json.loads(body) for method, _, body in self.requests
            if method == "POST" and json.loads(body)["session_id"] == sid
        ]


@pytest.fixture
def server():
    s = Server()
    yield s
    s.httpd.shutdown()
    s.httpd.server_close()


def _transcript(path: Path, sid: str, turns: int = 3) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(
        json.dumps(dict(type="user", sessionId=sid, message=dict(role="user", content=f"t{i}")))
        + "\n"
        for i in range(turns)
    ))
    return path


@pytest.fixture
def daemon(tmp_path, monkeypatch, server):
    monkeypatch.setenv("PROBE_TRANSCRIPT_STATE_DIR", str(tmp_path / "v2"))
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("PROBE_BASE_URL", server.url)
    monkeypatch.setattr(tapmain, "_shutdown_requested", False)
    monkeypatch.setattr(tapmain.killswitch, "is_ingestion_enabled", lambda **kw: (True, None))
    monkeypatch.setattr(tapmain.reconcile, "find_gaps", lambda *args, **kw: [])
    sid = str(uuid4())
    config = SimpleNamespace(
        session_id=sid, transcript_path=_transcript(tmp_path / f"{sid}.jsonl", sid), cwd=tmp_path,
        token="synthetic-token", active_interval_s=1, idle_interval_s=1,
        shutdown_sentinel=tmp_path / "shutdown",
    )
    storage = Storage(tmp_path / "legacy.db")
    yield config, storage
    storage.close()


def _run_ticks(monkeypatch, config, storage, ticks: int = 3) -> None:
    count = 0

    def sleep(_seconds):
        nonlocal count
        count += 1
        if count >= ticks:
            tapmain._shutdown_requested = True

    monkeypatch.setattr(tapmain, "_shutdown_requested", False)
    monkeypatch.setattr(tapmain.time, "sleep", sleep)
    assert tapmain._run_durable_loop(config, storage) == 0


def _journal(server) -> Journal:
    return Journal(server.url, CUSTOMER, SOURCE)


def _imported_row(server, cwd: Path, folder: Path | None = None, *, staged: bool = True):
    """A past chat the import reserved and staged from a copy, then deleted the copy: the row
    keeps its own stream id, cursor 0 and a pending batch, whatever the server holds. `folder`
    is the folder on record (None: none at all, and nothing staged)."""
    sid = str(uuid4())
    copy = _transcript(cwd / "staged" / f"codex-{sid}.jsonl", sid)
    journal, wire = _journal(server), Wire(server.url, "synthetic-token", SOURCE, timeout=5)
    recorded = str(folder or cwd) if staged else None
    journal.ensure(sid, copy, wire.receipts(sid), historical=True, cwd=recorded)
    if staged:
        assert journal.stage(sid, cwd=recorded) is not None
    state = journal.get(sid)
    journal.close()
    copy.unlink()
    return SimpleNamespace(
        sid=sid, copy=copy, size=state["historical_end"], digest=state["historical_hash"]
    )


def _finalized(row, **stream) -> dict:
    """The server's stream: another id and cursor than the row's (the importer's coverage path
    never touched this journal), finalized at the row's end with the same bytes by default."""
    return dict(
        stream_id=str(uuid4()), finalized=True, last_seq=4, source_byte_end=row.size,
        source_line_end=3, event_end=3, prefix_sha256=row.digest,
    ) | stream


def _lines(caplog, sid: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if sid in r.getMessage() and "is gone" in r.getMessage()]


def _retries(caplog, sid: str) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if sid in r.getMessage() and "retained for retry" in r.getMessage()]


def _state(server, sid: str) -> dict:
    journal = _journal(server)
    try:
        return journal.get(sid)
    finally:
        journal.close()


def test_gone_and_the_same_bytes_finalized_says_info_once_and_is_skipped(
    daemon, server, monkeypatch, caplog
):
    config, storage = daemon
    row = _imported_row(server, config.cwd)
    server.streams[row.sid] = _finalized(row)
    caplog.set_level(logging.INFO)
    _run_ticks(monkeypatch, config, storage, ticks=4)

    (line,) = _lines(caplog, row.sid)
    assert line.levelno == logging.INFO and "already complete in Probe" in line.getMessage()
    assert server.receipts_reads(row.sid) == 1
    assert _retries(caplog, row.sid) == [] and server.posts(row.sid) == []
    journal = _journal(server)
    state = journal.get(row.sid)
    assert state[SOURCE_GONE_AT] and state[SOURCE_GONE_NOTE] == "complete"
    assert state["error"] is None and journal.pending(row.sid) is not None  # kept, never evicted
    journal.close()


@pytest.mark.parametrize("stream,level,said", [
    # Equal byte counts are no proof: the bytes differ.
    (dict(prefix_sha256="0" * 64), logging.WARNING,
     "Probe's copy differs from what this machine sent"),
    # The server holds more than this row ever knew: nothing local to compare with.
    (dict(source_byte_end="{size+50}", prefix_sha256="1" * 64), logging.INFO,
     "Probe holds a finalized copy ({size+50} bytes); not compared with this file, which is gone"),
    (dict(source_byte_end=10), logging.WARNING, "Probe's copy is incomplete: missing bytes 10-{size}"),
    (dict(finalized=False), logging.WARNING, "Probe's copy is incomplete: not finalized"),
    (None, logging.WARNING, "Probe's copy is incomplete: missing bytes 0-{size}"),
])
def test_gone_and_not_provably_the_same_says_what_differs_once(
    daemon, server, monkeypatch, caplog, stream, level, said
):
    config, storage = daemon
    row = _imported_row(server, config.cwd)
    if stream is not None:
        if stream.get("source_byte_end") == "{size+50}":
            stream = stream | dict(source_byte_end=row.size + 50)
        server.streams[row.sid] = _finalized(row, **stream)
    caplog.set_level(logging.INFO)
    _run_ticks(monkeypatch, config, storage)

    (line,) = _lines(caplog, row.sid)
    assert line.levelno == level and str(row.copy) in line.getMessage()
    assert said.replace("{size+50}", str(row.size + 50)).format(size=row.size) in line.getMessage()
    assert server.receipts_reads(row.sid) == 1 and _retries(caplog, row.sid) == []


def test_the_whole_pinned_history_counts_not_only_what_was_sent(
    daemon, server, monkeypatch, caplog
):
    config, storage = daemon
    row = _imported_row(server, config.cwd)
    journal = _journal(server)
    journal.update(row.sid, historical_end=1000)
    journal.close()
    server.streams[row.sid] = _finalized(row, source_byte_end=200)
    caplog.set_level(logging.INFO)
    _run_ticks(monkeypatch, config, storage)

    (line,) = _lines(caplog, row.sid)
    assert line.levelno == logging.WARNING and "missing bytes 200-1000" in line.getMessage()


def test_gone_with_the_server_unreachable_saves_not_checked_and_stops(
    daemon, server, monkeypatch, caplog
):
    config, storage = daemon
    row = _imported_row(server, config.cwd)
    server.receipts_status[row.sid] = 503
    caplog.set_level(logging.INFO)
    _run_ticks(monkeypatch, config, storage)

    (line,) = _lines(caplog, row.sid)
    assert line.levelno == logging.WARNING and "not checked against Probe" in line.getMessage()
    assert server.receipts_reads(row.sid) == 1 and _retries(caplog, row.sid) == []
    assert _state(server, row.sid)[SOURCE_GONE_NOTE] == "not checked"


@pytest.mark.parametrize("on_record", ["disabled", "none"])
def test_a_folder_not_captured_or_unknown_is_never_asked_about(
    daemon, server, monkeypatch, caplog, on_record
):
    config, storage = daemon
    folder = config.cwd / "private"
    folder.mkdir()
    row = _imported_row(server, config.cwd, folder, staged=on_record == "disabled")
    plugin = Path(tapmain.cfg.plugin_dir())
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / ".disabled_paths").write_text(f"{folder}\n")
    server.streams[row.sid] = _finalized(row)
    caplog.set_level(logging.INFO)
    _run_ticks(monkeypatch, config, storage)

    (line,) = _lines(caplog, row.sid)
    assert "not checked against Probe" in line.getMessage()
    assert server.receipts_reads(row.sid) == 0, "no request the base tap never sent"
    state = _state(server, row.sid)
    assert state[SOURCE_GONE_AT] and state[SOURCE_GONE_NOTE] == "not checked"


def test_a_restarted_tap_neither_logs_nor_asks_again(daemon, server, monkeypatch, caplog):
    config, storage = daemon
    row = _imported_row(server, config.cwd)
    server.streams[row.sid] = _finalized(row)
    caplog.set_level(logging.INFO)
    _run_ticks(monkeypatch, config, storage)
    assert len(_lines(caplog, row.sid)) == 1

    caplog.clear()
    _run_ticks(monkeypatch, config, storage)
    assert _lines(caplog, row.sid) == [] and _retries(caplog, row.sid) == []
    assert server.receipts_reads(row.sid) == 1


def test_a_file_back_at_the_same_path_drains_and_a_later_loss_is_said_again(
    daemon, server, monkeypatch, caplog
):
    config, storage = daemon
    row = _imported_row(server, config.cwd)
    caplog.set_level(logging.INFO)
    _run_ticks(monkeypatch, config, storage)
    assert server.posts(row.sid) == [] and len(_lines(caplog, row.sid)) == 1

    _transcript(row.copy, row.sid)  # the same bytes, back where the row points (a re-staged copy)
    _run_ticks(monkeypatch, config, storage)
    assert server.posts(row.sid), "the pending batch went once its file was readable again"
    state = _state(server, row.sid)
    assert SOURCE_GONE_AT not in state and SOURCE_GONE_NOTE not in state

    row.copy.unlink()  # gone for real this time: said again, once
    _run_ticks(monkeypatch, config, storage)
    assert len(_lines(caplog, row.sid)) == 2 and _retries(caplog, row.sid) == []


def test_a_resumed_chat_reregistered_by_ensure_clears_the_mark_and_drains(
    daemon, server, monkeypatch
):
    config, storage = daemon
    row = _imported_row(server, config.cwd)
    _run_ticks(monkeypatch, config, storage)
    journal = _journal(server)
    assert journal.get(row.sid)[SOURCE_GONE_AT]

    resumed = _transcript(config.cwd / "live" / f"{row.sid}.jsonl", row.sid)
    wire = Wire(server.url, "synthetic-token", SOURCE, timeout=5)
    state = journal.ensure(
        row.sid, resumed, wire.receipts(row.sid), historical=True, cwd=str(config.cwd)
    )
    assert SOURCE_GONE_AT not in state and SOURCE_GONE_NOTE not in state
    assert state["path"] == str(resumed)
    journal.close()

    _run_ticks(monkeypatch, config, storage)
    assert server.posts(row.sid)


def test_a_parent_turned_into_a_file_counts_as_gone(daemon, server, monkeypatch, caplog):
    config, storage = daemon
    row = _imported_row(server, config.cwd)
    blocker = config.cwd / "not-a-folder"
    blocker.write_text("")
    journal = _journal(server)
    journal.update(row.sid, path=str(blocker / row.copy.name))  # NotADirectoryError
    journal.close()
    caplog.set_level(logging.INFO)
    _run_ticks(monkeypatch, config, storage)

    assert len(_lines(caplog, row.sid)) == 1 and _retries(caplog, row.sid) == []
    assert _state(server, row.sid)[SOURCE_GONE_AT]


def test_another_error_reading_the_file_keeps_todays_retry(daemon, server, monkeypatch, caplog):
    config, storage = daemon
    row = _imported_row(server, config.cwd)
    journal = _journal(server)
    journal.update(row.sid, path=str(config.cwd / ("x" * 300) / row.copy.name))  # name too long
    journal.close()
    caplog.set_level(logging.INFO)
    _run_ticks(monkeypatch, config, storage)

    assert _retries(caplog, row.sid) and _lines(caplog, row.sid) == []
    assert server.receipts_reads(row.sid) == 0
    assert SOURCE_GONE_AT not in _state(server, row.sid)


@pytest.mark.parametrize("answer", ["state", "410"])
def test_a_deleted_answer_takes_the_existing_deleted_path(
    daemon, server, monkeypatch, caplog, answer
):
    config, storage = daemon
    row = _imported_row(server, config.cwd)
    server.deleted.add(row.sid)
    if answer == "410":
        server.deleted_410.add(row.sid)
    caplog.set_level(logging.INFO)
    _run_ticks(monkeypatch, config, storage)

    assert [r for r in caplog.records
            if row.sid in r.getMessage() and "deleted at your team's request" in r.getMessage()]
    assert _lines(caplog, row.sid) == [] and _retries(caplog, row.sid) == []
    journal = _journal(server)
    assert journal.is_deleted(row.sid) and journal.pending(row.sid) is None
    assert SOURCE_GONE_AT not in journal.get(row.sid)
    journal.close()
