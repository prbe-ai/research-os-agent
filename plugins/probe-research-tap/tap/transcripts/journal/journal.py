"""Protocol-2 and -3 source reservations and immutable delivery, shared with the tap.

Old tap processes cannot enumerate this namespace. One SQLite transaction owns
the reservation cursor, retained-event ordinal and pending body. The network is
outside the transaction. Acknowledgement and retiring pending bytes are atomic.
Pending bodies are never evicted on capacity pressure, authorization or poison.
The one exception is final by construction: the server saying the session was
deleted at its customer's request (`SessionDeleted`, `Journal.mark_deleted`).

PROTOCOL 3 is protocol 2 with each retained event also sent as an ATIF fragment
(`fragments`, built by the vendored engine module `atif_fragment`), and the
events themselves only when the server asked for them. Which protocol a stream
uses is settled once: a new stream starts on what the receipts answer offers
(`accepts`, see `negotiate`), the server pins it with the first accepted batch,
and from then on every batch of that stream uses it, whatever is offered later.
Once the server holds a session's stream on protocol 3, the session is kept
where taps that predate it cannot see it (`_FRAGMENT_SESSIONS`): those drain
every session in the journal, and would stage protocol-2 batches the server
refuses for a protocol-3 stream. Until then it stays where they see it, so that
one stream is all any tap on this machine starts for it.

Protocol 2 can be retired for NEW streams (the engine's
`SESSION_PROTOCOL2_NEW_STREAMS=false`). A session a tap staged on protocol 2
before that, and the server never took, is then refused at batch 0 for good:
the journal drops that batch and sends the session again, from its batch 0, on
protocol 3 (`Journal._restart_retired_protocol2`).

PASTED IMAGES (prbe-knowledge #646, research-os #2647). When the receipts answer
says `accepts.media` and this machine has not turned it off (the tap passes
`pasted_images`), an image the researcher pasted into their own prompt keeps
its placeholder (`{type, mimeType, bytes}`) plus the SHA-256 of its bytes, and
a protocol-3 fragment (version 2) names it as an image part. The bytes never
enter an event, a fragment or a batch: `stage` keeps them in this journal
(`media`, beside the pending batch that names them, under
MEDIA_SPOOL_CAP_BYTES) and `deliver` uploads each one through the gateway's
media door before it posts that batch. Which fragment version a stream builds
is negotiated like its protocol (`fragment_version_for`): the highest one the
server and this builder share, recorded in the session's state; a state that
predates it builds version 1.

A transcript line over MAX_SOURCE_LINE is skipped (read past, never held) and
stands as an `oversize_line` system event, so one giant paste no longer stalls
its session's capture.

PROBE OFF (`capture_gate`, tap 0.10.19). `stage` asks the gate first: a session
whose switch reads `off`, or that has an off stretch still open, gets no new
batch (one staged before the switch moved still goes); otherwise every line
that STARTS inside one of its off stretches is read past as nothing (no event,
no title, no oversize placeholder), so the server's cursor moves past it and
no process can ever send it.
"""

from __future__ import annotations
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from ...compose.compose import compose
from .delivery import DeliveryMixin
from .ensure import EnsureMixin
from .media.media import MediaUploadMixin
from .reconcile import ReconcileMixin
from .stage import StageMixin
from .stream_protocols.fragments import FragmentBuilderMixin, _FRAGMENT_SESSIONS
#: Beside a journal: the destination its folder's name is the hash of
#: (`{"base_url", "customer_id", "source"}`), so a reader can tell whose
#: receipts these are (the Probe service, `probe.service.receipts`).
DESTINATION_NAME = "destination.json"
DEFAULT_CAP_BYTES = 100 * 1024 * 1024


