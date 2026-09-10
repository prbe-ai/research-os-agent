"""Protocol-2 source reservations and immutable delivery, shared with the tap.

Old tap processes cannot enumerate this namespace. One SQLite transaction owns
the reservation cursor, retained-event ordinal and pending body. The network is
outside the transaction. Acknowledgement and retiring pending bytes are atomic.
Pending bodies are never evicted on capacity pressure, authorization or poison.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import socket
import sqlite3
import ssl
import tempfile
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from pathlib import Path

from . import codex_sanitize, pi_sanitize, sanitize

PROTOCOL_VERSION = 2
EMPTY_HASH = hashlib.sha256(b"").hexdigest()
MAX_BODY_BYTES = 1024 * 1024
MAX_SOURCE_LINE = 64 * 1024 * 1024
DEFAULT_CAP_BYTES = 100 * 1024 * 1024
_FIELDS = (
    "session_id",
    "batch_seq",
    "cwd",
    "events",
    "finalize",
    "protocol_version",
    "stream_id",
    "source_byte_start",
    "source_byte_end",
    "source_line_start",
    "source_line_end",
    "event_start",
    "event_end",
    "prefix_sha256",
    "provenance",
    "snapshot_byte_end",
    "snapshot_sha256",
)
_SANITIZERS = {
    "claude_code": sanitize.sanitize_event,
    "codex": codex_sanitize.sanitize_event,
    "pi": pi_sanitize.sanitize_event,
}
_ROUTES = {"claude_code": "claude-code", "codex": "codex", "pi": "pi"}


class ReconciliationRequired(RuntimeError):
    pass


class DeliveryPending(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.retryable = retryable


def _retryable_status(code: int) -> bool:
    return code in (408, 429) or 500 <= code < 600


def _network_interruption(exc: Exception) -> bool:
    """Only connectivity failures; malformed responses and local files stay errors."""
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, Exception):
        return _network_interruption(exc.reason)
    if isinstance(exc, ssl.SSLError):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, socket.gaierror)):
        return True
    return isinstance(exc, OSError) and exc.errno in {
        errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTDOWN, errno.EHOSTUNREACH,
        errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT,
        errno.EPIPE,
    }


def canonical_payload(payload: dict) -> bytes:
    return json.dumps(
        {k: payload[k] for k in _FIELDS if k in payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def prefix_hash(path: Path, end: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        left = end
        while left:
            chunk = handle.read(min(left, 1024 * 1024))
            if not chunk:
                raise ReconciliationRequired(
                    "source truncated inside an acknowledged or pending range"
                )
            digest.update(chunk)
            left -= len(chunk)
    return digest.hexdigest()


def complete_line_end(path: Path, end: int) -> int:
    """Find the last complete source record without loading a partial giant line."""
    with path.open("rb") as handle:
        while end:
            start = max(0, end - 64 * 1024)
            handle.seek(start)
            tail = handle.read(end - start)
            newline = tail.rfind(b"\n")
            if newline >= 0:
                return start + newline + 1
            end = start
    return 0


def state_root() -> Path:
    return Path(
        os.environ.get("PROBE_TRANSCRIPT_STATE_DIR", "~/.probe/transcripts-v2")
    ).expanduser()


class Wire:
    """The existing ingest routes and receipt read, using the paired credential."""

    def __init__(self, base_url: str, token: str, source: str, timeout: float = 30):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.source = source
        self.timeout = timeout
        self.last_request_retryable = False

    def _request(self, path: str, body: bytes | None = None) -> tuple[int, dict]:
        self.last_request_retryable = False
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "probe-transcript/2",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read())
                if not isinstance(body, dict):
                    raise ValueError("transcript endpoint returned a non-object response")
                return response.status, body
        except urllib.error.HTTPError as exc:
            self.last_request_retryable = _retryable_status(exc.code)
            try:
                return exc.code, json.loads(exc.read(8192))
            except ValueError:
                return exc.code, {"detail": "invalid error response"}
        except (OSError, ValueError) as exc:
            self.last_request_retryable = _network_interruption(exc)
            return 0, {"detail": str(exc)}

    def receipts(self, session_id: str) -> dict:
        code, body = self._request(f"/ingest/v1/sessions/{self.source}/{session_id}/receipts")
        if code != 200 or body.get("protocol_version") != 2:
            raise DeliveryPending(
                f"protocol 2 receipts unavailable (http {code}); nothing newly staged",
                retryable=_retryable_status(code) or (code == 0 and self.last_request_retryable),
            )
        if (
            body.get("session_id") != session_id
            or body.get("source") != self.source
            or not body.get("customer_id")
        ):
            raise ReconciliationRequired("receipt identity does not match the requested transcript")
        return body

    def post(self, body: bytes) -> tuple[int, dict]:
        return self._request(f"/ingest/v1/sessions/{_ROUTES[self.source]}", body)


class Journal:
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
        namespace = hashlib.sha256(
            json.dumps([self.base_url, customer_id, source]).encode()
        ).hexdigest()
        self.directory = (directory or state_root()) / namespace
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.directory / "state.sqlite3"
        self.conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=5)
        if self.conn.execute("PRAGMA user_version").fetchone()[0] not in (0, 2):
            self.conn.close()
            raise ReconciliationRequired("unsupported transcript journal version; state preserved")
        os.chmod(self.db_path, 0o600)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pending(session_id TEXT PRIMARY KEY, body BLOB NOT NULL, digest TEXT NOT NULL);
        """)
        self.conn.execute("PRAGMA user_version=2")

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
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
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
            )
            return True
        finally:
            handle.close()

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def get(self, session_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT state FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, state: dict):
        self.conn.execute(
            "INSERT INTO sessions(session_id,state) VALUES(?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET state=excluded.state",
            (state["session_id"], json.dumps(state, separators=(",", ":"))),
        )

    def pending(self, session_id: str) -> bytes | None:
        row = self.conn.execute(
            "SELECT body FROM pending WHERE session_id=?", (session_id,)
        ).fetchone()
        return bytes(row[0]) if row else None

    def _validate_source(self, state: dict, path: Path):
        body = self.pending(state["session_id"])
        end = json.loads(body)["source_byte_end"] if body else state["source_byte_end"]
        expected = json.loads(body)["prefix_sha256"] if body else state["prefix_sha256"]
        if prefix_hash(path, end) != expected:
            raise ReconciliationRequired("source changed inside an acknowledged or pending range")

    def ensure(
        self,
        session_id: str,
        path: Path,
        remote: dict,
        *,
        historical: bool,
        provenance: dict | None = None,
        cwd: str | None = None,
        complete_lines_only: bool = False,
    ) -> dict:
        if (remote.get("customer_id"), remote.get("source"), remote.get("session_id")) != (
            self.customer_id,
            self.source,
            session_id,
        ):
            raise ReconciliationRequired(
                "refusing adoption across transcript destination identities"
            )
        if remote.get("state") == "legacy":
            raise ReconciliationRequired(
                "legacy transcript coverage is unverified; reconcile before replay"
            )
        with self.transaction():
            # A process may die between snapshot fsync and the SQLite commit.
            # The write lock proves no other producer is still creating these.
            referenced = {item.get("snapshot_path") for item in self.sessions()}
            for orphan in self.directory.glob("*.snapshot.jsonl"):
                if str(orphan) not in referenced:
                    orphan.unlink(missing_ok=True)
            for orphan in self.directory.glob("tmp*"):
                if orphan.is_file():
                    orphan.unlink(missing_ok=True)
            state = self.get(session_id)
            if state is None:
                stream = remote.get("stream") or {}
                state = {
                    "session_id": session_id,
                    "stream_id": stream.get("stream_id") or str(uuid.uuid4()),
                    "source_byte_end": stream.get("source_byte_end", 0),
                    "source_line_end": stream.get("source_line_end", 0),
                    "event_end": stream.get("event_end", 0),
                    "last_seq": stream.get("last_seq", -1),
                    "prefix_sha256": stream.get("prefix_sha256", EMPTY_HASH),
                    "finalized": bool(stream.get("finalized")),
                    "path": str(path),
                    "provenance": provenance or {},
                    "digest_state": "not_requested",
                    "error": None,
                }
                self._validate_source(state, path)
            else:
                self._validate_source(state, path)
                stream = remote.get("stream")
                if stream and stream["stream_id"] != state["stream_id"]:
                    raise ReconciliationRequired(
                        "server stream changed; existing pending bytes retained"
                    )
                if (
                    stream
                    and stream["last_seq"] > state["last_seq"]
                    and not self.pending(session_id)
                ):
                    # Another compatible device continued this exact source. Receipt adoption
                    # requires matching its acknowledged raw prefix, not cwd/name similarity.
                    if prefix_hash(path, stream["source_byte_end"]) != stream["prefix_sha256"]:
                        raise ReconciliationRequired(
                            "remote stream advanced over a different source prefix"
                        )
                    state.update(
                        {
                            k: stream[k]
                            for k in (
                                "last_seq",
                                "source_byte_end",
                                "source_line_end",
                                "event_end",
                                "prefix_sha256",
                                "finalized",
                            )
                        }
                    )
                state["path"] = str(path)
            if not historical:
                state["live_enabled"] = True
                state["finalize_requested"] = False
            if not state.get("cwd"):
                pending = self.pending(session_id)
                pending_cwd = json.loads(pending).get("cwd") if pending else None
                for candidate in (pending_cwd, cwd, (provenance or {}).get("source_cwd")):
                    if isinstance(candidate, str) and Path(candidate).is_absolute():
                        state["cwd"] = candidate
                        break
            remote_stream = remote.get("stream") or {}
            remote_snapshot = (
                remote_stream.get("snapshot_byte_end")
                if not remote_stream.get("finalized")
                else None
            )
            freeze_history = historical or remote_snapshot is not None
            extended = (
                historical
                and state.get("historical_complete")
                and path.stat().st_size > state.get("historical_end", 0)
            )
            if freeze_history and (
                not state.get("snapshot_path")
                or not Path(state["snapshot_path"]).exists()
                or extended
            ):
                before = path.stat()
                snapshot_end = remote_snapshot if remote_snapshot is not None else before.st_size
                if complete_lines_only and remote_snapshot is None:
                    # A killed producer may leave half a JSONL record. Freeze
                    # only complete records, so the original partial tail can
                    # be completed and consumed after this prefix is finalized.
                    snapshot_end = complete_line_end(path, snapshot_end)
                    pending = self.pending(session_id)
                    pending_end = json.loads(pending)["source_byte_end"] if pending else 0
                    if snapshot_end < max(state["source_byte_end"], pending_end):
                        raise ReconciliationRequired(
                            "complete source prefix ends before an acknowledged or pending cursor"
                        )
                occupied = sum(p.stat().st_size for p in self.directory.glob("*.snapshot.jsonl"))
                if occupied + snapshot_end + MAX_BODY_BYTES > self.cap_bytes:
                    raise DeliveryPending("transcript snapshot capacity reached; source retained")
                snapshot = (
                    self.directory / f"{state['stream_id']}-{uuid.uuid4().hex}.snapshot.jsonl"
                )
                fd, temporary = tempfile.mkstemp(dir=self.directory)
                try:
                    with os.fdopen(fd, "wb") as target, path.open("rb") as source:
                        left = snapshot_end
                        while left:
                            chunk = source.read(min(left, 1024 * 1024))
                            if not chunk:
                                raise ReconciliationRequired(
                                    "source truncated while freezing historical prefix"
                                )
                            target.write(chunk)
                            left -= len(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                    after = path.stat()
                    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                    ):
                        raise ReconciliationRequired(
                            "transcript changed while freezing its historical prefix"
                        )
                    snapshot_hash = prefix_hash(Path(temporary), snapshot_end)
                    if remote_snapshot is not None and snapshot_hash != remote_stream.get(
                        "snapshot_sha256"
                    ):
                        raise ReconciliationRequired(
                            "copied source differs from the remote historical snapshot"
                        )
                    os.replace(temporary, snapshot)
                    directory_fd = os.open(self.directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    Path(temporary).unlink(missing_ok=True)
                state["snapshot_path"] = str(snapshot)
                state["historical_end"] = snapshot.stat().st_size
                state["historical_hash"] = snapshot_hash
                state["historical_complete"] = bool(
                    state["finalized"] and state["source_byte_end"] >= state["historical_end"]
                )
                if extended:
                    state.update(digest_state="not_requested", pending_digest=None)
            self._save(state)
            return state

    def stage(
        self,
        session_id: str,
        *,
        cwd: str,
        finalize: bool = False,
        historical_only: bool = False,
        max_body_bytes: int = MAX_BODY_BYTES,
    ) -> bytes | None:
        with self.transaction():
            state = self.get(session_id)
            if state is None:
                raise ReconciliationRequired("session has no validated source reservation")
            if not state.get("cwd") and cwd and Path(cwd).is_absolute():
                state["cwd"] = cwd
            self._validate_source(state, Path(state["path"]))
            pending = self.pending(session_id)
            if pending:
                return pending
            historic = bool(state.get("snapshot_path") and not state.get("historical_complete"))
            if historical_only and state.get("historical_complete"):
                return None
            path = Path(state["snapshot_path"] if historic else state["path"])
            if historic and prefix_hash(path, state["historical_end"]) != state["historical_hash"]:
                raise ReconciliationRequired("historical snapshot changed")
            end = state["source_byte_end"]
            lines = state["source_line_end"]
            event_no = state["event_end"]
            events: list[dict] = []
            size = 2048  # envelope + bounded source provenance
            with path.open("rb") as handle:
                handle.seek(end)
                while True:
                    raw = handle.readline(MAX_SOURCE_LINE + 1)
                    if len(raw) > MAX_SOURCE_LINE:
                        raise DeliveryPending(
                            "source event exceeds the bounded parser; source retained"
                        )
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        if historic:
                            raise DeliveryPending(
                                "historical transcript has an incomplete trailing line"
                            )
                        break
                    try:
                        value = _SANITIZERS[self.source](json.loads(raw)) if raw.strip() else None
                    except (ValueError, UnicodeDecodeError):
                        value = None
                    retained = (
                        value if isinstance(value, list) else ([] if value is None else [value])
                    )
                    if historic:
                        for event in retained:
                            if isinstance(event, dict):
                                event.pop("toolUseResult", None)
                    additions = [
                        {"line_no": event_no + i, "raw": event} for i, event in enumerate(retained)
                    ]
                    increment = len(json.dumps(additions, separators=(",", ":")).encode())
                    if size + increment > max_body_bytes:
                        if not events:
                            raise DeliveryPending(
                                "sanitized event exceeds the gateway batch budget; source retained"
                            )
                        break
                    events.extend(additions)
                    event_no += len(additions)
                    size += increment
                    end += len(raw)
                    lines += 1
                    # Bound reads even when the sanitizer drops everything.
                    if end - state["source_byte_end"] >= 4 * 1024 * 1024:
                        break
            at_end = end == path.stat().st_size
            final = (
                not events and end == state["source_byte_end"] and at_end and (finalize or historic)
            )
            if end == state["source_byte_end"] and not final:
                return None
            if final and state["finalized"]:
                if historic:
                    state["historical_complete"] = True
                    self._save(state)
                return None
            body = {
                "protocol_version": 2,
                "session_id": session_id,
                "stream_id": state["stream_id"],
                "batch_seq": state["last_seq"] + 1,
                "source_byte_start": state["source_byte_end"],
                "source_byte_end": end,
                "source_line_start": state["source_line_end"],
                "source_line_end": lines,
                "event_start": state["event_end"],
                "event_end": event_no,
                "prefix_sha256": prefix_hash(path, end),
            }
            if final:
                body["finalize"] = True
            else:
                body.update({"cwd": state.get("cwd") or cwd, "events": events})
                if state["last_seq"] == -1 and state.get("provenance"):
                    body["provenance"] = state["provenance"]
            if historic:
                body["snapshot_byte_end"] = state["historical_end"]
                body["snapshot_sha256"] = state["historical_hash"]
            encoded = canonical_payload(body)
            if len(encoded) > max_body_bytes:
                raise DeliveryPending("transcript envelope exceeds the batch budget")
            used = self.conn.execute(
                "SELECT COALESCE(sum(length(body)),0) FROM pending"
            ).fetchone()[0]
            if used + len(encoded) > self.cap_bytes:
                raise DeliveryPending("transcript queue full; pending bytes preserved")
            self.conn.execute(
                "INSERT INTO pending(session_id,body,digest) VALUES(?,?,?)",
                (session_id, encoded, hashlib.sha256(encoded).hexdigest()),
            )
            # The reservation is represented by this row. It is committed together
            # with state; no source cursor can pass it until its receipt arrives.
            self._save(state)
            return encoded

    def acknowledge(self, session_id: str, result: dict, *, sent_body: bytes | None = None):
        with self.transaction():
            pending = self.pending(session_id)
            body = sent_body if sent_body is not None else pending
            if body is None:
                return
            payload = json.loads(body)
            receipt = result.get("receipt") or {}
            if (
                result.get("protocol_version") != 2
                or receipt.get("body_sha256") != hashlib.sha256(body).hexdigest()
            ):
                raise ReconciliationRequired(
                    "server acknowledgment does not match the immutable pending batch"
                )
            for key in (
                "batch_seq",
                "source_byte_end",
                "source_line_end",
                "event_end",
                "prefix_sha256",
            ):
                if receipt.get(key) != payload[key]:
                    raise ReconciliationRequired(
                        "server acknowledgment cursor does not match the pending batch"
                    )
            if receipt.get("finalized") != bool(payload.get("finalize")):
                raise ReconciliationRequired(
                    "server acknowledgment finalized a different stream boundary"
                )
            state = self.get(session_id)
            if state["last_seq"] >= payload["batch_seq"]:
                # Another compatible drainer committed this receipt and may
                # already have reserved its successor. Never retire that row.
                return
            if pending != body:
                raise ReconciliationRequired("pending reservation changed before acknowledgment")
            state.update(
                {
                    k: payload[k]
                    for k in ("source_byte_end", "source_line_end", "event_end", "prefix_sha256")
                }
            )
            state.update(
                {
                    "last_seq": payload["batch_seq"],
                    "finalized": bool(payload.get("finalize")),
                    "error": None,
                }
            )
            if (
                state.get("historical_end") is not None
                and state["finalized"]
                and state["source_byte_end"] >= state["historical_end"]
            ):
                state["historical_complete"] = True
            self._save(state)
            self.conn.execute("DELETE FROM pending WHERE session_id=?", (session_id,))

    def deliver(self, session_id: str, wire: Wire) -> bool:
        body = self.pending(session_id)
        if body is None:
            return False
        code, result = wire.post(body)
        if code != 202:
            message = f"http {code}: {result.get('detail', 'transcript delivery pending')}"
            self.update(session_id, error=message)
            if code in (409, 422):
                raise ReconciliationRequired(message)
            raise DeliveryPending(
                message,
                retryable=_retryable_status(code) or (
                    code == 0 and getattr(wire, "last_request_retryable", False)
                ),
            )
        self.acknowledge(session_id, result, sent_body=body)
        return True

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
            if state and state.get("historical_complete") and state.get("snapshot_path"):
                Path(state["snapshot_path"]).unlink(missing_ok=True)

    def sessions(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.conn.execute("SELECT state FROM sessions")]
