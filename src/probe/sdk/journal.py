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
    <dir>/producers/<producer>.json      per-writer accounting (parity F4):
                                         sequence high-water, delivered tally,
                                         capture gaps, open/closed state

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
      # when the writer registered with the producer registry (F4):
      "producer_id": <str>, "producer_sequence": <int>,
      # kind == "http":
      "method": ..., "path": ..., "body": {...} | null,
      # kind == "upload":
      "upload": {"anchor", "anchor_id", "name", "content_type", "kind",
                 "meta", "span_id", "step_index", "blob": <sha256|null>,
                 "src_path", "staged": <bool>, "size_bytes": <int|null>,
                 "unstaged_reason": <str|null>, "artifact_id": <hint|null>}
    }

``staged`` false means the op REFERENCES ``src_path`` and the drainer reads the
original bytes. ``unstaged_reason`` says why when the journal made that choice
itself (disk headroom) rather than the caller asking for it.

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
import shutil
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

def _inline_hash_max() -> int:
    raw = os.environ.get("PROBE_ASYNC_INLINE_HASH_MAX")
    if raw:
        try:
            return int(raw)
        except ValueError:
            # A malformed override must not crash every import of this module
            # (client construction, enqueue, the drainer) -- fall back loudly.
            import warnings

            warnings.warn(
                f"ignoring malformed PROBE_ASYNC_INLINE_HASH_MAX={raw!r}; "
                "expected an integer byte count",
                stacklevel=2,
            )
    return 256 * 1024 * 1024


#: 11A -- files at or under this size hash (and presign-ping) inline at
#: enqueue; larger ones snapshot instantly and hash in the drainer.
INLINE_HASH_MAX_BYTES = _inline_hash_max()


def _min_free_bytes() -> int:
    raw = os.environ.get("PROBE_OUTBOX_MIN_FREE_BYTES")
    if raw:
        try:
            return max(0, int(raw))
        except ValueError:
            # Same contract as _inline_hash_max: a malformed override must not
            # crash every import of this module -- fall back loudly.
            import warnings

            warnings.warn(
                f"ignoring malformed PROBE_OUTBOX_MIN_FREE_BYTES={raw!r}; "
                "expected an integer byte count",
                stacklevel=2,
            )
    return 2 * 1024 * 1024 * 1024


#: Headroom the blob store must LEAVE on its filesystem. Staging is a real byte
#: copy whenever the source is on another filesystem -- `try_clone` is
#: copy-on-write and cannot span mounts -- so importing a research folder off a
#: network share copies every file under the reference threshold onto the local
#: disk. Enqueue is fire-and-forget and the drainer is a single worker, so a
#: producer that outruns delivery grows the queue without bound: filling the
#: disk is the STEADY STATE of that shape, not an edge case.
#:
#: The check is deliberately blind to whether the copy would actually consume
#: space: a same-filesystem clone costs nothing, but so does declining to make
#: one (the source is on that same disk and the drainer reads it in place), so
#: the conservative answer is never the worse one and costs one statvfs.
#:
#: Zero disables the guard, for a caller who has measured their own ceiling.
MIN_FREE_BYTES = _min_free_bytes()