@compose
class Journal(
    FragmentBuilderMixin,
    MediaUploadMixin,
    EnsureMixin,
    StageMixin,
    DeliveryMixin,
    ReconcileMixin,
):
    def __init__(
        self,
        base_url: str,
        customer_id: str,
        source: str,
        *,
        directory: Path | None = None,
        cap_bytes: int = DEFAULT_CAP_BYTES,
    ):
        self.source = source
        self.customer_id = customer_id
        self.base_url = base_url.rstrip("/")
        self.cap_bytes = cap_bytes
        self._live_owners = {}
        self._in_transaction = False
        self._savepoints = 0
        namespace = hashlib.sha256(
            json.dumps([self.base_url, customer_id, source]).encode()
        ).hexdigest()
        self.directory = (directory or state_root()) / namespace
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._record_destination(source)
        self.db_path = self.directory / "state.sqlite3"
        self.conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=5)
        if self.conn.execute("PRAGMA user_version").fetchone()[0] not in (0, 2):
            self.conn.close()
            raise ReconciliationRequired("unsupported transcript journal version; state preserved")
        os.chmod(self.db_path, 0o600)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS {_FRAGMENT_SESSIONS}(session_id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pending(session_id TEXT PRIMARY KEY, body BLOB NOT NULL, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS media(
                session_id TEXT NOT NULL, sha256 TEXT NOT NULL, media_type TEXT NOT NULL,
                size INTEGER NOT NULL, bytes BLOB NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, first_failure REAL,
                PRIMARY KEY(session_id, sha256)
            );
        """)
        # `media` holds the bytes of the pasted images a session's pending
        # batch names, until `deliver` has uploaded them (or given them up).
        # A row never outlives its session's pending batch: every path that
        # retires one drops them (`_drop_media`), and `stage` sweeps rows an
        # older tap left behind when it delivered such a batch itself.
        # Still 2: taps that predate protocol 3 keep opening this journal for
        # their own protocol-2 streams, and never read the table above.
        self.conn.execute("PRAGMA user_version=2")

    def _record_destination(self, source: str) -> None:
        """Write `DESTINATION_NAME` once (0600, atomically): which backend,
        customer and agent this folder's name hashes. Best effort: a journal
        without it reads as one whose account is unknown."""
        path = self.directory / DESTINATION_NAME
        wanted = {"base_url": self.base_url, "customer_id": self.customer_id, "source": source}
        try:
            if json.loads(path.read_text(encoding="utf-8")) == wanted:
                return
        except (OSError, ValueError):
            pass
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        try:
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as handle:
                json.dump(wanted, handle, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError:
            with suppress(OSError):
                tmp.unlink()

    def close(self):
        self.conn.close()
        for handle in self._live_owners.values():
            handle.close()
        self._live_owners.clear()

    def _try_owner(self, session_id: str):
        # Kernel ownership survives PID reuse and disappears on SIGKILL. Never
        # unlink lock files: another process may still hold the old inode.
        name = hashlib.sha256(session_id.encode()).hexdigest() + ".owner.lock"
        handle = (self.directory / name).open("a+b")
        if not _lock_now(handle):
            handle.close()
            return None
        return handle

    def claim_live(self, session_id: str) -> bool:
        if session_id in self._live_owners:
            return True
        handle = self._try_owner(session_id)
        if handle is None:
            return False
        self._live_owners[session_id] = handle
        return True

    def holds_live(self, session_id: str) -> bool:
        """Does this journal hold the session's live claim (`claim_live`)?"""
        return session_id in self._live_owners

    def release_live(self, session_id: str) -> None:
        """Let go of the session's live claim, so another recorder can take it
        (the Probe service's, when `tap watch` hands the session to it:
        `tap/handback.py`). The lock file stays (see `_try_owner`)."""
        handle = self._live_owners.pop(session_id, None)
        if handle is not None:
            _unlock(handle)
            handle.close()

    def pin_orphan(self, session_id: str, remote: dict) -> bool:
        """Freeze an observed prefix only while no local live producer owns it.

        The caller also checks wrapper liveness and source quiet time. This
        lock closes the missing/stale PID-file race with compatible daemons.
        A resumed producer finishes this immutable prefix before its new tail.
        """
        handle = self._try_owner(session_id)
        if handle is None:
            return False
        try:
            state = self.get(session_id)
            if state is None:
                raise ReconciliationRequired("unknown orphan session")
            self.ensure(
                session_id,
                Path(state["path"]),
                remote,
                historical=True,
                complete_lines_only=True,
                continue_server_stream=True,
            )
            return True
        finally:
            handle.close()

    @contextmanager
    def transaction(self):
        if self._in_transaction:
            # Inside one this journal opened (`_rebuild_from_server` around
            # `ensure`): a savepoint, so a failure the outer block catches
            # undoes this block's writes only. The outermost one commits.
            self._savepoints += 1
            name = f"nested_{self._savepoints}"
            self.conn.execute(f"SAVEPOINT {name}")
            try:
                yield
                self.conn.execute(f"RELEASE {name}")
            except BaseException:
                if self.conn.in_transaction:  # not when SQLite already rolled it all back
                    self.conn.execute(f"ROLLBACK TO {name}")
                    self.conn.execute(f"RELEASE {name}")
                raise
            return
        self.conn.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise
        finally:
            self._in_transaction = False

    def get(self, session_id: str) -> dict | None:
        for table in (_FRAGMENT_SESSIONS, "sessions"):
            row = self.conn.execute(
                f"SELECT state FROM {table} WHERE session_id=?", (session_id,)
            ).fetchone()
            if row:
                return json.loads(row[0])
        return None

    def _save(self, state: dict):
        # By the PINNED protocol, never the offer (see _FRAGMENT_SESSIONS): a
        # session moves once, when the server is known to hold it on 3.
        table, other = (
            (_FRAGMENT_SESSIONS, "sessions")
            if state.get("protocol_version") == PROTOCOL_FRAGMENTS
            else ("sessions", _FRAGMENT_SESSIONS)
        )
        if table == _FRAGMENT_SESSIONS:
            self._keep_snapshot_from_older_taps(state)
        self.conn.execute(
            f"INSERT INTO {table}(session_id,state) VALUES(?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET state=excluded.state",
            (state["session_id"], json.dumps(state, separators=(",", ":"))),
        )
        self.conn.execute(f"DELETE FROM {other} WHERE session_id=?", (state["session_id"],))

    def _keep_snapshot_from_older_taps(self, state: dict) -> None:
        """Give a protocol-3 session's snapshot the name older taps never delete.

        An older tap that froze the session's history while it was still in
        its view (`pin_orphan`) named the copy `*.snapshot.jsonl`; once the
        session leaves its view, it deletes that copy as unreferenced. The same
        bytes get the protocol-3 name first: a hard link (no copy, atomic), or
        a durable copy where the filesystem refuses links. The old name is then
        unreferenced here too, and the next sweep removes it.
        """
        snapshot = state.get("snapshot_path")
        p2, p3 = _SNAPSHOT_SUFFIXES[PROTOCOL_EVENTS], _SNAPSHOT_SUFFIXES[PROTOCOL_FRAGMENTS]
        if not snapshot or not snapshot.endswith(p2):
            return
        source = Path(snapshot)
        if not source.exists():
            return  # already gone: `ensure` freezes the history again
        target = source.with_name(source.name[: -len(p2)] + p3)
        if not target.exists():
            try:
                os.link(source, target)
            except OSError:
                fd, temporary = tempfile.mkstemp(dir=self.directory)
                try:
                    with os.fdopen(fd, "wb") as copy, source.open("rb") as original:
                        shutil.copyfileobj(original, copy, 1024 * 1024)
                        copy.flush()
                        os.fsync(copy.fileno())
                    os.replace(temporary, target)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            _sync_directory(self.directory)
        state["snapshot_path"] = str(target)

    def is_deleted(self, session_id: str) -> bool:
        state = self.get(session_id)
        return bool(state and state.get("deleted"))

    def mark_deleted(self, session_id: str, path: Path | None = None) -> bool:
        """The server deleted this session: forget everything still owed to it.

        One transaction drops the pending batch and marks the session done
        (`deleted`, finalized, history complete, no live capture), so no path
        stages or sends it again; the local snapshot copy of its source is
        removed after the commit. Idempotent. Returns True only on the call
        that changed something, so a caller can log it once. The researcher's
        own transcript file is theirs and is never touched.
        """
        with self.transaction():
            state = self.get(session_id)
            dropped = self.conn.execute(
                "DELETE FROM pending WHERE session_id=?", (session_id,)
            ).rowcount + self._drop_media(session_id)
            if state is not None and state.get("deleted") and not dropped:
                return False
            snapshot = (state or {}).get("snapshot_path")
            if state is None:
                state = {
                    "session_id": session_id,
                    "stream_id": str(uuid.uuid4()),
                    "source_byte_end": 0,
                    "source_line_end": 0,
                    "event_end": 0,
                    "last_seq": -1,
                    "prefix_sha256": EMPTY_HASH,
                    "path": str(path) if path is not None else "",
                    "provenance": {},
                    "digest_state": "not_requested",
                }
            state.update(
                deleted=True,
                finalized=True,
                historical_complete=True,
                live_enabled=False,
                finalize_requested=False,
                snapshot_path=None,
                error=None,
            )
            self._save(state)
        if snapshot:
            Path(snapshot).unlink(missing_ok=True)
        return True

    def _drop_media(self, session_id: str) -> int:
        """Drop the pasted images waiting to upload for a session whose
        pending batch is going (acknowledged, dropped, or the session deleted)."""
        return self.conn.execute("DELETE FROM media WHERE session_id=?", (session_id,)).rowcount

    def _refuse_deleted(self, session_id: str) -> None:
        if self.is_deleted(session_id):
            raise SessionDeleted(session_id, newly=False)

    def _deleted_remote(self, session_id: str, remote: dict, path: Path | None = None) -> None:
        """Settle a receipts answer that says the server deleted the session."""
        if remote.get("state") == DELETED_STATE:
            raise SessionDeleted(session_id, newly=self.mark_deleted(session_id, path))

    def pending(self, session_id: str) -> bytes | None:
        row = self.conn.execute(
            "SELECT body FROM pending WHERE session_id=?", (session_id,)
        ).fetchone()
        return bytes(row[0]) if row else None

    def update(self, session_id: str, **values):
        with self.transaction():
            state = self.get(session_id)
            if state is None:
                raise ReconciliationRequired("unknown session")
            state.update(values)
            self._save(state)

    def release_snapshot(self, session_id: str):
        """Retain source proof, not another permanent raw transcript archive."""
        with self.transaction():
            state = self.get(session_id)
            if (
                state and state.get("historical_complete") and state.get("snapshot_path")
                and not self.pending(session_id)
            ):
                Path(state["snapshot_path"]).unlink(missing_ok=True)

    def sessions(self) -> list[dict]:
        found = {
            row[0]: json.loads(row[1])
            for table in ("sessions", _FRAGMENT_SESSIONS)  # a protocol-3 row wins, as in get()
            for row in self.conn.execute(f"SELECT session_id,state FROM {table}")
        }
        return list(found.values())
from .errors import DELETED_STATE, ReconciliationRequired, SessionDeleted  # noqa: E402 -- pieces need each other; the front door loads this one first
from .integrity import EMPTY_HASH  # noqa: E402 -- pieces need each other; the front door loads this one first
from .lock import _lock_now, _sync_directory, _unlock, state_root  # noqa: E402 -- pieces need each other; the front door loads this one first
from .stream_protocol import PROTOCOL_EVENTS, PROTOCOL_FRAGMENTS, _SNAPSHOT_SUFFIXES  # noqa: E402 -- pieces need each other; the front door loads this one first
