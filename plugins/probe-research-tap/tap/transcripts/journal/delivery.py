"""deliver the pending batch, retire it on acknowledgment, wait out a lasting 409, name the session."""

from __future__ import annotations
import hashlib
import json
import random
import time

from ..sanitizers import claude_code as sanitize
from ..credential_scan.credential_scan import redact
#: How often a session with no batch to send retries a refused title.
TITLE_RETRY_SECONDS = 600
#: A pending batch the server still refuses with 409 after `reconcile_pending`
#: could not settle it waits before it is sent again: the same bytes get the
#: same 409, and each refusal costs the server a full credential scrub of the
#: body (one machine re-sent six refused sessions' batches, up to 1.1 MB each,
#: every ~10 s, for good). The first wait is CONFLICT_RETRY_MIN_SECONDS, doubling with each
#: further refusal up to CONFLICT_RETRY_MAX_SECONDS; each is shortened at random
#: by up to CONFLICT_RETRY_JITTER of itself, so refused sessions do not re-send
#: in step. See `conflict_retry_seconds` and `Journal.defer_conflict`.
CONFLICT_RETRY_MIN_SECONDS = 60
#: A session refused again this soon after its journal continued the server's
#: stream has another producer racing it (a second journal on this machine):
#: its batch waits out the 409 instead of continuing the stream again.
REBASE_COOLDOWN_SECONDS = 600
CONFLICT_RETRY_MAX_SECONDS = 3600
CONFLICT_RETRY_JITTER = 0.25


def conflict_retry_seconds(refusals: int) -> float:
    """How long a batch waits after its `refusals`-th lasting 409 (1 = the first)."""
    doublings = min(max(refusals, 1) - 1, 16)
    wait = min(CONFLICT_RETRY_MAX_SECONDS, CONFLICT_RETRY_MIN_SECONDS * 2**doublings)
    return wait * random.uniform(1 - CONFLICT_RETRY_JITTER, 1)