_RUN_PATH = re.compile(r"^/v1/runs/([^/]+)(?:/|$)")

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_component(value: str) -> str:
    return _SAFE_COMPONENT.sub("_", value)[:120] or "producer"

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
        self.producers_dir = self.dir / "producers"
        #: default {"name", "base_url"} pin stamped onto appended ops.
        self.context = context
        #: parity F4: set via register_producer; None = unstamped ops.
        self._producer_id: str | None = None
        self._producer_role: str | None = None

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
        """FIFO name: monotonic per-journal sequence first, wall clock second.

        Runs under the append lock. The sequence file makes ordering immune to
        backwards clock steps (red team: NTP correction between enqueues could
        otherwise sort a run-end PATCH before the metrics it must follow)."""
        seq_file = self.dir / ".seq"
        try:
            seq = int(seq_file.read_text().strip() or 0)
        except (OSError, ValueError):
            seq = 0
        seq += 1
        write_text_atomic(seq_file, f"{seq}\n", mode=0o600)
        return f"{seq:012d}-{time.time_ns():020d}-{op_id}.json"

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

    def _append(
        self, op: dict, *, before_write=None, unstaged_low_disk: int = 0
    ) -> str:
        self._ensure()
        with file_lock(self.append_lock):
            if self._producer_id is not None:
                # Sequence allocation reads the registry INSIDE the lock, not
                # an in-memory counter: a producer_id shared across processes
                # (the CLI's per-host one) must never mint the same sequence
                # twice (parity F4).
                record = self._read_producer_locked(self._producer_id)
                sequence = int(record.get("last_sequence") or 0) + 1
                op["producer_id"] = self._producer_id
                op["producer_sequence"] = sequence
                self._update_producer_locked(
                    self._producer_id,
                    record,
                    role=self._producer_role,
                    last_sequence=sequence,
                )
            if before_write is not None:
                # Runs INSIDE the lock, before the op file exists -- used by
                # append_upload to publish its staged blob atomically with the
                # op that references it, so gc_blobs (which also takes this
                # lock) can never see the blob as unreferenced garbage.
                before_write()
            path = self.ops_dir / self._op_filename(op["op_id"])
            write_text_atomic(path, json.dumps(op, indent=2) + "\n", mode=0o600)
            self._write_status_locked(unstaged_low_disk=unstaged_low_disk)
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
        notes: str | None = None,
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

        A requested ``stage`` can still come back false: staging is refused
        when the snapshot would drive the blob store's filesystem under
        :data:`MIN_FREE_BYTES`. The returned ``staged``/``unstaged_reason``
        (also recorded on the op) say so.
        """
        self._ensure()
        op = self._base_op("upload", run_ref)
        staged = False
        publish = None
        digest: str | None = None
        size_bytes: int | None = None
        unstaged_reason: str | None = None
        if stage:
            unstaged_reason = self._staging_headroom(src_path)
            if unstaged_reason is not None:
                # Degrade to referencing the source rather than refusing the
                # write: enqueue is fail-open by contract (it runs beside a
                # training loop), so the op still goes in and the drainer reads
                # the original bytes. If the source is gone by then the drain
                # raises a 422 -- a dead letter with a message, never a lost
                # write nobody was told about, and never a full disk.
                stage = False
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
            "notes": notes,
            "span_id": span_id,
            "step_index": step_index,
            "blob": digest,
            "src_path": os.path.abspath(src_path),
            "staged": staged,
            "size_bytes": size_bytes,
            "unstaged_reason": unstaged_reason,
        }
        self._append(
            op,
            before_write=publish,
            unstaged_low_disk=1 if unstaged_reason is not None else 0,
        )
        return {
            "op_id": op["op_id"],
            "blob": digest,
            "size_bytes": size_bytes,
            "staged": staged,
            "unstaged_reason": unstaged_reason,
        }

    def _staging_headroom(self, src_path: str | Path) -> str | None:
        """``None`` when there is room to stage ``src_path``, else WHY there is not.

        Two stat-class syscalls, no read of the file -- this runs on every
        upload enqueue, beside a training loop, so it must not scale with the
        bytes being queued. Unmeasurable is NOT a breach: if the size or the
        filesystem cannot be read we have no evidence to degrade on, and
        enqueue is fail-open.
        """
        floor = MIN_FREE_BYTES
        if floor <= 0:
            return None
        try:
            size = os.path.getsize(src_path)
            free = shutil.disk_usage(self.blobs_dir).free
        except OSError:
            return None
        if free - size >= floor:
            return None
        return (
            f"low disk: staging {size} bytes would leave {free - size} bytes "
            f"free on {self.blobs_dir}, under the {floor}-byte floor "
            "(PROBE_OUTBOX_MIN_FREE_BYTES); the op references the source "
            "instead and the drainer reads its original bytes"
        )

    # -- producer registry (parity F4) --------------------------------------
    def _producer_file(self, producer_id: str) -> Path:
        return self.producers_dir / f"{_safe_component(producer_id)}.json"

    def _ensure_producers_dir(self) -> None:
        self.producers_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.producers_dir, 0o700)
        except OSError:
            pass

    def _read_producer_locked(self, producer_id: str) -> dict:
        try:
            return json.loads(self._producer_file(producer_id).read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            return {}

    def _update_producer_locked(
        self,
        producer_id: str,
        record: dict,
        *,
        role: str | None = None,
        last_sequence: int | None = None,
        delivered: int | None = None,
        gap: dict | None = None,
        state: str | None = None,
    ) -> None:
        record.setdefault("schema", SCHEMA)
        record.setdefault("producer_id", producer_id)
        record.setdefault("role", role)
        record.setdefault("registered_at", now_iso())
        record.setdefault("last_sequence", 0)
        record.setdefault("delivered", 0)
        record.setdefault("gaps", [])
        record.setdefault("closed_at", None)
        if last_sequence is not None:
            record["last_sequence"] = max(int(record["last_sequence"]), last_sequence)
        if delivered is not None:
            # Additive, and re-read from disk under the lock by every caller:
            # the drainer and a live writer touch the same record from
            # different processes, so an in-memory counter would clobber.
            record["delivered"] = int(record.get("delivered") or 0) + int(delivered)
        if gap is not None:
            record["gaps"].append(gap)
        if state is not None:
            record["state"] = state
            record["closed_at"] = now_iso() if state == "closed" else None
        else:
            record.setdefault("state", "open")
        write_text_atomic(
            self._producer_file(producer_id),
            json.dumps(record, indent=2) + "\n",
            mode=0o600,
        )

    def register_producer(self, producer_id: str, *, role: str = "sdk") -> None:
        """Join this journal's producer registry.

        Sequences make silent capture loss VISIBLE: every subsequent append
        stamps ``producer_id`` + a per-producer sequence, and the registry
        records the high-water mark. A registry that says N with no op --
        queued, dead-lettered, or delivered -- ever stamped N is a write lost
        between the caller and the journal; ``note_capture_gap`` records
        those the caller catches itself. Re-registering an existing id
        resumes its sequence (restarts, and ids deliberately shared across
        short-lived processes, both continue the same line).
        """
        self._ensure()
        self._ensure_producers_dir()
        self._producer_id = producer_id
        self._producer_role = role
        with file_lock(self.append_lock):
            record = self._read_producer_locked(producer_id)
            self._update_producer_locked(
                producer_id, record, role=role, state="open"
            )

    def note_capture_gap(self, reason: str) -> None:
        """Burn a sequence for a write that never reached the journal, so the
        loss is a visible hole instead of silence (Miles' capture_gaps)."""
        if self._producer_id is None:
            return
        with file_lock(self.append_lock):
            record = self._read_producer_locked(self._producer_id)
            sequence = int(record.get("last_sequence") or 0) + 1
            self._update_producer_locked(
                self._producer_id,
                record,
                role=self._producer_role,
                last_sequence=sequence,
                gap={"sequence": sequence, "reason": reason, "at": now_iso()},
            )

    def note_delivered(self, producer_id: str, count: int = 1) -> None:
        """Add ``count`` LANDED ops to one producer's tally.

        ``last_sequence`` says what a producer handed the journal; this says
        how much of it actually reached the server, so "of what I enqueued,
        how much is really there" is answerable without correlating two
        machines. ``DrainReport.delivered`` cannot answer it: it is per-pass
        and machine-wide.

        Called by :func:`drain` per delivered op, NOT batched at the end of a
        pass -- a pass that dies mid-flight (SIGKILL, OOM, a dead laptop) is
        exactly when the number matters, and a batched tally would lose the
        whole pass. It takes a producer id rather than using this journal's
        own because the drainer delivers for every producer, usually including
        producers that no longer have a live process.

        The tally is a FLOOR. It is written after the op file is unlinked, so
        a crash in that window under-counts by at most one op per crash;
        writing it first would over-count instead, and a producer reporting
        more delivered than it ever enqueued reads as a bug rather than as the
        at-least-once delivery it actually is.
        """
        if not producer_id or count <= 0:
            return
        self._ensure_producers_dir()
        with file_lock(self.append_lock):
            record = self._read_producer_locked(producer_id)
            self._update_producer_locked(producer_id, record, delivered=count)

    def seal_producer(self) -> None:
        """Mark this producer cleanly closed. A producer left "open" whose
        process is gone is a crashed writer -- the report shows exactly that."""
        if self._producer_id is None or not self.producers_dir.exists():
            return
        with file_lock(self.append_lock):
            record = self._read_producer_locked(self._producer_id)
            self._update_producer_locked(
                self._producer_id, record, role=self._producer_role, state="closed"
            )

    def producer_report(self) -> list[dict]:
        """Every producer this journal knows: sequence high-water, ``delivered``
        tally, capture gaps, open/closed state. Unparseable records are skipped
        rather than raising -- a report is diagnostics, never a blocker."""
        if not self.producers_dir.exists():
            return []
        out: list[dict] = []
        for path in sorted(self.producers_dir.iterdir()):
            if not path.name.endswith(".json"):
                continue
            try:
                out.append(json.loads(path.read_text()))
            except (OSError, json.JSONDecodeError, ValueError):
                continue
        return out

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

    def discard_failed(self, op_id: str | None = None) -> int:
        """Move dead letters (one, or all) to ``discarded/`` -- an audit-safe
        tombstone rather than deletion. Covers quarantined-corrupt files too
        (matched by name when they cannot be parsed), which retry can never
        requeue (red team: without a discard verb they nagged forever)."""
        target = self.failed_dir.parent / "discarded"
        moved = 0
        with file_lock(self.append_lock):
            if self.failed_dir.exists():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                for path in sorted(self.failed_dir.iterdir()):
                    if op_id is not None:
                        try:
                            if json.loads(path.read_text()).get("op_id") != op_id:
                                continue
                        except (OSError, json.JSONDecodeError, ValueError):
                            if op_id not in path.name:
                                continue
                    os.replace(path, target / path.name)
                    moved += 1
            if moved:
                fsync_directory(target)
                fsync_directory(self.failed_dir)
                self._write_status_locked()
        if moved:
            self.gc_blobs()
        return moved

    def clear_auth_block(self) -> None:
        """Forget a recorded auth block (after re-login / explicit retry) so
        the wake-on-enqueue drainer starts spawning again (codex: nothing
        cleared it, so delivery stayed stopped forever after one 401)."""
        self.write_status(auth_blocked_since=None)

    def retry_failed(
        self, op_id: str | None = None, *, run_ref: str | None = None
    ) -> int:
        """Requeue dead letters (one op, one run's, or all). Files keep their
        names, so a retried op re-enters at its original FIFO position."""
        moved = 0
        with file_lock(self.append_lock):
            for path, op in self._read_dir(self.failed_dir):
                if op_id is not None and op.get("op_id") != op_id:
                    continue
                if run_ref is not None and op.get("run_ref") != run_ref:
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

    def _write_status_locked(self, *, unstaged_low_disk: int = 0, **extra: Any) -> None:
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
            # Monotonic tally of uploads that DEGRADED to unstaged for want of
            # disk headroom -- carried forward like the two fields above.
            # Counting them by scanning ops instead would make every append
            # O(queue), which is exactly the regression the O(1) status
            # rewrite fixed.
            "unstaged_low_disk": (
                int(previous.get("unstaged_low_disk") or 0) + int(unstaged_low_disk)
            ),
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
                    # BOTH possible names stay referenced: mid-drain, an op
                    # whose digest was just persisted may still hold its bytes
                    # under the incoming staging name (red team).
                    referenced.add(f"incoming-{op['op_id']}")
                    if upload.get("blob"):
                        referenced.add(upload["blob"])
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
        if self.context is None:
            # Stamp the records with the context that is current AT IMPORT
            # TIME (red team: a null pin resolves at drain time, so a context
            # switch in between would deliver the old spool's writes to a
            # different tenant than the one that captured them).
            from .config import current_context_name, resolve

            self.context = {
                "name": current_context_name() or None,
                "base_url": resolve().base_url,
            }
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

    def tally_delivered(op: dict) -> None:
        """Credit one landed op to the producer that enqueued it. Runs AFTER
        the op file is gone (see ``note_delivered``) and can never fail a
        delivery: accounting is diagnostics, the write already happened."""
        producer_id = op.get("producer_id")
        if not producer_id:
            return
        try:
            journal.note_delivered(producer_id)
        except Exception:  # noqa: BLE001 -- accounting must never break the drain
            pass

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
                    tally_delivered(op)
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
                tally_delivered(op)
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
    if not source.exists() and upload.get("staged"):
        # Recover an interrupted incoming-><digest> rename from a previous
        # drain (red team): the op may name a digest whose file only exists
        # under the incoming staging name (or vice versa).
        incoming = journal.blobs_dir / f"incoming-{op['op_id']}"
        if incoming.exists():
            if upload.get("blob"):
                os.replace(incoming, source)
                fsync_directory(journal.blobs_dir)
            else:
                source = incoming
    if not source.exists():
        # The two absences are different failures and want different fixes: a
        # missing STAGED blob is the outbox losing bytes it owned (gc, a wiped
        # state dir); a missing SOURCE is a file the producer deleted, moved or
        # unmounted before delivery, which the outbox never copied and could
        # not have kept. Reporting both as "staged bytes are gone" sent people
        # hunting the blob store for a file that was never in it.
        if upload.get("staged"):
            raise errors.ValidationError(
                f"staged bytes for op {op['op_id']} are gone ({source})", status=422
            )
        reason = upload.get("unstaged_reason")
        raise errors.ValidationError(
            f"source file for op {op['op_id']} is gone ({source}) and it was "
            "never staged into the outbox, so there are no bytes to fall back "
            "on -- "
            + (reason if reason else "the op was enqueued with stage=False"),
            status=422,
        )
    digest = upload.get("blob")
    size = upload.get("size_bytes")
    if digest is None:
        # 11A: the big-file path hashed nothing at enqueue.
        digest, size = fingerprint(str(source))
        upload["blob"], upload["size_bytes"] = digest, size
        # Persist the digest BEFORE the rename (red team: the reverse order
        # left a crash window where the op pointed at a staging name that no
        # longer existed, and gc then reaped the renamed blob as unreferenced).
        write_text_atomic(op_path, json.dumps(op, indent=2) + "\n", mode=0o600)
        if upload.get("staged"):
            hashed = journal.blobs_dir / digest
            if hashed.exists():
                source.unlink(missing_ok=True)  # dedup: identical bytes already staged
            else:
                os.replace(source, hashed)
                fsync_directory(journal.blobs_dir)
            source = hashed

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
        # .get, not ["notes"]: a journal written by an older CLI has no such key,
        # and the drainer must replay those ops rather than KeyError on them.
        notes=upload.get("notes"),
        span_id=upload.get("span_id"),
        step_index=upload.get("step_index"),
    )
