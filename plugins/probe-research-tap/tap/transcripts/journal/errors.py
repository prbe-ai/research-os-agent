"""what can go wrong with a batch (reconciliation, deletion, restarts) and how a failed request is classified."""

from __future__ import annotations
import errno
import socket
import ssl
import urllib.error
import urllib.request


class ReconciliationRequired(RuntimeError):
    def __init__(
        self, message: str, *, safe_message: str | None = None, status_code: int | None = None,
        body_sha256: str | None = None,
    ):
        super().__init__(message)
        self.safe_message = message if safe_message is None else safe_message
        self.status_code = status_code
        #: The digest of the body the server refused (`Journal.deliver`), so
        #: a wait (`Journal.defer_conflict`) lands on that body and no other.
        self.body_sha256 = body_sha256


class RebaseCooling(ReconciliationRequired):
    """A session refused again soon after its journal continued the server's
    stream (`Journal._rebase_on_server`): its batch waits out the 409, and the
    daemon asks `reconcile_pending` again once that wait is over."""


class SourceRewritten(ReconciliationRequired):
    """The transcript's bytes changed inside a range already uploaded or
    reserved: its producer rewrote the file in place. Kimi Code does, on
    resume, when the file's `protocol_version` is older than its own (an
    equal-size rewrite included, so file size proves nothing).

    Final for this session on this machine: the journal keeps refusing it, so
    nothing is re-uploaded and the pending bytes are kept, until the server
    can take a replacement stream. The tap says so once.
    """


class UnsafePending(ReconciliationRequired):
    """A pending batch that today's redaction would change.

    `Journal.reconcile_pending` settles it against the server's receipt.
    """


def recoverable(exc: ReconciliationRequired) -> bool:
    """Whether `Journal.reconcile_pending` may be able to settle `exc`.

    An unsafe pending batch, or a 409 (the server may already hold this
    batch_seq, from a lost response or from another producer).
    """
    return isinstance(exc, UnsafePending) or exc.status_code == 409


class DeliveryPending(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, safe_message: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.safe_message = message if safe_message is None else safe_message


#: How the server says a session was deleted at its customer's request (the
#: engine's `POST /api/session-deletions`, passed through unchanged by the
#: gateway `app/ingestion/sessions_router.py`): HTTP 410 with
#: {"detail": {"reason": "session_deleted", ...}} from the upload door, and
#: `state: "deleted"` from the receipts read. Both are FINAL: the engine refuses
#: every later byte of that session, forever.
DELETED_STATUS = 410
DELETED_REASON = "session_deleted"
DELETED_STATE = "deleted"


class SessionDeleted(DeliveryPending):
    """The server deleted this session at its customer's request. Final.

    Raised from a `Journal` method, the journal has already dropped the
    session's pending batch and snapshot and marked it done
    (`Journal.mark_deleted`), so nothing stages or sends it again. Raised from
    `Wire.receipts`, nothing is recorded yet: the caller marks the journal it
    holds (`mark_deleted` is idempotent). It subclasses DeliveryPending, never
    retryable, only so a caller that does not know it still contains it;
    callers catch it first and log it once. Retrying it was the bug: the upload
    was refused before a byte was stored, every tick, for as long as the daemon
    ran.
    """

    def __init__(self, session_id: str, *, newly: bool = True):
        super().__init__(
            f"session {session_id} was deleted at the customer's request; "
            "its pending upload was dropped and it will not be sent again",
            retryable=False,
            safe_message="session was deleted at the customer's request; it is not uploaded",
        )
        self.session_id = session_id
        #: False when this journal had already recorded the deletion, so a
        #: caller that logs it says so once, not once per process start.
        self.newly = newly


def deleted_response(code: int, body: object, session_id: str | None = None) -> bool:
    """Whether an HTTP answer is the server's final 'this session was deleted'.

    Matched on the body's reason as well as the status: only the server's own
    statement may make this client drop a session's data. With `session_id`,
    a statement naming ANOTHER session is not one about this session.
    """
    detail = body.get("detail") if isinstance(body, dict) else None
    return (
        code == DELETED_STATUS
        and isinstance(detail, dict)
        and detail.get("reason") == DELETED_REASON
        and (session_id is None or detail.get("session_id") in (None, session_id))
    )


def _validate_history(state: dict, expected: tuple[int, str] | None) -> None:
    if expected is not None and (
        state.get("historical_end"), state.get("historical_hash")
    ) != expected:
        raise ReconciliationRequired("approved transcript snapshot changed during reconciliation")


def _request_failure(wire, code: int) -> str:
    """Displayable diagnostics, never an HTTP body, URL or exception string."""
    if code == 0:
        kind = getattr(wire, "last_request_error", None)
        return f"request failed ({kind})" if kind else "request failed without an HTTP response"
    guidance = {
        401: "capture credentials were rejected; reconnect this agent",
        403: "capture access was refused; check this agent's pairing",
        409: "transcript conflicts with accepted data; reconciliation required",
        422: "transcript was rejected; reconciliation required",
    }.get(code, "transcript request was not accepted")
    return f"HTTP {code}: {guidance}"


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
