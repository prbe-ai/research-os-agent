"""The durable outbox journal: ONE ordered queue for every deferred write.

Design (eng review 2026-07-29, T1-C): a single versioned operation journal
replaces the JSONL spool + would-be maildir split. Every async operation --
metric batch, span, note, reference-add, artifact upload, run end -- is one op
file; file-bearing ops reference a content-addressed blob store next door.

Layout (everything 0o700 dirs / 0o600 files -- queue contents are research
data and the journal may live on shared storage):

    <dir>/ops/<time_ns>-<op_id>.json     one op, atomic write; FIFO by filename
    <dir>/failed/<same name>.json        dead letters (permanent rejections)
    <dir>/blobs/<sha256>                 staged bytes, deduped by content
    <dir>/blobs/incoming-<op_id>         staged bytes not yet hashed (11A: big
                                         files hash in the drainer, not enqueue)
    <dir>/status.json                    single-stat summary for the banner
    <dir>/paused                         marker: drains are suspended
    <dir>/.append.lock / .drain.lock     flock sidecars

Ops never carry credentials: they pin a context NAME + base_url (5A) and the
drain resolves tokens fresh via ``config.resolve``. Import of this module must
stay httpx-free -- enqueue runs beside training loops; the network stack loads
only inside :func:`drain`.

Op schema (``probe.outbox/1``)::

    {
      "schema": "probe.outbox/1",
      "op_id": "<uuid hex>",
      "kind": "http" | "upload",
      "run_ref": "<run id/slug>" | null,       # barrier scoping (T3-A)
      "context": {"name": <str|null>, "base_url": <str>},
      "enqueued_at": <iso8601>,
      "attempts": <int>, "last_error": <str|null>,
      # kind == "http":
      "method": ..., "path": ..., "body": {...} | null,
      # kind == "upload":
      "upload": {"anchor", "anchor_id", "name", "content_type", "kind",
                 "meta", "span_id", "step_index", "blob": <sha256|null>,
                 "src_path", "staged": <bool>, "size_bytes": <int|null>,
                 "artifact_id": <hint|null>}
    }

Failure policy (7A + T2-A, phase-aware per the codex pass):
  * permanent rejection (4xx except 408/429/auth) -> the op moves to failed/
    and the queue keeps flowing; ``retry_failed`` puts it back.
  * transient (network, 5xx, 408, 429, unexpected exceptions) -> the drain
    stops in place; nothing is moved; the drainer retries with backoff.
  * 401/403 -> the drain halts as a CREDENTIAL-level blocker; ops stay queued
    untouched (an expired token must not cascade the queue into dead letters).
  * 409 carrying ``existing_id`` -> an idempotent replay already landed;
    counted as delivered.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import errors
from .durable import (
    file_lock,
    fsync_directory,
    now_iso,
    snapshot_file,
    write_text_atomic,
)
from .hashing import fingerprint

SCHEMA = "probe.outbox/1"

#: 11A -- files at or under this size hash (and presign-ping) inline at
#: enqueue; larger ones snapshot instantly and hash in the drainer.
INLINE_HASH_MAX_BYTES = int(
    os.environ.get("PROBE_ASYNC_INLINE_HASH_MAX", 256 * 1024 * 1024)
)

_RUN_PATH = re.compile(r"^/v1/runs/([^/]+)(?:/|$)")

# Transient-when-status statuses beyond the typed transport/server errors.
_TRANSIENT_STATUSES = {408, 429}


def default_dir() -> Path:
    configured = os.environ.get("PROBE_OUTBOX_DIR")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "probe" / "outbox"


def classify(exc: Exception) -> str:
    """``transient`` | ``permanent`` | ``auth`` | ``idempotent`` for one failure.

    Phase-agnostic core; the phase-AWARE cases (404 after a ``have`` dedup, the
    superseded-confirm race) are handled where the phase is known -- inside
    ``Client.upload_fingerprinted`` -- and never reach this classifier.
    Anything that is not a typed client error counts as transient: our own bug
    must park the queue, not destroy data.
    """
    if isinstance(exc, (errors.AuthError, errors.ScopeError)):
        return "auth"
    if isinstance(exc, errors.ConflictError):
        return "idempotent" if exc.existing_id else "permanent"
    if isinstance(exc, errors.ValidationError):
        # Includes locally-raised journal errors (missing staged bytes, unknown
        # op kind) that carry no HTTP status: retrying can never satisfy them,
        # and 'transient' would park the drainer on them forever (perf review).
        return "permanent"
    if isinstance(exc, (errors.TransportError, errors.ServerError)):
        return "transient"
    if isinstance(exc, errors.RosError):
        if exc.status in _TRANSIENT_STATUSES:
            return "transient"
        if exc.status is not None and 400 <= exc.status < 500:
            return "permanent"
        return "transient"
    return "transient"


def run_ref_for_path(path: str) -> str | None:
    """The run a v1 path addresses, when it addresses one. Barrier scoping key."""
    match = _RUN_PATH.match(path)
    return match.group(1) if match else None


@dataclass
class DrainReport:
    delivered: int = 0
    dead_lettered: int = 0
    remaining: int = 0
    auth_blocked: bool = False
    stopped_transient: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return (
            self.dead_lettered == 0
            and self.remaining == 0
            and not self.auth_blocked
        )


class Journal:
    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        context: dict | None = None,
    ):
        self.dir = Path(directory).expanduser() if directory else default_dir()
        self.ops_dir = self.dir / "ops"
        self.failed_dir = self.dir / "failed"
        self.blobs_dir = self.dir / "blobs"
        self.status_file = self.dir / "status.json"
        self.paused_file = self.dir / "paused"
        self.append_lock = self.dir / ".append.lock"
        self.drain_lock = self.dir / ".drain.lock"
        #: default {"name", "base_url"} pin stamped onto appended ops.
        self.context = context

    # -- layout -------------------------------------------------------------
    def _ensure(self) -> None:
        for directory in (self.dir, self.ops_dir, self.failed_dir, self.blobs_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            # mkdir mode is masked by umask; queue contents are research data,
            # so re-assert (codex finding: primitives default to umask).
            try:
                os.chmod(directory, 0o700)
            except OSError:
                pass
        if not getattr(self, "_spool_checked", False):
            # One-time fold of a surviving pre-journal spool (T1-C). Two stat
            # calls when there is nothing to import. ONLY the default journal
            # auto-imports: a custom/temporary journal directory must never
            # steal (and then delete) the machine's global spool (codex).
            self._spool_checked = True
            if self.dir == default_dir():
                try:
                    self.import_spool()
                except Exception:  # noqa: BLE001 -- legacy debris must not block a write
                    pass

    # -- append -------------------------------------------------------------
    def _op_filename(self, op_id: str) -> str:
        return f"{time.time_ns():020d}-{op_id}.json"

    def _base_op(self, kind: str, run_ref: str | None) -> dict:
        return {
            "schema": SCHEMA,
            "op_id": uuid.uuid4().hex,
            "kind": kind,
            "run_ref": run_ref,
            "context": self.context,
            "enqueued_at": now_iso(),
            "attempts": 0,
            "last_error": None,
        }

    def _append(self, op: dict, *, before_write=None) -> str:
        self._ensure()
        with file_lock(self.append_lock):
            if before_write is not None:
                # Runs INSIDE the lock, before the op file exists -- used by
                # append_upload to publish its staged blob atomically with the
                # op that references it, so gc_blobs (which also takes this
                # lock) can never see the blob as unreferenced garbage.
                before_write()
            path = self.ops_dir / self._op_filename(op["op_id"])
            write_text_atomic(path, json.dumps(op, indent=2) + "\n", mode=0o600)
            self._write_status_locked()
        return op["op_id"]

    def append_http(
        self,
        method: str,
        path: str,
        body: dict | None,
        *,
        run_ref: str | None = None,
    ) -> str:
        op = self._base_op("http", run_ref or run_ref_for_path(path))
        op.update({"method": method, "path": path, "body": body})
        return self._append(op)

    def append_upload(
        self,
        *,
        anchor: str,
        anchor_id: str | None,
        name: str,
        src_path: str,
        stage: bool = True,
        inline_hash: bool = False,
        content_type: str | None = None,
        kind: str | None = None,
        meta: dict | None = None,
        span_id: str | None = None,
        step_index: int | None = None,
        run_ref: str | None = None,
    ) -> dict:
        """Queue a byte upload; returns ``{op_id, blob, size_bytes}``.

        When ``stage`` is set the file is snapshotted into the blob store, and
        with ``inline_hash`` the digest is taken FROM THE SNAPSHOT -- never
        from the live source, whose bytes can change between a hash pass and a
        copy pass (codex: a same-size rewrite in that window would poison the
        content address). Without ``inline_hash`` the drainer hashes later
        (11A). Snapshot lands under a dot-prefixed staging name (gc ignores
        dotfiles); the publish rename + op-file write happen together under
        the append lock, so gc can never see an unreferenced blob to delete.
        """
        self._ensure()
        op = self._base_op("upload", run_ref)
        staged = False
        publish = None
        digest: str | None = None
        size_bytes: int | None = None
        if stage:
            staging = self.blobs_dir / f".staging-{op['op_id']}"
            snapshot_file(src_path, staging)
            if inline_hash:
                digest, size_bytes = fingerprint(str(staging))
            final = (
                self.blobs_dir / digest
                if digest is not None
                else self.blobs_dir / f"incoming-{op['op_id']}"
            )

            def publish() -> None:
                if digest is not None and final.exists():
                    staging.unlink(missing_ok=True)  # dedup: bytes already staged
                else:
                    os.replace(staging, final)
                fsync_directory(self.blobs_dir)

            staged = True
        elif inline_hash:
            digest, size_bytes = fingerprint(src_path)
        op["upload"] = {
            "anchor": anchor,
            "anchor_id": anchor_id,
            "name": name,
            "content_type": content_type,
            "kind": kind,
            "meta": meta,
            "span_id": span_id,
            "step_index": step_index,
            "blob": digest,
            "src_path": os.path.abspath(src_path),
            "staged": staged,
            "size_bytes": size_bytes,
        }
        self._append(op, before_write=publish)
        return {"op_id": op["op_id"], "blob": digest, "size_bytes": size_bytes}

    # -- reads --------------------------------------------------------------
    @staticmethod
    def _read_dir(directory: Path) -> list[tuple[Path, dict]]:
        if not directory.exists():
            return []
        out: list[tuple[Path, dict]] = []
        for path in sorted(directory.iterdir()):
            if not path.name.endswith(".json"):
                continue
            try:
                out.append((path, json.loads(path.read_text())))
            except (OSError, json.JSONDecodeError):
                continue
        return out

    def pending(self) -> list[tuple[Path, dict]]:
        return self._read_dir(self.ops_dir)

    def failed(self) -> list[tuple[Path, dict]]:
        return self._read_dir(self.failed_dir)

    @property
    def paused(self) -> bool:
        return self.paused_file.exists()

    def pause(self) -> None:
        self._ensure()
        write_text_atomic(self.paused_file, now_iso() + "\n", mode=0o600)
        with file_lock(self.append_lock):
            self._write_status_locked()

    def resume(self) -> None:
        self.paused_file.unlink(missing_ok=True)
        if self.dir.exists():
            with file_lock(self.append_lock):
                self._write_status_locked()

    def quarantine_corrupt(self) -> int:
        """Move unparseable op files to failed/ so they stay VISIBLE.

        Silently skipping them (codex) let status count files the drain never
        saw: the worker exited 'empty' while the banner re-kicked it forever.
        Quarantined files keep their names -- they count as failed in
        status.json, while the (parse-skipping) readers ignore them.
        """
        moved = 0
        with file_lock(self.append_lock):
            if self.ops_dir.exists():
                for path in sorted(self.ops_dir.iterdir()):
                    if not path.name.endswith(".json"):
                        continue
                    try:
                        json.loads(path.read_text())
                    except (OSError, json.JSONDecodeError, ValueError):
                        os.replace(path, self.failed_dir / path.name)
                        moved += 1
            if moved:
                fsync_directory(self.failed_dir)
                fsync_directory(self.ops_dir)
                self._write_status_locked()
        return moved

    def clear_auth_block(self) -> None:
        """Forget a recorded auth block (after re-login / explicit retry) so
        the wake-on-enqueue drainer starts spawning again (codex: nothing
        cleared it, so delivery stayed stopped forever after one 401)."""
        self.write_status(auth_blocked_since=None)

    def retry_failed(self, op_id: str | None = None) -> int:
        """Requeue dead letters (one op, or all). Files keep their names, so a
        retried op re-enters at its original FIFO position."""
        moved = 0
        with file_lock(self.append_lock):
            for path, op in self._read_dir(self.failed_dir):
                if op_id is not None and op.get("op_id") != op_id:
                    continue
                os.replace(path, self.ops_dir / path.name)
                moved += 1
            if moved:
                fsync_directory(self.ops_dir)
                fsync_directory(self.failed_dir)
                self._write_status_locked()
        return moved

    # -- status -------------------------------------------------------------
    @staticmethod
    def _count_dir(directory: Path) -> tuple[int, str | None]:
        """(count, first filename) without parsing op bodies -- this runs on
        EVERY append, so it must stay O(directory listing), not O(total bytes
        queued) (perf review: a training loop enqueuing offline was O(N^2))."""
        if not directory.exists():
            return 0, None
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
        return len(names), names[0] if names else None

    def _write_status_locked(self, **extra: Any) -> None:
        pending_count, first_pending = self._count_dir(self.ops_dir)
        failed_count, _ = self._count_dir(self.failed_dir)
        oldest = None
        if first_pending:
            try:
                oldest = json.loads(
                    (self.ops_dir / first_pending).read_text()
                ).get("enqueued_at")
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        previous: dict = {}
        try:
            previous = json.loads(self.status_file.read_text())
        except (OSError, json.JSONDecodeError):
            pass
        status = {
            "schema": SCHEMA,
            "updated_at": now_iso(),
            "pending": pending_count,
            "failed": failed_count,
            "oldest_pending": oldest,
            "paused": self.paused,
            "auth_blocked_since": previous.get("auth_blocked_since"),
            "last_error": previous.get("last_error"),
        }
        status.update(extra)
        write_text_atomic(self.status_file, json.dumps(status, indent=2) + "\n", mode=0o600)

    def write_status(self, **extra: Any) -> None:
        self._ensure()
        with file_lock(self.append_lock):
            self._write_status_locked(**extra)

    @staticmethod
    def read_status(directory: str | Path | None = None) -> dict | None:
        """Banner-grade read: one file, no locks, never raises."""
        path = (Path(directory).expanduser() if directory else default_dir()) / "status.json"
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return None

    # -- blob store ---------------------------------------------------------
    def blob_path(self, op: dict) -> Path | None:
        upload = op.get("upload") or {}
        if not upload.get("staged"):
            return None
        if upload.get("blob"):
            return self.blobs_dir / upload["blob"]
        return self.blobs_dir / f"incoming-{op['op_id']}"

    def gc_blobs(self, *, staging_grace_seconds: float = 86_400.0) -> int:
        """Drop blobs no live or dead op references. Liveness is computed, not
        counted -- crash-safe by construction. The reference scan happens
        INSIDE the append lock (codex: a stale reference set computed before
        the lock could delete a blob published while gc waited). Crash-orphaned
        ``.staging-*`` files older than the grace window are swept too, so an
        interrupted enqueue cannot leak a multi-GB snapshot forever."""
        if not self.blobs_dir.exists():
            return 0
        removed = 0
        with file_lock(self.append_lock):
            referenced: set[str] = set()
            for _, op in self.pending() + self.failed():
                upload = op.get("upload") or {}
                if upload.get("staged"):
                    referenced.add(upload.get("blob") or f"incoming-{op['op_id']}")
            cutoff = time.time() - staging_grace_seconds
            for path in self.blobs_dir.iterdir():
                if path.name.startswith(".staging-"):
                    try:
                        if path.stat().st_mtime < cutoff:
                            path.unlink(missing_ok=True)
                            removed += 1
                    except OSError:
                        pass
                    continue
                if path.name.startswith("."):
                    continue
                if path.name not in referenced:
                    path.unlink(missing_ok=True)
                    removed += 1
        return removed

    # -- legacy spool import -------------------------------------------------
    def import_spool(self, spool=None) -> int:
        """Fold a surviving pre-journal spool into the journal, in order.

        The spool's two-file protocol is honored by reading inflight first --
        exactly the order ``Spool.flush`` would have replayed. Records import
        with the CURRENT context pin (the spool never recorded one) and the
        spool files are removed, so this runs once per machine, ever.
        """
        from .spool import Spool

        spool = spool or Spool()
        if not (spool.file.exists() or spool.inflight_file.exists()):
            return 0
        imported = 0
        with file_lock(spool.lock_file):
            # NOT spool.pending(): that takes the same lock we already hold,
            # and flock is not reentrant across file handles.
            records = spool._read_records(spool.inflight_file) + spool._read_records(
                spool.file
            )
            for record in records:
                self.append_http(record.method, record.path, record.json_body)
                imported += 1
            spool.file.unlink(missing_ok=True)
            spool.inflight_file.unlink(missing_ok=True)
        return imported


_URL_QUERY = re.compile(r"\?\S+")


def _redact(text: str) -> str:
    """Strip query strings from URLs embedded in error text. A presigned PUT
    URL's query IS a signed, bearer-equivalent write capability; transport
    errors embed the full URL, and last_error is persisted to op files,
    status.json, doctor output, and the drainer log (security review)."""
    return _URL_QUERY.sub("?<redacted>", text)


def _settings_for(context: dict | None):
    from .config import DEFAULT_BASE_URL, Settings, load_context, resolve

    name = (context or {}).get("name")
    pinned = (context or {}).get("base_url")
    settings = resolve(context=name)
    stored = load_context(name)
    if pinned and pinned.rstrip("/") != settings.base_url.rstrip("/"):
        # Ambient resolution (env vars outrank the context file) produced a
        # credential for a DIFFERENT endpoint than this op was enqueued for.
        # Never mix: a token issued for endpoint A must not be sent to pinned
        # host B (security review). Use only the named context's stored record
        # for the pinned endpoint; auth-block when none matches.
        stored_base = (stored.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        if stored_base == pinned.rstrip("/") and (
            stored.get("token") or stored.get("ingest_token")
        ):
            return Settings(
                base_url=pinned.rstrip("/"),
                token=stored.get("token"),
                ingest_token=stored.get("ingest_token"),
                hmac_secret=stored.get("hmac_secret"),
            )
        raise errors.AuthError(
            f"no stored credential for pinned endpoint {pinned} "
            f"(context {name or '<default>'}); run `probe login`"
        )
    if pinned:
        settings.base_url = pinned.rstrip("/")
    # Same endpoint, but the pinned context STORES its own credential: the
    # stored token outranks any ambient PROBE_TOKEN for a drain. Tenants can
    # share one API URL, and a detached worker inheriting another account's
    # env must not replay this context's ops under the wrong principal
    # (codex). Env credentials still serve contexts that store none.
    if stored.get("token"):
        settings.token = stored.get("token")
    if stored.get("ingest_token"):
        settings.ingest_token = stored.get("ingest_token")
    return settings


def drain(
    journal: Journal,
    *,
    run_ref: str | None = None,
    client_factory: Callable[[dict | None], Any] | None = None,
    wait_for_lock: bool = True,
) -> DrainReport:
    """Deliver queued ops in FIFO order. Foreground and background drains share
    this; the drainer loop wraps it in backoff.

    ``run_ref`` scopes a barrier drain (T3-A): only that run's ops are
    attempted, everything else is left for the machine-wide drainer.
    ``client_factory(context)`` exists for tests; production resolves a client
    per pinned context, tokens fresh (5A).
    """
    report = DrainReport()
    journal._ensure()
    try:
        journal.quarantine_corrupt()
    except Exception:  # noqa: BLE001 -- quarantine is best-effort hygiene
        pass
    if journal.paused:
        report.remaining = len(journal.pending())
        return report

    from .client import Client  # lazy: enqueue paths must not import httpx

    clients: dict[tuple, Any] = {}
    constructed: list[Any] = []  # only clients WE built get closed

    def client_for(context: dict | None):
        key = ((context or {}).get("name"), (context or {}).get("base_url"))
        if key not in clients:
            supplied = client_factory(context) if client_factory is not None else None
            if supplied is not None:
                clients[key] = supplied
            else:
                settings = _settings_for(context)
                if not settings.token and not settings.ingest_token:
                    raise errors.AuthError(
                        "no credentials for context "
                        f"{key[0] or '<default>'} (run `probe login`)"
                    )
                client = Client(settings=settings)
                constructed.append(client)
                clients[key] = client
        return clients[key]

    lock_handle = journal.drain_lock.open("a+")
    import fcntl as _fcntl

    try:
        _fcntl.flock(
            lock_handle.fileno(),
            _fcntl.LOCK_EX if wait_for_lock else _fcntl.LOCK_EX | _fcntl.LOCK_NB,
        )
    except BlockingIOError:
        lock_handle.close()
        report.remaining = len(journal.pending())
        report.errors.append("another drain holds the lock")
        return report

    touched_upload = False
    try:
        for path, op in journal.pending():
            if run_ref is not None and op.get("run_ref") != run_ref:
                continue
            touched_upload = touched_upload or op.get("kind") == "upload"
            try:
                _execute(journal, client_for(op.get("context")), path, op)
            except Exception as exc:  # noqa: BLE001 -- classified below
                verdict = classify(exc)
                if verdict == "idempotent" and int(op.get("attempts", 0)) > 0:
                    # A 409-with-existing_id on a RETRY plausibly names our own
                    # earlier half-delivered attempt. On a FIRST attempt it is
                    # a genuine natural-key conflict -- treating it as success
                    # would silently discard the queued write (codex), so it
                    # falls through to the permanent path below.
                    path.unlink(missing_ok=True)
                    fsync_directory(journal.ops_dir)
                    report.delivered += 1
                    continue
                op["attempts"] = int(op.get("attempts", 0)) + 1
                op["last_error"] = _redact(f"{type(exc).__name__}: {exc}")
                if verdict == "auth":
                    write_text_atomic(path, json.dumps(op, indent=2) + "\n", mode=0o600)
                    report.auth_blocked = True
                    report.errors.append(op["last_error"])
                    break
                if verdict in ("permanent", "idempotent"):
                    # Update in place, then MOVE atomically: writing a failed/
                    # copy before unlinking the original leaves the op in both
                    # queues across a crash (codex).
                    write_text_atomic(path, json.dumps(op, indent=2) + "\n", mode=0o600)
                    os.replace(path, journal.failed_dir / path.name)
                    fsync_directory(journal.failed_dir)
                    fsync_directory(journal.ops_dir)
                    report.dead_lettered += 1
                    report.errors.append(op["last_error"])
                    continue
                # transient: park in place, order preserved
                write_text_atomic(path, json.dumps(op, indent=2) + "\n", mode=0o600)
                report.stopped_transient = True
                report.errors.append(op["last_error"])
                break
            else:
                path.unlink(missing_ok=True)
                # fsync so a post-delivery crash cannot resurrect the op file
                # and replay a write the server already committed (codex:
                # replay is at-least-once; keep the window as small as disk
                # semantics allow).
                fsync_directory(journal.ops_dir)
                report.delivered += 1
    finally:
        for client in constructed:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        if touched_upload:  # a pass over pure-JSON ops has no blobs to collect
            try:
                journal.gc_blobs()
            except Exception:  # noqa: BLE001
                pass
        remaining = journal.pending()
        report.remaining = len(remaining)
        journal.write_status(
            auth_blocked_since=(now_iso() if report.auth_blocked else None),
            last_error=(report.errors[-1] if report.errors else None),
        )
        _fcntl.flock(lock_handle.fileno(), _fcntl.LOCK_UN)
        lock_handle.close()
    return report


def _execute(journal: Journal, client, op_path: Path, op: dict) -> None:
    """Deliver one op. Raises the transport/client error on failure. Field
    guards raise ValidationError (permanent -> dead letter): a KeyError here
    would classify transient and wedge the FIFO on a malformed op forever."""
    if op.get("kind") == "http":
        if not op.get("method") or not op.get("path"):
            raise errors.ValidationError(
                f"op {op.get('op_id')} is missing method/path", status=422
            )
        client.transport.request(op["method"], op["path"], json_body=op.get("body"))
        return
    if op.get("kind") != "upload":
        raise errors.ValidationError(
            f"unknown journal op kind {op.get('kind')!r}", status=422
        )

    upload = op.get("upload") or {}
    if not upload.get("anchor") or not upload.get("name") or not upload.get("src_path"):
        raise errors.ValidationError(
            f"upload op {op.get('op_id')} is missing anchor/name/src_path", status=422
        )
    source = journal.blob_path(op) or Path(upload["src_path"])
    if not source.exists():
        raise errors.ValidationError(
            f"staged bytes for op {op['op_id']} are gone ({source})", status=422
        )
    digest = upload.get("blob")
    size = upload.get("size_bytes")
    if digest is None:
        # 11A: the big-file path hashed nothing at enqueue.
        digest, size = fingerprint(str(source))
        if upload.get("staged"):
            hashed = journal.blobs_dir / digest
            if hashed.exists():
                source.unlink(missing_ok=True)  # dedup: identical bytes already staged
            else:
                os.replace(source, hashed)
                fsync_directory(journal.blobs_dir)
            source = hashed
        upload["blob"], upload["size_bytes"] = digest, size
        # Persist the rename BEFORE attempting delivery: a crash after the
        # incoming-><digest> move would otherwise leave an op file pointing at
        # a staging name that no longer exists, dead-lettering a good upload
        # on the next drain.
        write_text_atomic(op_path, json.dumps(op, indent=2) + "\n", mode=0o600)

    client.upload_fingerprinted(
        upload["anchor"],
        upload["anchor_id"],
        upload["name"],
        str(source),
        digest=digest,
        size=size,
        content_type=upload.get("content_type"),
        kind=upload.get("kind"),
        meta=upload.get("meta"),
        span_id=upload.get("span_id"),
        step_index=upload.get("step_index"),
    )
