"""Build batch payloads, enqueue them, and drain the outbox.

Each event's `raw` is the parsed JSON value (CC's transcript line) with
sanitization applied to strip API metadata that has no content value —
see tap.sanitize for what gets dropped. Bookkeeping-only system events
(e.g. stop_hook_summary, turn_duration) are dropped entirely.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from tap import config as cfg
from tap import httpclient
from tap.sanitize import sanitize_event
from tap.storage import Storage

log = logging.getLogger("probe-research-tap.outbox")


class HaltError(Exception):
    """Raised when the server returns 401 — the ingest token is dead, daemon
    must exit. Fixed by setting a valid PROBE_INGEST_TOKEN or re-running
    `probe login`, NOT by any pairing step (there is none)."""


def token_fingerprint(token: str) -> str:
    """Stable fingerprint of the ingest token, for the 401-halt latch.

    Stored (never the token itself) so a daemon start can tell whether the
    credential changed since the 401 — a changed token clears the halt."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def build_batch_body(
    *,
    device_id: str,
    session_id: str,
    batch_seq: int,
    cwd: str,
    base_line_no: int,
    lines: list[bytes],
) -> bytes | None:
    """Construct the JSON body for /ingest/v1/sessions/claude-code.

    Identity is injected server-side from the ingest token — no employee
    fields here. The backend passes device_id through to the engine, which
    uses it as the device external id.

    Each line is parsed JSON, then run through `sanitize_event` to strip
    Anthropic API metadata (usage, iterations, cache_creation, thinking
    signatures, …) and drop CC-internal bookkeeping events
    (`stop_hook_summary`, `turn_duration`). Lines whose JSON fails to parse
    are kept as raw strings — same lenient fallback as before.

    Returns None if every event was dropped by the sanitizer (e.g. a tick
    that only saw stop_hook_summary + turn_duration). Caller should treat
    None as "nothing to ship, but advance the offset."
    """
    # TODO: split oversized batches client-side so a legitimately large tick
    # isn't dropped. Today a body over the gateway's 2MB cap comes back 413 and
    # is classified POISON (dropped) — see httpclient.classify — because the tap
    # has no batch-splitting. Splitting here would let a big tick ship in parts
    # instead of being lost.
    events = []
    for i, line in enumerate(lines):
        try:
            raw = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            raw = line.decode("utf-8", errors="replace")
        sanitized = sanitize_event(raw)
        if sanitized is None:
            continue
        events.append({"line_no": base_line_no + i, "raw": sanitized})
    if not events:
        return None
    body = {
        "device_id": device_id,
        "session_id": session_id,
        "batch_seq": batch_seq,
        "cwd": cwd,
        "events": events,
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def enqueue(
    *,
    storage: Storage,
    session_id: str,
    batch_seq: int,
    cwd: str,
    body: bytes,
    now: int,
) -> None:
    storage.enqueue_batch(
        session_id=session_id,
        batch_seq=batch_seq,
        cwd=cwd,
        body=body,
        created_at=now,
        next_attempt_at=now,
    )


def drain_once(*, storage: Storage, token: str, base_url: str, session_id: str) -> bool:
    """Pop the next due batch for session_id and POST it.

    Returns True if a row was processed (caller may want to drain again),
    False if this session has nothing due. Raises HaltError on 401.
    """
    now = int(time.time())
    row = storage.next_due_batch(now, session_id)
    if row is None:
        storage.enforce_outbox_cap()
        return False

    if not token:
        storage.mark_failure(row.id, now + 30, "no ingest token")
        return True

    url = base_url + cfg.WEBHOOK_PATH
    resp = httpclient.post_json(url, row.body, bearer=token)

    if resp.classification == httpclient.Classification.SUCCESS:
        storage.mark_success(row.id)
        storage.set_meta("last_successful_post_at", str(now))
        return True
    if resp.classification == httpclient.Classification.POISON:
        # Any non-401 4xx: 400/404 malformed/unroutable, 403 = the backend
        # QUARANTINED this session, 413 = body over the gateway's 2MB cap,
        # 422 = schema rejection. None can succeed on retry of the SAME batch,
        # so the batch is dropped and the daemon keeps running — a per-session /
        # per-batch server-side decision, not a credential failure.
        log.warning(
            "outbox: poison drop id=%d status=%d body=%r",
            row.id, resp.status, resp.body[:200],
        )
        storage.mark_success(row.id)
        return True
    if resp.classification == httpclient.Classification.HALT:
        storage.clear_outbox()
        # Latch the timestamp AND the rejected-credential fingerprint in ONE
        # atomic write. A crash between the two would leave last_401_at set but
        # the fingerprint empty, and the next daemon start could neither prove
        # the token changed nor justify holding the halt. The next start uses
        # the fingerprint to self-clear once the token actually changes, and the
        # timestamp to self-clear after a cooldown (transient 401 re-probe).
        storage.set_meta_pair(
            "last_401_at", str(now),
            "last_401_token_sha256", token_fingerprint(token),
        )
        raise HaltError(
            "ingest token rejected (401) — fix PROBE_INGEST_TOKEN or run "
            "`probe login` with a valid ingest token"
        )

    msg = resp.error or f"http {resp.status}"
    next_at = now + int(httpclient.backoff_seconds(row.attempt_count))
    storage.mark_failure(row.id, next_at, msg)
    return True
