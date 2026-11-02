"""settle a batch that cannot be replayed: restart on the other protocol, rebase on or rebuild from the server's stream."""

from __future__ import annotations
import hashlib
import json
import time
from pathlib import Path
#: What a receipt and a pending batch must share for one to acknowledge the
#: other (`Journal.acknowledge`, `Journal.reconcile_pending`).
_RECEIPT_RANGE = ("batch_seq", "source_byte_end", "source_line_end", "event_end", "prefix_sha256")
#: What a session's state keeps across `Journal._rebuild_from_server`: what
#: this machine decided about the session, not where its stream stands.
_KEPT_ON_REBASE = (
    "live_enabled", "finalize_requested", "custom_title", "ai_title", "title_sent", "title_tried_at",
)
#: Where a session's stream stands: what `Journal._rebase_on_server` checks
#: did not move while it asked the server.
_STREAM_VIEW = (
    "stream_id", "protocol_version", "last_seq", "source_byte_end", "source_line_end", "event_end",
    "prefix_sha256", "finalized",
)


class ReconcileMixin:
    """Part of Journal: its methods run with `self` the Journal; compose() folds them into Journal."""

    def _restart_refused_offer(
        self, session_id: str, body: bytes, result: dict, message: str
    ) -> bool:
        """Restart on protocol 2 a new stream the server will not take on 3.

        Between a receipts answer offering protocol 3 and batch 0 arriving, the
        offer can be withdrawn (a settings rollout, the kill switch), or the
        stream can already exist on protocol 2: the door then answers 409 with
        one of PROTOCOL3_REFUSALS, and would answer it forever. Only batch 0 of
        a stream this journal never had acknowledged qualifies, so no byte the
        server holds is dropped: the pending body goes and the same source range
        is staged again on protocol 2 (which a server holding batch 0 already
        settles through its receipt). Anything else stays a reconciliation.
        """
        detail = result.get("detail") if isinstance(result, dict) else None
        payload = json.loads(body)
        if (
            detail not in PROTOCOL3_REFUSALS
            or payload.get("protocol_version") != PROTOCOL_FRAGMENTS
            or payload.get("batch_seq") != 0
        ):
            return False
        with self.transaction():
            state = self.get(session_id)
            if (
                state is None
                or state.get("protocol_version") is not None
                or state["last_seq"] != -1
                or self.pending(session_id) != body
            ):
                return False
            self.conn.execute("DELETE FROM pending WHERE session_id=?", (session_id,))
            self._drop_media(session_id)
            state.update(protocol_offer=PROTOCOL_EVENTS, send_events=False, error=message)
            self._save(state)
        return True

    def _never_held(self, state: dict | None, session_id: str, body: bytes) -> bool:
        """Whether `body` is batch 0 of a stream this journal never had the
        server acknowledge or report, and still the session's pending body."""
        return (
            state is not None
            and not state.get("deleted")
            and state.get("protocol_version") is None
            and state["last_seq"] == -1
            and self.pending(session_id) == body
        )

    def _restart_retired_protocol2(
        self, session_id: str, body: bytes, result: dict, message: str, wire: Wire
    ) -> bool:
        """Restart on protocol 3 a new stream the server will not START on 2.

        A tap before 0.9.14 (or this one, before protocol 3 was offered)
        staged the session's protocol-2 batch 0, and the server retired
        protocol 2 for new streams before taking it: the door answers 409
        PROTOCOL2_RETIRED, and would forever. Restarting is safe only for batch
        0 of a stream the server does not hold, so both are proven first: the
        journal never had a batch of it acknowledged (no pinned protocol,
        `last_seq` -1, the same pending body), and a fresh receipts answer says
        the server has no stream for the session. That answer also says what a
        new stream may use (`negotiate`); only protocol 3 restarts, anything
        else leaves the batch pending for a later offer.

        Then, in one transaction: the pending protocol-2 body goes, the session
        is offered protocol 3 with that answer's `events`, and a historical
        snapshot gets its protocol-3 name (same bytes, see
        `_keep_snapshot_from_older_taps`); the cursors stay at 0, so the WHOLE
        source is staged again, under the SAME stream id, from batch 0. The
        session stays in `sessions` until the server pins it to 3, so a tap
        before 0.9.14 on this machine keeps seeing it and never starts a
        second stream for it (it may re-send the staged protocol-3 batch 0,
        which the server takes once, as a duplicate after that).
        """
        detail = result.get("detail") if isinstance(result, dict) else None
        payload = json.loads(body)
        if (
            detail != PROTOCOL2_RETIRED
            or payload.get("protocol_version") != PROTOCOL_EVENTS
            or payload.get("batch_seq") != 0
            or not self._never_held(self.get(session_id), session_id, body)
        ):
            return False
        try:
            remote = wire.receipts(session_id)
        except SessionDeleted:
            raise SessionDeleted(session_id, newly=self.mark_deleted(session_id)) from None
        self._deleted_remote(session_id, remote)
        if (
            remote.get("customer_id") != self.customer_id
            or remote.get("state") != "absent"
            or remote.get("stream")
        ):
            return False  # the server holds something for it: reconcile, never restart
        protocol, send_events = negotiate(remote)
        if protocol != PROTOCOL_FRAGMENTS:
            return False  # nothing to restart on: the batch waits for an offer
        with self.transaction():
            state = self.get(session_id)
            if not self._never_held(state, session_id, body):
                return False
            self.conn.execute("DELETE FROM pending WHERE session_id=?", (session_id,))
            self._drop_media(session_id)
            state.update(
                protocol_offer=PROTOCOL_FRAGMENTS, send_events=send_events, error=message
            )
            _record_terms(state, remote)
            self._keep_snapshot_from_older_taps(state)
            self._save(state)
        return True

    def reconcile_pending(self, session_id: str, wire: Wire) -> str | None:
        """Settle a pending batch that cannot be replayed as it is.

        Pending bytes are immutable because the server may already hold them.
        So ask it, for this one batch_seq:

          it has a receipt  -> adopt it. Same cursors and prefix hash means the
          for the same range   same source range, even if another scrub of
                               those bytes was the body it accepted.
          its stream does not line up with this batch (another stream_id, a
          receipt for another range, or a cursor this batch does not continue)
                            -> continue the server's stream instead
                               (`_rebase_on_server`), once its accepted bytes
                               are proven to be this transcript's own prefix.
          it has none, and the body fails today's redaction
                            -> re-redact the SAME reservation in place: same
                               batch_seq, same cursors, only content changes.
                               An older daemon may still be sending the
                               original; whichever copy the server takes, the
                               other is then adopted through its receipt.
          it has none, and the body is clean
                            -> None. Nothing to settle here, and the caller's
                               own error is the more specific one.

        Before this, a batch staged by an older scrubber was refused by every
        newer daemon, and its session stopped shipping for good. And a journal
        that no longer matched the server's stream (two taps on one machine
        raced a session, 2026-10-17..19) re-sent its batch every few seconds
        for days: 409 "batch identity already accepted with different
        content" or "session owned by another stream".
        """
        body = self.pending(session_id)
        state = self.get(session_id)
        if body is None or state is None:
            return "nothing pending"
        payload = json.loads(body)
        sequence = payload["batch_seq"]
        try:
            remote = wire.receipts(session_id, after=sequence - 1, limit=1)
        except SessionDeleted:
            self.mark_deleted(session_id)
            raise
        self._deleted_remote(session_id, remote)
        stream = remote.get("stream") or {}
        receipt = next(
            (item for item in remote.get("receipts") or [] if item.get("batch_seq") == sequence),
            None,
        )
        same_stream = stream.get("stream_id") == state["stream_id"] == payload.get("stream_id")
        if (
            receipt is not None
            and same_stream
            and all(receipt.get(key) == payload[key] for key in _RECEIPT_RANGE)
        ):
            self.acknowledge(
                session_id,
                {"protocol_version": remote.get("protocol_version"), "receipt": receipt},
                sent_body=body,
                content_may_differ=True,
            )
            return f"adopted the server's receipt for batch {sequence}"
        if stream and (not same_stream or receipt is not None or not _continues(stream, payload)):
            return self._rebase_on_server(session_id, state, body, wire)
        scrubbed = _scrub_content(payload)
        if scrubbed == payload:
            return None
        if _scrub_content(scrubbed) != scrubbed:
            raise ReconciliationRequired(
                "redaction does not settle on the pending batch; bytes retained"
            )
        encoded = canonical_payload(scrubbed)
        budget = (
            SERVER_FRAGMENT_BATCH_LIMIT
            if payload.get("protocol_version") == PROTOCOL_FRAGMENTS
            else SERVER_BATCH_LIMIT
        )
        if len(encoded) > budget:
            raise DeliveryPending("re-redacted batch exceeds the route budget; bytes retained")
        with self.transaction():
            updated = self.conn.execute(
                "UPDATE pending SET body=?, digest=? WHERE session_id=? AND body=?",
                (encoded, hashlib.sha256(encoded).hexdigest(), session_id, body),
            )
            if updated.rowcount != 1:
                raise ReconciliationRequired("pending reservation changed before reconciliation")
        return f"re-redacted batch {sequence} in place; the server holds no copy of it"

    def _rebase_on_server(self, session_id: str, state: dict, body: bytes, wire: Wire) -> str:
        """Drop a pending batch the server's stream will never take and go on
        from where that stream ends (`_rebuild_from_server`).

        A refusal carries the 409 and this body's digest, so the batch waits
        out its conflict (`defer_conflict`) like any other lasting 409, and so
        does a session refused again within `REBASE_COOLDOWN_SECONDS` of its
        last rebuild: another producer is racing it, and continuing the
        stream every time would only trade refusals with that producer.
        """
        digest = hashlib.sha256(body).hexdigest()
        rebased_at = state.get("rebased_at")
        if type(rebased_at) in (int, float) and 0 <= time.time() - rebased_at < REBASE_COOLDOWN_SECONDS:
            raise RebaseCooling(
                "refused again soon after continuing the server's stream; "
                "another producer is racing this session",
                status_code=409,
                body_sha256=digest,
            )
        remote = wire.receipts(session_id)
        self._deleted_remote(session_id, remote)
        dropped = json.loads(body)["batch_seq"]
        try:
            with self.transaction():
                current = self.get(session_id)
                if (
                    self.pending(session_id) != body
                    or current is None
                    or [current.get(k) for k in _STREAM_VIEW] != [state.get(k) for k in _STREAM_VIEW]
                ):
                    raise ReconciliationRequired("pending reservation changed before reconciliation")
                rebuilt = self._rebuild_from_server(
                    session_id,
                    current,
                    Path(current["path"]),
                    remote,
                    historical=not current.get("live_enabled", False),
                    complete_lines_only=True,
                    provenance=current.get("provenance"),
                    cwd=current.get("cwd"),
                )
        except SourceRewritten:
            raise
        except ReconciliationRequired as exc:
            raise ReconciliationRequired(str(exc), status_code=409, body_sha256=digest) from exc
        return (
            f"dropped pending batch {dropped}; continuing the server's stream "
            f"from batch {rebuilt['last_seq'] + 1}"
        )

    def _rebuild_from_server(
        self,
        session_id: str,
        state: dict,
        path: Path,
        remote: dict,
        *,
        historical: bool,
        complete_lines_only: bool,
        provenance: dict | None,
        cwd: str | None,
        expected_history: tuple[int, str] | None = None,
    ) -> dict:
        """Replace this journal's state for a session with the server's stream
        of it, as `ensure` builds it for a journal lost here. Inside the
        caller's transaction: a refusal anywhere leaves the journal as it was.

        Only once the server's accepted bytes are proven to be this
        transcript's first bytes (its `prefix_sha256`): then nothing the
        server holds is lost or doubled, and bytes this journal had staged
        past them are staged again from the file. A source rewritten inside
        what this journal sent or staged stays `SourceRewritten`, never
        re-uploaded. A frozen history that still covers the server's stream
        is kept, copy and all; otherwise `ensure` freezes complete lines only.
        """
        stream = remote.get("stream")
        if not stream:
            raise ReconciliationRequired("server holds no stream to continue; pending bytes retained")
        try:
            self._validate_source(state, path)  # SourceRewritten stays itself
        except OSError as exc:
            raise ReconciliationRequired("source is unreadable; pending bytes retained") from exc
        try:
            held = prefix_hash(path, stream["source_byte_end"])
        except OSError as exc:
            raise ReconciliationRequired("source is unreadable; pending bytes retained") from exc
        except ReconciliationRequired as exc:  # the file is shorter than the stream
            raise ReconciliationRequired(
                "server stream holds more than this source; pending bytes retained"
            ) from exc
        if held != stream["prefix_sha256"]:
            raise ReconciliationRequired(
                "server stream holds another source prefix; pending bytes retained"
            )
        history = None
        snapshot = state.get("snapshot_path")
        server_pin = None if stream.get("finalized") else stream.get("snapshot_byte_end")
        if (
            snapshot
            and not state.get("historical_complete")
            and state.get("historical_end", -1) >= stream["source_byte_end"]
            and (
                server_pin is None
                or (server_pin, stream.get("snapshot_sha256"))
                == (state.get("historical_end"), state.get("historical_hash"))
            )
        ):
            try:
                if prefix_hash(Path(snapshot), stream["source_byte_end"]) == stream["prefix_sha256"]:
                    history = {
                        key: state[key]
                        for key in ("snapshot_path", "historical_end", "historical_hash")
                    }
            except (OSError, ReconciliationRequired):
                history = None  # a lost or short copy: `ensure` freezes a new one
        kept = {key: state[key] for key in _KEPT_ON_REBASE if key in state}
        with self.transaction():
            self.conn.execute("DELETE FROM pending WHERE session_id=?", (session_id,))
            self._drop_media(session_id)
            for table in (_FRAGMENT_SESSIONS, "sessions"):
                self.conn.execute(f"DELETE FROM {table} WHERE session_id=?", (session_id,))
            rebuilt = self.ensure(
                session_id,
                path,
                remote,
                historical=historical,
                provenance=provenance,
                cwd=cwd,
                complete_lines_only=complete_lines_only,
                expected_history=expected_history,
                sweep=False,
                carried_history=history,
            )
            rebuilt.update(kept, rebased_at=time.time())
            self._save(rebuilt)
        return rebuilt
from .delivery import REBASE_COOLDOWN_SECONDS  # noqa: E402 -- pieces need each other; the front door loads this one first
from .errors import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    DeliveryPending,
    RebaseCooling,
    ReconciliationRequired,
    SessionDeleted,
    SourceRewritten,
)
from .integrity import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    _continues,
    _scrub_content,
    canonical_payload,
    prefix_hash,
)
from .stream_protocol import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    PROTOCOL2_RETIRED,
    PROTOCOL3_REFUSALS,
    PROTOCOL_EVENTS,
    PROTOCOL_FRAGMENTS,
    _record_terms,
    negotiate,
)
from .stream_protocols.events import SERVER_BATCH_LIMIT  # noqa: E402 -- pieces need each other; the front door loads this one first
from .stream_protocols.fragments import SERVER_FRAGMENT_BATCH_LIMIT, _FRAGMENT_SESSIONS  # noqa: E402 -- pieces need each other; the front door loads this one first
from .wire import Wire  # noqa: E402 -- pieces need each other; the front door loads this one first
