"""the StreamProtocol interface (batch shape, limits, snapshot name, restart rule) and negotiating it from a receipts answer."""

from __future__ import annotations
from .errors import DeliveryPending

#: Upload protocols with streams, cursors and receipts (prbe-knowledge
#: `kb/session_receipts.py`). 2 sends the sanitized probe-events/1 `events`;
#: 3 sends one ATIF fragment per event (`fragments`), plus the events only when
#: the server asked for them as the stream started (`accepts.events`).
PROTOCOL_EVENTS = 2
PROTOCOL_FRAGMENTS = 3
RECEIPTED_PROTOCOLS = (PROTOCOL_EVENTS, PROTOCOL_FRAGMENTS)
#: The protocol of a stream nothing else is recorded for: every stream a tap
#: before protocol 3 started.
PROTOCOL_VERSION = PROTOCOL_EVENTS
#: The server's 409 `detail` when it will not take a protocol-3 batch 0: the
#: customer is not (or no longer) offered protocol 3, or the stream already
#: exists on another protocol. Matched on the text until the door names them
#: with reason constants. Only batch 0 restarts on protocol 2 (`Journal.deliver`).
PROTOCOL3_REFUSALS = ("protocol 3 not enabled", "protocol mismatch")
#: The server's 409 `detail` when it will not START a stream on protocol 2
#: (prbe-knowledge `kb/session_receipts.py` `PROTOCOL2_RETIRED`): protocol 2 is
#: retired for new sessions of a customer offered protocol 3, while a stream
#: the server already holds on 2 keeps being accepted. Only batch 0 of a
#: stream the server does not hold restarts, on protocol 3 (`Journal.deliver`).
PROTOCOL2_RETIRED = "protocol 2 retired"
_SNAPSHOT_SUFFIXES = {PROTOCOL_EVENTS: ".snapshot.jsonl", PROTOCOL_FRAGMENTS: ".snapshot-p3.jsonl"}


def _route(source: str) -> str:
    return _harness(source).route


def fragment_version_for(remote: dict) -> int | None:
    """The fragment version a stream builds, from a receipts answer: the
    highest one both the answer's `accepts.fragment_versions` and this
    builder hold (D21: 2 only where the server takes 2, else 1), or None when
    they share none, the answer offers none, or the builder does not import."""
    accepts = remote.get("accepts")
    versions = accepts.get("fragment_versions") if isinstance(accepts, dict) else None
    if not isinstance(versions, list):
        return None
    offered = {v for v in versions if type(v) is int}
    if not offered:
        return None
    module = _fragment_module()
    shared = offered.intersection(_builder_versions(module)) if module is not None else ()
    return max(shared) if shared else None


def negotiate(remote: dict) -> tuple[int, bool]:
    """(protocol, send events) for a NEW stream, from a receipts answer.

    Protocol 3 only when the answer offers it AND a fragment version this
    client builds (`fragment_version_for`), and the vendored builder imports
    here. No `accepts` at all is an older or self-hosted server: protocol 2.
    `send events` is the server's `accepts.events`, for protocol 3 only.
    """
    accepts = remote.get("accepts")
    if not isinstance(accepts, dict):
        return PROTOCOL_EVENTS, False
    protocols, versions = accepts.get("protocols"), accepts.get("fragment_versions")
    if not (
        isinstance(protocols, list)
        and any(type(p) is int and p == PROTOCOL_FRAGMENTS for p in protocols)
        and isinstance(versions, list)
    ):
        return PROTOCOL_EVENTS, False  # not offered: the builder is never imported
    if fragment_version_for(remote) is None:
        return PROTOCOL_EVENTS, False
    return PROTOCOL_FRAGMENTS, accepts.get("events") is True


def _record_terms(state: dict, remote: dict) -> None:
    """What a receipts answer lets this session's next batches carry, kept in
    its state: the fragment version (`fragment_version_for`; kept as it was
    when the answer names none this builder shares) and whether the server
    takes pasted images (`accepts.media is True`; an answer without `accepts`
    is a server that does not)."""
    accepts = remote.get("accepts")
    state["media"] = isinstance(accepts, dict) and accepts.get("media") is True
    version = fragment_version_for(remote) if isinstance(accepts, dict) else None
    if version is not None:
        state["fragment_version"] = version


def _stream_fragment_version(state: dict, module) -> int:
    """The fragment version a protocol-3 batch of this session is built in:
    the negotiated one, and 1 for a state recorded before versions were."""
    versions = _builder_versions(module)
    stored = state.get("fragment_version")
    if type(stored) is int and stored in versions:
        return stored
    return 1 if 1 in versions else module.FRAGMENT_VERSION


def _stream_protocol(state: dict) -> int:
    """The protocol a session's next batch uses.

    `protocol_version` is the stream's own, recorded once the server holds it
    (the first acknowledged batch, or an existing stream adopted from the
    receipts answer). Without it, a stream the server already holds was started
    by a tap that predates protocol 3; one it does not hold yet starts on what
    was offered (`protocol_offer`), and may still restart on protocol 2.
    """
    pinned = state.get("protocol_version")
    if pinned in RECEIPTED_PROTOCOLS:
        return pinned
    if state.get("last_seq", -1) >= 0:
        return PROTOCOL_EVENTS
    offer = state.get("protocol_offer")
    return offer if offer in RECEIPTED_PROTOCOLS else PROTOCOL_EVENTS


class ProtocolRestarted(DeliveryPending):
    """The server would not take this new session's batch 0 on its protocol,
    so the journal dropped it and the session starts again on `protocol`:
    protocol 3 refused -> 2 (`Journal._restart_refused_offer`), protocol 2
    retired -> 3 (`Journal._restart_retired_protocol2`). Nothing is lost and
    nothing waits: the caller stages again at once. A DeliveryPending
    (retryable) only so a caller that does not know it still keeps the
    session for its next pass."""

    def __init__(self, message: str, protocol: int = PROTOCOL_EVENTS):
        refused = PROTOCOL_EVENTS if protocol == PROTOCOL_FRAGMENTS else PROTOCOL_FRAGMENTS
        super().__init__(
            message + f"; the session restarts on protocol {protocol}",
            retryable=True,
            safe_message=(
                f"the server did not take protocol {refused} for this new session; "
                f"it is sent again on protocol {protocol}"
            ),
        )
        self.protocol = protocol
from .harness import _harness  # noqa: E402 -- pieces need each other; the front door loads this one first
from .stream_protocols.fragments import _builder_versions, _fragment_module  # noqa: E402 -- pieces need each other; the front door loads this one first
