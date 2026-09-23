"""The Probe daemon's ledger: one SQLite file per session, the worker's memory.

    <state>/probe/companion/<session_id>.sqlite

It is the durable queue AND the audit trail. Every proposal is written here
BEFORE it is published (persist -> publish -> receipt), so a crash between the
two replays the same idempotency key instead of asking the model to author the
write again; the server's `Idempotency-Key` store makes that replay a no-op.

    proposed ──> held        (ungrounded / already recorded / not allowed / shadow)
        │
        └──────> pending ──> published   (2xx, or the server replayed it)
                    │
                    └──────> failed      (4xx the retry cannot fix)

`probe companion log | report | feedback` read and write this file directly
(the CLI cannot import the tap), so the format carries a version:
`meta.format_version`. A reader that finds a version it does not know must
refuse ("upgrade the CLI"), never guess; the writer never opens a file newer
than itself. Bump LEDGER_VERSION on a change an older reader or writer would
misread (a changed column, a changed meaning) and keep the CLI reader in
`probe/cli/companion.py` in step -- `agent/tests/test_companion_ledger_contract.py`
fails if they disagree. A NEW table older code never touches is additive and
keeps the version: bumping it would lock every released CLI out of every daemon
ledger until the next CLI release, and make a tap rollback refuse its own files.

`judgments` (additive) are the Jev judge's answers about each finished turn,
kept beside what the model then decided (SHADOW: they never skip a call).
`traces` (additive) keeps the exact conversation of every model round: the
request the worker sent (system prompt, session facts, transcript view) and the
answer or the error, zlib-compressed JSON, so nothing the daemon saw, asked or
answered is invisible afterwards (`probe companion trace <cycle>`). Bounded per
session: TRACE_KEEP_SECONDS and TRACE_KEEP_BYTES, oldest first; an existing file
gains both tables when a worker opens it.

`<state>/probe/companion/device.sqlite` is the device-wide spend ceiling, shared
by every session's worker.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

LEDGER_VERSION = 1

TRACE_KEEP_SECONDS = 7 * 24 * 3600
TRACE_KEEP_BYTES = 50 * 1024 * 1024

STATUS_HELD = "held"
STATUS_PENDING = "pending"
STATUS_PUBLISHED = "published"
STATUS_FAILED = "failed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idem_key TEXT NOT NULL UNIQUE,
    cycle INTEGER NOT NULL,
    kind TEXT NOT NULL,
    target_type TEXT,
    target_id TEXT,
    payload TEXT NOT NULL,
    evidence TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    response TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS proposals_status ON proposals(status);
CREATE TABLE IF NOT EXISTS boundaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    byte_offset INTEGER NOT NULL,
    from_writer TEXT NOT NULL,
    to_writer TEXT NOT NULL,
    reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cycles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    events INTEGER NOT NULL,
    outcome TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS seen_ids (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT,
    first_seen_offset INTEGER NOT NULL,
    context TEXT
);
CREATE TABLE IF NOT EXISTS directed (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    byte_offset INTEGER NOT NULL,
    command TEXT NOT NULL,
    UNIQUE (byte_offset, command)
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    proposal_id INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    note TEXT,
    consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS traces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle INTEGER,
    round INTEGER NOT NULL,
    at REAL NOT NULL,
    request BLOB NOT NULL,
    response BLOB,
    error TEXT,
    bytes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS traces_cycle ON traces(cycle);
CREATE INDEX IF NOT EXISTS traces_bytes ON traces(bytes);
CREATE TABLE IF NOT EXISTS judgments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    purpose TEXT NOT NULL,
    byte_start INTEGER NOT NULL,
    byte_end INTEGER NOT NULL,
    questions TEXT NOT NULL,
    answers TEXT,
    model TEXT,
    error TEXT,
    elapsed_ms INTEGER
);
"""


class LedgerVersionError(RuntimeError):
    """The file was written by a newer worker than this one."""


def companion_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "probe" / "companion"


def ledger_path(session_id: str) -> Path:
    return companion_dir() / f"{session_id}.sqlite"


@dataclass
class Proposal:
    id: int
    idem_key: str
    kind: str
    target_type: str | None
    target_id: str | None
    payload: dict
    evidence: dict
    status: str
    reason: str | None
    attempts: int
    updated_at: float = 0.0


def _connect(path: Path) -> sqlite3.Connection:
    # Private: the ledger holds transcript-derived text (proposals, evidence).
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.exists():
        path.touch(mode=0o600)
    conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


