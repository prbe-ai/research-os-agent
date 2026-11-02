"""open or resume a session's source reservation and negotiate its stream."""

from __future__ import annotations
import hashlib
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path
# Raw history can be much larger than its sanitized pending delivery. Its
# immutable disk copy needs space for the source, not the upload queue limit.
SNAPSHOT_FREE_RESERVE_BYTES = 100 * 1024 * 1024


class EnsureMixin:
    """Part of Journal: its methods run with `self` the Journal; compose() folds them into Journal."""

    def _validate_source(self, state: dict, path: Path):
        body = self.pending(state["session_id"])
        end = json.loads(body)["source_byte_end"] if body else state["source_byte_end"]
        expected = json.loads(body)["prefix_sha256"] if body else state["prefix_sha256"]
        try:
            actual = prefix_hash(path, end)
        except ReconciliationRequired as exc:  # shorter than what was already sent
            raise SourceRewritten(str(exc)) from exc
        if actual != expected:
            raise SourceRewritten("source changed inside an acknowledged or pending range")

    def _reconcile_finalization(self, state: dict, remote: dict) -> bool:
        """Retire only a no-data completion whose exact boundary is accepted.

        Called after source validation, inside ensure's write transaction. A
        different producer can finalize the same prefix with another envelope;
        that does not authorize acknowledging different transcript event bytes.
        """
        stream = remote.get("stream") or {}
        protocol = _stream_protocol(state)
        if (remote.get("protocol_version") != protocol or remote.get("state") != "ready"
                or stream.get("finalized") is not True):
            return False
        row = self.conn.execute(
            "SELECT body,digest FROM pending WHERE session_id=?", (state["session_id"],)
        ).fetchone()
        if row is None:
            return False
        body, digest = bytes(row[0]), row[1]
        if hashlib.sha256(body).hexdigest() != digest:
            return False
        payload = json.loads(body)
        sequence, remote_sequence = payload.get("batch_seq"), stream.get("last_seq")
        if (
            payload.get("protocol_version") != protocol or payload.get("finalize") is not True
            or payload.get("events") not in (None, [])
            or payload.get("fragments") not in (None, [])
            or payload.get("session_id") != state["session_id"]
            or payload.get("stream_id") != state["stream_id"]
            or stream.get("stream_id") != state["stream_id"]
            or type(sequence) is not int or sequence != state["last_seq"] + 1
            or type(remote_sequence) is not int or remote_sequence < sequence
            or payload.get("prefix_sha256") != state["prefix_sha256"]
            or stream.get("prefix_sha256") != state["prefix_sha256"]
        ):
            return False
        for cursor in ("source_byte", "source_line", "event"):
            end = state[cursor + "_end"]
            if (type(end) is not int or end < 0
                    or any(type(item) is not int for item in (
                        payload.get(cursor + "_start"), payload.get(cursor + "_end"),
                        stream.get(cursor + "_end"),
                    ))
                    or payload.get(cursor + "_start") != end
                    or payload.get(cursor + "_end") != end
                    or stream.get(cursor + "_end") != end):
                return False
        removed = self.conn.execute(
            "DELETE FROM pending WHERE session_id=? AND digest=? AND body=?",
            (state["session_id"], digest, body),
        )
        if removed.rowcount != 1:
            return False
        self._drop_media(state["session_id"])
        state.update(last_seq=remote_sequence, finalized=True, error=None)
        if (state.get("historical_end") == state["source_byte_end"]
                and state.get("historical_hash") == state["prefix_sha256"]):
            state["historical_complete"] = True
        return True

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
        reconcile_body: bytes | None = None,
        expected_history: tuple[int, str] | None = None,
        sweep: bool = True,
        carried_history: dict | None = None,
        continue_server_stream: bool = False,
    ) -> dict:
        if (remote.get("customer_id"), remote.get("source"), remote.get("session_id")) != (
            self.customer_id,
            self.source,
            session_id,
        ):
            raise ReconciliationRequired(
                "refusing adoption across transcript destination identities"
            )
        # Final, and before anything else is looked at: a deleted session is
        # never reserved, snapshotted or staged again, whatever else is true.
        self._deleted_remote(session_id, remote, path)
        self._refuse_deleted(session_id)
        if remote.get("state") == "legacy":
            raise ReconciliationRequired(
                "legacy transcript coverage is unverified; reconcile before replay"
            )
        with self.transaction():
            if reconcile_body is not None and (
                self.get(session_id) is None or self.pending(session_id) != reconcile_body
            ):
                raise ReconciliationRequired("pending reservation changed before reconciliation")
            # A process may die between snapshot fsync and the SQLite commit.
            # The write lock proves no other producer is still creating these.
            # Not inside a rebuild (`sweep=False`): it can still roll back to
            # the state that names the snapshot it would find unreferenced.
            if sweep:
                states = self.sessions()
                for item in states:
                    if item.get("historical_complete") and not self.pending(item["session_id"]):
                        snapshot_path = item.get("snapshot_path")
                        if snapshot_path:
                            Path(snapshot_path).unlink(missing_ok=True)
                referenced = {item.get("snapshot_path") for item in states}
                for suffix in _SNAPSHOT_SUFFIXES.values():
                    for orphan in self.directory.glob("*" + suffix):
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
                if remote.get("stream"):
                    # The server holds this stream (another device, or a journal
                    # lost here): it keeps the protocol the server pinned it to,
                    # whatever is offered today, and sends events if asked today.
                    state["protocol_version"] = remote.get("protocol_version")
                    accepts = remote.get("accepts")
                    state["send_events"] = (
                        state["protocol_version"] == PROTOCOL_FRAGMENTS
                        and isinstance(accepts, dict)
                        and accepts.get("events") is True
                    )
                else:
                    state["protocol_offer"], state["send_events"] = negotiate(remote)
                if carried_history:
                    # The frozen history a rebuilt session already had
                    # (`_rebuild_from_server`): the same pin, the same copy.
                    state.update(carried_history, historical_complete=False)
                _record_terms(state, remote)
                self._validate_source(state, path)
            else:
                self._validate_source(state, path)
                # Fresh terms for the next batches: a fragment version and
                # pasted images follow the server, batch by batch (the engine
                # folds mixed versions); the protocol never does.
                _record_terms(state, remote)
                stream = remote.get("stream")
                if stream and stream["stream_id"] != state["stream_id"]:
                    if not continue_server_stream or reconcile_body is not None:
                        raise ReconciliationRequired(
                            "server stream changed; existing pending bytes retained"
                        )
                    # The tap's orphan and stopping passes (`continue_server_stream`)
                    # meet another journal's stream here, before any batch is
                    # posted, so no 409 ever reaches `reconcile_pending`:
                    # continue that stream, as `_rebuild_from_server` proves safe.
                    return self._rebuild_from_server(
                        session_id,
                        state,
                        path,
                        remote,
                        historical=historical,
                        complete_lines_only=complete_lines_only,
                        provenance=provenance or state.get("provenance"),
                        cwd=cwd or state.get("cwd"),
                        expected_history=expected_history,
                    )
                if stream:
                    served, pinned = remote.get("protocol_version"), state.get("protocol_version")
                    if pinned is not None and pinned != served:
                        raise ReconciliationRequired(
                            "server stream uses another upload protocol; pending bytes retained"
                        )
                    if pinned is None and (served == _stream_protocol(state) or not self.pending(session_id)):
                        # The server holds this stream, so its protocol is settled:
                        # record it. An unacknowledged protocol-3 batch 0 the server
                        # will refuse is left to delivery, which restarts it on 2.
                        state["protocol_version"] = served
                        if served != PROTOCOL_FRAGMENTS:
                            state["send_events"] = False
                reconciled = self._reconcile_finalization(state, remote)
                if reconcile_body is not None and not reconciled:
                    raise ReconciliationRequired("pending finalization does not match accepted coverage")
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
            pinned_end = (
                state.get("historical_end")
                if not state.get("historical_complete") else None
            )
            if remote_snapshot is not None and pinned_end is not None and (
                remote_snapshot != pinned_end
                or remote_stream.get("snapshot_sha256") != state.get("historical_hash")
            ):
                raise ReconciliationRequired(
                    "remote historical snapshot differs from the local pinned prefix"
                )
            if freeze_history and (
                not state.get("snapshot_path")
                or not Path(state["snapshot_path"]).exists()
                or extended
            ):
                before = path.stat()
                # Losing a copy (for example after a crash) must not silently
                # extend the previously pinned history to a later live tail.
                snapshot_end = (
                    remote_snapshot if remote_snapshot is not None
                    else pinned_end if pinned_end is not None else before.st_size
                )
                expected_snapshot_hash = (
                    remote_stream.get("snapshot_sha256") if remote_snapshot is not None
                    else state.get("historical_hash") if pinned_end is not None else None
                )
                if complete_lines_only and remote_snapshot is None and pinned_end is None:
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
                if (
                    state["finalized"]
                    and state["source_byte_end"] >= snapshot_end
                    and not self.pending(session_id)
                ):
                    # Receipt/source validation above already proved coverage.
                    # A completed retry needs no second copy of its raw history.
                    snapshot_hash = prefix_hash(path, snapshot_end)
                    if expected_snapshot_hash and snapshot_hash != expected_snapshot_hash:
                        raise ReconciliationRequired(
                            "source differs from the pinned historical snapshot"
                        )
                    state.update(
                        historical_end=snapshot_end,
                        historical_hash=snapshot_hash,
                        historical_complete=True,
                    )
                    _validate_history(state, expected_history)
                    self._save(state)
                    return state
                if (
                    shutil.disk_usage(self.directory).free
                    < snapshot_end + SNAPSHOT_FREE_RESERVE_BYTES
                ):
                    raise DeliveryPending(
                        "not enough free disk space for the transcript snapshot; "
                        "source and pending uploads retained. Free disk space, then resume"
                    )
                snapshot = self.directory / (
                    f"{state['stream_id']}-{uuid.uuid4().hex}"
                    + _SNAPSHOT_SUFFIXES[_stream_protocol(state)]
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
                    if expected_snapshot_hash and snapshot_hash != expected_snapshot_hash:
                        raise ReconciliationRequired(
                            "copied source differs from the pinned historical snapshot"
                        )
                    os.replace(temporary, snapshot)
                    _sync_directory(self.directory)
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
            _validate_history(state, expected_history)
            self._save(state)
            return state
from .errors import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    DeliveryPending,
    ReconciliationRequired,
    SourceRewritten,
    _validate_history,
)
from .integrity import EMPTY_HASH, complete_line_end, prefix_hash  # noqa: E402 -- pieces need each other; the front door loads this one first
from .lock import _sync_directory  # noqa: E402 -- pieces need each other; the front door loads this one first
from .stream_protocol import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    PROTOCOL_FRAGMENTS,
    _SNAPSHOT_SUFFIXES,
    _record_terms,
    _stream_protocol,
    negotiate,
)