class DeliveryMixin:
    """Part of Journal: its methods run with `self` the Journal; compose() folds them into Journal."""

    def acknowledge(
        self,
        session_id: str,
        result: dict,
        *,
        sent_body: bytes | None = None,
        content_may_differ: bool = False,
    ):
        """Retire the pending batch the server acknowledged.

        `content_may_differ` accepts a receipt for the same source range whose
        body differs only in content (another scrub of the same bytes): every
        cursor and the prefix hash must still match exactly.
        """
        with self.transaction():
            pending = self.pending(session_id)
            body = sent_body if sent_body is not None else pending
            if body is None:
                return
            payload = json.loads(body)
            state = self.get(session_id)
            if state is None or payload.get("stream_id") != state["stream_id"]:
                # Another producer's reservation for this session (the pending
                # row is per session, not per stream): its receipt says nothing
                # about the stream this journal continues.
                raise ReconciliationRequired(
                    "pending batch belongs to another stream of this session; bytes retained"
                )
            receipt = result.get("receipt") or {}
            # The protocol the server holds this stream on. A body of another
            # protocol is only ever adopted through a receipt for its range.
            accepted = result.get("protocol_version")
            if accepted not in RECEIPTED_PROTOCOLS or (
                not content_may_differ
                and (
                    accepted != payload.get("protocol_version")
                    or receipt.get("body_sha256") != hashlib.sha256(body).hexdigest()
                )
            ):
                raise ReconciliationRequired(
                    "server acknowledgment does not match the immutable pending batch"
                )
            for key in _RECEIPT_RANGE:
                if receipt.get(key) != payload[key]:
                    raise ReconciliationRequired(
                        "server acknowledgment cursor does not match the pending batch"
                    )
            if receipt.get("finalized") != bool(payload.get("finalize")):
                raise ReconciliationRequired(
                    "server acknowledgment finalized a different stream boundary"
                )
            if state["last_seq"] >= payload["batch_seq"]:
                # Another compatible drainer committed this receipt and may
                # already have reserved its successor. Never retire that row.
                return
            if pending != body:
                raise ReconciliationRequired("pending reservation changed before acknowledgment")
            if state.get("protocol_version") not in (None, accepted):
                raise ReconciliationRequired(
                    "server stream uses another upload protocol; pending bytes retained"
                )
            # The server holds the stream now, so its protocol is settled for good.
            state["protocol_version"] = accepted
            if accepted != PROTOCOL_FRAGMENTS:
                state["send_events"] = False
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
                    # When the server last took this session's transcript
                    # (epoch seconds): the desktop's "Last received" (plan
                    # D29, `probe.service.receipts`). A journal row from an
                    # older tap has none until its next receipt.
                    "received_at": time.time(),
                }
            )
            # Accepted: a wait out of a lasting 409 (`defer_conflict`) is over.
            state.pop("conflict_wait", None)
            if (
                state.get("historical_end") is not None
                and state["finalized"]
                and state["source_byte_end"] >= state["historical_end"]
            ):
                state["historical_complete"] = True
            self._save(state)
            self.conn.execute("DELETE FROM pending WHERE session_id=?", (session_id,))
            self._drop_media(session_id)

    def deliver(self, session_id: str, wire: Wire, *, expected_body: bytes | None = None) -> bool:
        # A compatible drainer may already have acknowledged the staged body
        # and reserved a successor. Replay the caller's immutable bytes only;
        # acknowledge's cursor guard leaves that newer reservation untouched.
        self._refuse_deleted(session_id)
        body = expected_body if expected_body is not None else self.pending(session_id)
        if body is None:
            return False
        _require_safe_pending(body)
        self._upload_media(session_id, body, wire)
        code, result = wire.post(body)
        if deleted_response(code, result, session_id):
            raise SessionDeleted(session_id, newly=self.mark_deleted(session_id))
        if code != 202:
            message = f"http {code}: {result.get('detail', 'transcript delivery pending')}"
            if code == 409 and self._restart_refused_offer(session_id, body, result, message):
                raise ProtocolRestarted(message)
            if code == 409 and self._restart_retired_protocol2(
                session_id, body, result, message, wire
            ):
                raise ProtocolRestarted(message, PROTOCOL_FRAGMENTS)
            self.update(session_id, error=message)
            if code in (409, 422):
                raise ReconciliationRequired(
                    message, safe_message=_request_failure(wire, code), status_code=code,
                    body_sha256=hashlib.sha256(body).hexdigest(),
                )
            raise DeliveryPending(
                message,
                retryable=_retryable_status(code) or (
                    code == 0 and getattr(wire, "last_request_retryable", False)
                ),
                safe_message=_request_failure(wire, code),
            )
        self.acknowledge(session_id, result, sent_body=body)
        try:
            self.send_title(session_id, wire)
        except Exception:  # noqa: BLE001
            # The batch is delivered and acknowledged; its session's name is
            # cosmetic and goes again later.
            log.debug("session title not sent", exc_info=True)
        return True

    def send_title(self, session_id: str, wire: Wire, *, idle: bool = False) -> None:
        """Name the session on the server: the title the agent gave it
        (`sanitize.session_title`), whenever it differs from the last one the
        server took.

        BESIDE THE BATCH, NOT IN IT. A batch's every key is digested by the
        engine's receipts door (`_FIELDS`), so a key only the gateway reads would
        break every receipt. `deliver` sends it after each batch lands; a caller
        with no batch to send passes `idle`, which retries a refused title at
        most every TITLE_RETRY_SECONDS -- the case of a session whose LAST batch
        was followed by a refusal (a network drop, a server without the door, a
        laptop change the server had not seen yet), which no later batch fixes.
        """
        state = self.get(session_id)
        title = state and (state.get("custom_title") or state.get("ai_title"))
        if not title:
            return
        # The batch is scrubbed by `_scrub_content`; this string goes on its own,
        # to the same fixed point, and is cut only after (a cut could split a
        # credential into a fragment no rule recognises).
        for _ in range(_MAX_SCRUB_PASSES):
            redacted = redact(title)[0]
            if redacted == title:
                break
            title = redacted
        title = title[: sanitize.TITLE_MAX_LEN]
        if state.get("title_sent") == title:
            return
        if idle and time.time() - state.get("title_tried_at", 0) < TITLE_RETRY_SECONDS:
            return
        code, _ = wire.title(session_id, title)
        if code == 200:
            self.update(session_id, title_sent=title)
        else:
            self.update(session_id, title_tried_at=time.time())

    def _pending_digest(self, session_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT digest FROM pending WHERE session_id=?", (session_id,)
        ).fetchone()
        return row[0] if row else None

    @staticmethod
    def _conflict_left(wait: dict, now: float) -> float:
        """Seconds left of a recorded wait: 0 once it is over, and 0 when the
        clock was set back past it (no wait is ever longer than the cap)."""
        until = wait.get("until")
        if type(until) not in (int, float):
            return 0.0
        left = until - now
        return left if 0 < left <= CONFLICT_RETRY_MAX_SECONDS + 1 else 0.0

    def defer_conflict(self, session_id: str, refused: str | None = None) -> float:
        """Hold back the session's pending batch after a 409 that
        `reconcile_pending` left standing. Returns the seconds until it may be
        sent again (`conflict_retry_seconds`); 0 with nothing pending, or when
        the pending body is no longer `refused` (the digest of the body the
        server refused): another drainer settled it meanwhile, and its
        successor has earned no wait.

        The wait is kept in the session's state (`conflict_wait`), so it
        outlives the process and binds every tap and Probe service recorder on
        this machine, each of which drains every session in this journal. It
        names the pending body's digest, so it is the session's alone and ends
        with that body: acknowledged, re-redacted, dropped or replaced, the
        next body starts from the first wait. A refusal another drainer has
        already deferred is not counted twice.
        """
        with self.transaction():
            state = self.get(session_id)
            digest = self._pending_digest(session_id)
            if state is None or digest is None or refused not in (None, digest):
                return 0.0
            now = time.time()
            wait = state.get("conflict_wait")
            if not isinstance(wait, dict) or wait.get("digest") != digest:
                wait = {}
            left = self._conflict_left(wait, now)
            if left:
                return left
            refusals = wait.get("refusals")
            refusals = (refusals if type(refusals) is int else 0) + 1
            delay = conflict_retry_seconds(refusals)
            state["conflict_wait"] = {"digest": digest, "refusals": refusals, "until": now + delay}
            self._save(state)
        return delay

    def conflict_retry_in(self, session_id: str) -> float:
        """Seconds the session's pending batch still waits out a lasting 409
        (`defer_conflict`); 0 when it may be sent."""
        wait = (self.get(session_id) or {}).get("conflict_wait")
        if not isinstance(wait, dict) or wait.get("digest") != self._pending_digest(session_id):
            return 0.0
        return self._conflict_left(wait, time.time())
from .errors import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    DeliveryPending,
    ReconciliationRequired,
    SessionDeleted,
    _request_failure,
    _retryable_status,
    deleted_response,
)
from .integrity import _MAX_SCRUB_PASSES, _require_safe_pending  # noqa: E402 -- pieces need each other; the front door loads this one first
from .media.target import log  # noqa: E402 -- pieces need each other; the front door loads this one first
from .reconcile import _RECEIPT_RANGE  # noqa: E402 -- pieces need each other; the front door loads this one first
from .stream_protocol import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    PROTOCOL_FRAGMENTS,
    ProtocolRestarted,
    RECEIPTED_PROTOCOLS,
)
from .wire import Wire  # noqa: E402 -- pieces need each other; the front door loads this one first