class Ledger:
    def __init__(self, session_id: str, path: Path | None = None) -> None:
        self.session_id = session_id
        self.path = path or ledger_path(session_id)
        self.conn = _connect(self.path)
        self.conn.executescript(_SCHEMA)
        found = self._meta("format_version")
        if found is None:
            self._set_meta("format_version", str(LEDGER_VERSION))
            self._set_meta("session_id", session_id)
            self._set_meta("created_at", repr(time.time()))
        elif int(found) > LEDGER_VERSION:
            self.conn.close()
            raise LedgerVersionError(
                f"{self.path} is ledger format {found}; this worker writes {LEDGER_VERSION}"
            )
        else:
            self._prune_traces(time.time())

    def close(self) -> None:
        self.conn.close()

    # -- meta ---------------------------------------------------------------
    def _meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def watermark(self) -> int:
        """Transcript bytes the worker has finished deciding about."""
        value = self._meta("watermark")
        return int(value) if value is not None else 0

    def set_watermark(self, offset: int) -> None:
        self._set_meta("watermark", str(int(offset)))

    @property
    def boundary(self) -> int | None:
        """Where the daemon's authorship begins (the handover offset), or None."""
        value = self._meta("boundary_offset")
        return int(value) if value is not None else None

    def get_json(self, key: str, default: Any = None) -> Any:
        value = self._meta(key)
        return default if value is None else json.loads(value)

    def set_json(self, key: str, value: Any) -> None:
        self._set_meta(key, json.dumps(value))

    # -- boundaries ---------------------------------------------------------
    def record_boundary(self, *, byte_offset: int, from_writer: str, to_writer: str, reason: str) -> None:
        with self.transaction():
            self.conn.execute(
                "INSERT INTO boundaries(at, byte_offset, from_writer, to_writer, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (time.time(), int(byte_offset), from_writer, to_writer, reason),
            )
            if to_writer == "daemon":
                self._set_meta("boundary_offset", str(int(byte_offset)))
            self._set_meta("writer", to_writer)

    @property
    def writer(self) -> str | None:
        return self._meta("writer")

    # -- seen ids and directed writes ---------------------------------------
    def remember_ids(self, found: dict[str, tuple[str | None, int, str]]) -> None:
        """`{entity_id: (entity_type, offset, context)}`, first sighting wins."""
        self.conn.executemany(
            "INSERT OR IGNORE INTO seen_ids(entity_id, entity_type, first_seen_offset, context) "
            "VALUES (?, ?, ?, ?)",
            [(eid, etype, off, ctx[:300]) for eid, (etype, off, ctx) in found.items()],
        )

    def seen_contexts(self) -> dict[str, str | None]:
        """Where each id was first seen: the command, or the Probe tool's name."""
        return dict(self.conn.execute("SELECT entity_id, context FROM seen_ids"))

    def seen_ids(self) -> dict[str, str | None]:
        return dict(
            self.conn.execute(
                "SELECT entity_id, entity_type FROM seen_ids ORDER BY first_seen_offset"
            )
        )

    def record_directed(self, commands: list[tuple[int, str]]) -> None:
        # OR IGNORE: a chunk re-read after a failed cycle must not count twice.
        self.conn.executemany(
            "INSERT OR IGNORE INTO directed(at, byte_offset, command) VALUES (?, ?, ?)",
            [(time.time(), off, cmd[:500]) for off, cmd in commands],
        )

    # -- cycles -------------------------------------------------------------
    def start_cycle(self, *, byte_start: int, byte_end: int, events: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO cycles(started_at, byte_start, byte_end, events) VALUES (?, ?, ?, ?)",
            (time.time(), byte_start, byte_end, events),
        )
        return int(cur.lastrowid)

    def finish_cycle(self, cycle: int, *, outcome: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
        self.conn.execute(
            "UPDATE cycles SET finished_at = ?, outcome = ?, input_tokens = ?, output_tokens = ? "
            "WHERE id = ?",
            (time.time(), outcome, input_tokens, output_tokens, cycle),
        )

    # -- traces -------------------------------------------------------------
    def add_trace(self, *, cycle: int | None, round: int, request: Any, response: Any = None,
                  error: str | None = None) -> None:
        """One model round, exactly as sent and received (the caller redacts)."""
        req = zlib.compress(json.dumps(request, default=str).encode("utf-8"))
        resp = zlib.compress(json.dumps(response, default=str).encode("utf-8")) if response is not None else None
        size = len(req) + len(resp or b"")
        now = time.time()
        with self.transaction():
            self.conn.execute(
                "INSERT INTO traces(cycle, round, at, request, response, error, bytes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (cycle, round, now, req, resp, error, size),
            )
            self._prune_traces(now)

    def _prune_traces(self, now: float) -> None:
        """Older than TRACE_KEEP_SECONDS, then oldest first past TRACE_KEEP_BYTES
        (also run when a worker opens the file, so a finished session's ledger
        does not keep a week of traces for the ledger's whole retention)."""
        self.conn.execute("DELETE FROM traces WHERE at < ?", (now - TRACE_KEEP_SECONDS,))
        # `bytes` is indexed: the sum reads the index, not the BLOB pages.
        total = self.conn.execute("SELECT COALESCE(SUM(bytes), 0) FROM traces").fetchone()[0]
        while total > TRACE_KEEP_BYTES:
            row = self.conn.execute("SELECT id, bytes FROM traces ORDER BY id LIMIT 1").fetchone()
            if row is None:
                break
            self.conn.execute("DELETE FROM traces WHERE id = ?", (row[0],))
            total -= row[1]

    def traces(self, cycle: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT round, at, request, response, error FROM traces WHERE cycle = ? ORDER BY id", (cycle,)
        ).fetchall()
        return [
            {"round": r[0], "at": r[1], "request": json.loads(zlib.decompress(r[2])),
             "response": json.loads(zlib.decompress(r[3])) if r[3] is not None else None, "error": r[4]}
            for r in rows
        ]

    # -- judgments ----------------------------------------------------------
    def add_judgment(self, *, purpose: str, byte_start: int, byte_end: int, questions: dict,
                     answers: dict | None = None, model: str | None = None, error: str | None = None,
                     elapsed_ms: int | None = None) -> None:
        self.conn.execute(
            "INSERT INTO judgments(at, purpose, byte_start, byte_end, questions, answers, model, error, elapsed_ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), purpose, byte_start, byte_end, json.dumps(questions),
             json.dumps(answers) if answers is not None else None, model, error, elapsed_ms),
        )

    def judgments(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT purpose, byte_start, byte_end, questions, answers, model, error, elapsed_ms FROM judgments "
            "ORDER BY id"
        ).fetchall()
        return [
            {"purpose": r[0], "byte_start": r[1], "byte_end": r[2], "questions": json.loads(r[3]),
             "answers": json.loads(r[4]) if r[4] else None, "model": r[5], "error": r[6], "elapsed_ms": r[7]}
            for r in rows
        ]

    # -- proposals ----------------------------------------------------------
    def add_proposal(
        self,
        *,
        idem_key: str,
        cycle: int,
        kind: str,
        target_type: str | None,
        target_id: str | None,
        payload: dict,
        evidence: dict,
        status: str,
        reason: str | None = None,
    ) -> bool:
        """Persist one decision; False when this key was already decided."""
        now = time.time()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO proposals(idem_key, cycle, kind, target_type, target_id, "
            "payload, evidence, status, reason, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                idem_key,
                cycle,
                kind,
                target_type,
                target_id,
                json.dumps(payload),
                json.dumps(evidence),
                status,
                reason,
                now,
                now,
            ),
        )
        return cur.rowcount == 1

    def pending(self) -> list[Proposal]:
        return [
            self._proposal(row)
            for row in self.conn.execute(
                "SELECT id, idem_key, kind, target_type, target_id, payload, evidence, status, "
                "reason, attempts, updated_at FROM proposals WHERE status = ? ORDER BY id",
                (STATUS_PENDING,),
            )
        ]

    def mark(self, proposal_id: int, status: str, *, reason: str | None = None, response: Any = None) -> None:
        self.conn.execute(
            "UPDATE proposals SET status = ?, reason = ?, response = ?, attempts = attempts + 1, "
            "updated_at = ? WHERE id = ?",
            (
                status,
                reason,
                None if response is None else json.dumps(response)[:4000],
                time.time(),
                proposal_id,
            ),
        )

    def bump_attempt(self, proposal_id: int, reason: str) -> None:
        self.conn.execute(
            "UPDATE proposals SET attempts = attempts + 1, reason = ?, updated_at = ? WHERE id = ?",
            (reason, time.time(), proposal_id),
        )

    def published_count(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) FROM proposals WHERE status = ?", (STATUS_PUBLISHED,)
        ).fetchone()
        return int(row[0])

    def committed_writes(self, *, kind: str | None = None, exclude_kind: str | None = None) -> int:
        """Writes published or waiting to be: what the per-session caps count
        (file notes have a cap of their own, so they never crowd out a note)."""
        sql = "SELECT COUNT(*) FROM proposals WHERE status IN (?, ?)"
        args: list = [STATUS_PUBLISHED, STATUS_PENDING]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(kind)
        if exclude_kind is not None:
            sql += " AND kind != ?"
            args.append(exclude_kind)
        return int(self.conn.execute(sql, args).fetchone()[0])

    def hold_pending(self, reason: str) -> int:
        cur = self.conn.execute(
            "UPDATE proposals SET status = ?, reason = ?, updated_at = ? WHERE status = ?",
            (STATUS_HELD, reason, time.time(), STATUS_PENDING),
        )
        return cur.rowcount

    def recent_decisions(self, limit: int = 40) -> list[dict]:
        """What the worker already decided, for the next prompt: never re-propose."""
        rows = self.conn.execute(
            "SELECT kind, target_type, target_id, payload, status FROM proposals "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "kind": kind,
                "target": f"{ttype}:{tid}" if tid else None,
                "status": status,
                "summary": _summary(json.loads(payload)),
            }
            for kind, ttype, tid, payload, status in reversed(rows)
        ]

    def unconsumed_feedback(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT f.id, f.verdict, f.note, p.kind, p.payload FROM feedback f "
            "JOIN proposals p ON p.id = f.proposal_id WHERE f.consumed = 0 ORDER BY f.id"
        ).fetchall()
        return [
            {
                "feedback_id": fid,
                "verdict": verdict,
                "note": note,
                "kind": kind,
                "summary": _summary(json.loads(payload)),
            }
            for fid, verdict, note, kind, payload in rows
        ]

    def consume_feedback(self, ids: list[int]) -> None:
        self.conn.executemany("UPDATE feedback SET consumed = 1 WHERE id = ?", [(i,) for i in ids])

    @staticmethod
    def _proposal(row: tuple) -> Proposal:
        pid, key, kind, ttype, tid, payload, evidence, status, reason, attempts, updated_at = row
        return Proposal(
            id=pid,
            idem_key=key,
            kind=kind,
            target_type=ttype,
            target_id=tid,
            payload=json.loads(payload),
            evidence=json.loads(evidence),
            status=status,
            reason=reason,
            attempts=attempts,
            updated_at=updated_at,
        )

    # -- transactions -------------------------------------------------------
    def transaction(self) -> "_Tx":
        return _Tx(self.conn)


