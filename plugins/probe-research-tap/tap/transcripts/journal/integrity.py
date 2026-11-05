"""content integrity: canonical payloads, prefix hashes, complete line ends, re-scrubbing a pending batch safely."""

from __future__ import annotations
import hashlib
import json
from pathlib import Path
from ..credential_scan.events import redact_event
from ..pinned import open_transcript
EMPTY_HASH = hashlib.sha256(b"").hexdigest()
_FIELDS = (
    "session_id",
    "batch_seq",
    "cwd",
    "events",
    "fragments",
    "fragment_version",
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


def canonical_payload(payload: dict) -> bytes:
    return json.dumps(
        {k: payload[k] for k in _FIELDS if k in payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


#: Scrub passes before giving up on a fixed point. Real content settles in two.
_MAX_SCRUB_PASSES = 8


def _scrub_content(payload: dict) -> dict:
    """Scrub content without rewriting stream IDs, source hashes or cursors.

    Repeats until nothing changes. One pass is not always stable: a marker such
    as `<redacted:probe-token>` contains the word "token" and anchors the next
    value, and a shortened span can bring an anchor within reach of a value.
    `_require_safe_pending` scrubs once more and demands no change, so a body
    staged from a single pass could be refused by its own daemon forever.

    The keys are scrubbed independently (the scrubber joins text only across
    the items of one list), so a part already at its fixed point is unchanged
    by scrubbing it again with the rest.
    """
    content = {key: payload[key] for key in ('events', 'fragments', 'cwd', 'provenance') if key in payload}
    for _ in range(_MAX_SCRUB_PASSES):
        scrubbed, _rules = redact_event(content)
        if scrubbed == content:
            break
        content = scrubbed
    return payload | content


def _require_safe_pending(body: bytes) -> None:
    """An old immutable reservation cannot be silently assigned a new digest.

    It may already have reached the server before a lost response. Preserve
    the exact bytes and require receipt reconciliation instead of replaying a
    detectable credential or rewriting the accepted batch's identity.
    """
    payload = json.loads(body)
    if _scrub_content(payload) != payload:
        raise UnsafePending(
            'pending transcript contains credentials requiring redaction; '
            'reconcile its receipt before rebuilding the reservation'
        )


def _continues(stream: dict, payload: dict) -> bool:
    """Whether a batch picks up exactly where the server's stream ends."""
    return payload["batch_seq"] == stream["last_seq"] + 1 and all(
        payload[f"{unit}_start"] == stream[f"{unit}_end"]
        for unit in ("source_byte", "source_line", "event")
    )


def prefix_hash(path: Path, end: int) -> str:
    digest = hashlib.sha256()
    with open_transcript(path) as handle:
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
    with open_transcript(path) as handle:
        while end:
            start = max(0, end - 64 * 1024)
            handle.seek(start)
            tail = handle.read(end - start)
            newline = tail.rfind(b"\n")
            if newline >= 0:
                return start + newline + 1
            end = start
    return 0
from .errors import ReconciliationRequired, UnsafePending  # noqa: E402 -- pieces need each other; the front door loads this one first
