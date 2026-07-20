"""sqlite storage: file_offsets, outbox, meta.

The Python tail uses byte cursors (byte_offset on file_offsets) for
partial-line safety while last_line_no remains a counter for the line_no
field in event payloads.

WAL + 5s busy_timeout; sqlite3 is single-process here (one daemon per
session), but WAL is cheap and saves us if anyone reads state.db
concurrently.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS file_offsets (
    path           TEXT PRIMARY KEY,
    session_id     TEXT NOT NULL,
    cwd            TEXT NOT NULL,
    last_line_no   INTEGER NOT NULL,
    last_seen_at   INTEGER NOT NULL,
    inode          INTEGER,
    size           INTEGER NOT NULL,
    byte_offset    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    batch_seq       INTEGER NOT NULL,
    cwd             TEXT NOT NULL,
    body_json       BLOB NOT NULL,
    created_at      INTEGER NOT NULL,
    next_attempt_at INTEGER NOT NULL,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    UNIQUE(session_id, batch_seq)
);

CREATE INDEX IF NOT EXISTS outbox_next_attempt ON outbox(next_attempt_at);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""

DEFAULT_OUTBOX_CAP_BYTES = 100 * 1024 * 1024


@dataclass
class OutboxRow:
    id: int
    session_id: str
    batch_seq: int
    cwd: str
    body: bytes
    created_at: int
    next_attempt_at: int
    attempt_count: int
    last_error: str


@dataclass
class FileOffset:
    path: str
    session_id: str
    cwd: str
    last_line_no: int
    last_seen_at: int
    inode: int
    size: int
    byte_offset: int


class Storage:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None = autocommit; we issue explicit BEGINs only if
        # we ever batch writes. Simpler reasoning at this scale.
        self._conn = sqlite3.connect(str(db_path), timeout=5.0, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA_SQL)

    def close(self) -> None:
        self._conn.close()

    # ----- offsets -----

    def upsert_offset(self, f: FileOffset) -> None:
        self._conn.execute(
            """
            INSERT INTO file_offsets(path, session_id, cwd, last_line_no,
                                     last_seen_at, inode, size, byte_offset)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                session_id=excluded.session_id,
                cwd=excluded.cwd,
                last_line_no=excluded.last_line_no,
                last_seen_at=excluded.last_seen_at,
                inode=excluded.inode,
                size=excluded.size,
                byte_offset=excluded.byte_offset
            """,
            (f.path, f.session_id, f.cwd, f.last_line_no, f.last_seen_at,
             f.inode, f.size, f.byte_offset),
        )

    def get_offset(self, path: str) -> FileOffset | None:
        row = self._conn.execute(
            """SELECT path, session_id, cwd, last_line_no, last_seen_at,
                      COALESCE(inode, 0), size, byte_offset
               FROM file_offsets WHERE path=?""",
            (path,),
        ).fetchone()
        if row is None:
            return None
        return FileOffset(*row)

    # ----- outbox -----

    def enqueue_batch(self, *, session_id: str, batch_seq: int, cwd: str,
                      body: bytes, created_at: int, next_attempt_at: int) -> None:
        self._conn.execute(
            """INSERT INTO outbox(session_id, batch_seq, cwd, body_json,
                                  created_at, next_attempt_at)
               VALUES(?, ?, ?, ?, ?, ?)""",
            (session_id, batch_seq, cwd, body, created_at, next_attempt_at),
        )

    def next_due_batch(self, now: int, session_id: str) -> OutboxRow | None:
        """Return the oldest due row owned by session_id.

        Scoping to session_id prevents two concurrent daemons (one per CC
        session) from racing on the same row and double-shipping it. Orphan
        rows from crashed sessions whose ID is never reused stay queued until
        enforce_outbox_cap reaps them.
        """
        row = self._conn.execute(
            """SELECT id, session_id, batch_seq, cwd, body_json, created_at,
                      next_attempt_at, attempt_count, COALESCE(last_error, '')
               FROM outbox WHERE next_attempt_at <= ? AND session_id = ?
               ORDER BY id ASC LIMIT 1""",
            (now, session_id),
        ).fetchone()
        if row is None:
            return None
        return OutboxRow(*row)

    def mark_success(self, row_id: int) -> None:
        self._conn.execute("DELETE FROM outbox WHERE id=?", (row_id,))

    def mark_failure(self, row_id: int, next_attempt_at: int, msg: str) -> None:
        self._conn.execute(
            """UPDATE outbox
               SET attempt_count = attempt_count + 1,
                   next_attempt_at = ?,
                   last_error = ?
               WHERE id = ?""",
            (next_attempt_at, msg, row_id),
        )

    def clear_outbox(self) -> int:
        cur = self._conn.execute("DELETE FROM outbox")
        return cur.rowcount or 0

    def outbox_row_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM outbox").fetchone()
        return int(row[0]) if row else 0

    def outbox_byte_size(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(LENGTH(body_json)), 0) FROM outbox"
        ).fetchone()
        return int(row[0]) if row else 0

    def enforce_outbox_cap(self, max_bytes: int = DEFAULT_OUTBOX_CAP_BYTES) -> int:
        """Drop oldest rows until total body bytes <= max_bytes. Returns count dropped."""
        total = self.outbox_byte_size()
        if total <= max_bytes:
            return 0
        rows = self._conn.execute(
            "SELECT id, LENGTH(body_json) FROM outbox ORDER BY id ASC"
        ).fetchall()
        dropped = 0
        for row_id, size in rows:
            self._conn.execute("DELETE FROM outbox WHERE id=?", (row_id,))
            dropped += 1
            total -= int(size)
            if total <= max_bytes:
                break
        return dropped

    def max_batch_seq(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT MAX(batch_seq) FROM outbox WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None or row[0] is None:
            return -1
        return int(row[0])

    # ----- meta -----

    def set_meta(self, k: str, v: str) -> None:
        self._conn.execute(
            "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (k, v),
        )

    def insert_meta_if_absent(self, k: str, v: str) -> str:
        """Atomically claim meta[k]=v iff absent; return the WINNING value.

        `INSERT ... ON CONFLICT(k) DO NOTHING` is a single atomic statement, so
        two concurrent daemons that each mint a different value for the same key
        converge on ONE: the first writer wins, the second's insert no-ops, and
        the read returns the winner for both. Replaces the read-check-write mint
        that let two same-minute daemons fork machine identity (last-writer-wins).
        """
        self._conn.execute(
            "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO NOTHING",
            (k, v),
        )
        return self.get_meta(k)

    def set_meta_pair(self, k1: str, v1: str, k2: str, v2: str) -> None:
        """Upsert two meta rows inside one explicit transaction.

        The connection is autocommit (isolation_level=None), so each bare execute
        commits on its own — a crash between two set_meta calls can split a pair
        that must move together (e.g. the 401 latch's timestamp + fingerprint).
        An explicit BEGIN/COMMIT makes the pair all-or-nothing.
        """
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k1, v1),
            )
            self._conn.execute(
                "INSERT INTO meta(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k2, v2),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def get_meta(self, k: str) -> str:
        row = self._conn.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
        return row[0] if row else ""

    def delete_meta(self, k: str) -> None:
        self._conn.execute("DELETE FROM meta WHERE k=?", (k,))