class _Tx:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def __enter__(self) -> sqlite3.Connection:
        self.conn.execute("BEGIN IMMEDIATE")
        return self.conn

    def __exit__(self, exc_type, exc, tb) -> None:
        self.conn.execute("ROLLBACK" if exc_type else "COMMIT")


def _summary(payload: dict) -> str:
    for key in ("title", "name", "description", "tags", "source_url", "relation", "status"):
        value = payload.get(key)
        if value:
            text = value if isinstance(value, str) else json.dumps(value)
            return f"{key}={text[:120]}"
    return json.dumps(payload)[:120]


# ---------------------------------------------------------------------------
# DEVICE-WIDE SPEND. One file for every session on the machine, so ten parallel
# sessions cannot each spend a full day's ceiling.
# ---------------------------------------------------------------------------

#: Output+input tokens one device may spend through the gateway per UTC day.
#: A local ceiling, enforced here and never raised by a server reply; the
#: server's LiteLLM key budget is the backstop.
DEFAULT_DAILY_TOKEN_CEILING = 3_000_000
ENV_DAILY_TOKEN_CEILING = "PROBE_COMPANION_DAILY_TOKENS"


def daily_ceiling() -> int:
    raw = os.environ.get(ENV_DAILY_TOKEN_CEILING, "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_DAILY_TOKEN_CEILING
    return value if value > 0 else DEFAULT_DAILY_TOKEN_CEILING


class DeviceSpend:
    def __init__(self, path: Path | None = None) -> None:
        self.conn = _connect(path or companion_dir() / "device.sqlite")
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS spend (day TEXT PRIMARY KEY, tokens INTEGER NOT NULL)"
        )

    @staticmethod
    def _day(now: float | None = None) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(time.time() if now is None else now))

    def spent_today(self, now: float | None = None) -> int:
        row = self.conn.execute(
            "SELECT tokens FROM spend WHERE day = ?", (self._day(now),)
        ).fetchone()
        return int(row[0]) if row else 0

    def exhausted(self, now: float | None = None) -> bool:
        return self.spent_today(now) >= daily_ceiling()

    def add(self, tokens: int, now: float | None = None) -> None:
        self.conn.execute(
            "INSERT INTO spend(day, tokens) VALUES (?, ?) "
            "ON CONFLICT(day) DO UPDATE SET tokens = tokens + excluded.tokens",
            (self._day(now), max(0, int(tokens))),
        )

    def close(self) -> None:
        self.conn.close()


def iter_ledgers() -> Iterator[Path]:
    root = companion_dir()
    if not root.is_dir():
        return iter(())
    return (p for p in sorted(root.glob("*.sqlite")) if p.name != "device.sqlite")


#: Session ledgers (and their -wal/-shm/.lock siblings) untouched this long are deleted.
LEDGER_RETENTION_SECONDS = 30 * 86400


def prune(now: float | None = None) -> int:
    """Delete session ledgers nobody has written for LEDGER_RETENTION_SECONDS."""
    cutoff = (time.time() if now is None else now) - LEDGER_RETENTION_SECONDS
    removed = 0
    for path in list(iter_ledgers()):
        try:
            if path.stat().st_mtime >= cutoff:
                continue
            for sibling in (path, path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm"),
                            path.with_suffix(".lock")):
                sibling.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    return removed
