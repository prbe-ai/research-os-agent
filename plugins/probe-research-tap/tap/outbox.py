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
from importlib import import_module

from tap import config as cfg
from tap import httpclient
from tap import transcript as _transcript
from tap.storage import Storage

log = logging.getLogger("probe-research-tap.outbox")


class HaltError(Exception):
    """Raised when the server returns 401 — the ingest token is dead, daemon
    must exit. Fixed by setting a valid PROBE_INGEST_TOKEN or re-running
    `probe login`, NOT by any pairing step (there is none)."""


class SanitizerNotAvailable(RuntimeError):
    """The current source's registry row names a sanitizer module that has
    not shipped yet.

    A source can be registered in tap.sources (so pairing, config resolution
    and the webhook route all work) before its sanitizer module exists —
    that gap is deliberate, see tap/sources.py. Without this, hitting that
    gap mid-tick would surface as a bare ModuleNotFoundError with no
    indication of what's actually wrong; this names it instead. Mirrors
    config.APIBaseURLUnset: fail loudly with a specific reason rather than
    letting a stdlib import error stand in for it.
    """


def token_fingerprint(token: str) -> str:
    """Stable fingerprint of the ingest token, for the 401-halt latch.

    Stored (never the token itself) so a daemon start can tell whether the
    credential changed since the 401 — a changed token clears the halt."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sanitizer_for_current_source():
    """The `sanitize_event` callable for whichever source this daemon serves.

    Resolved through the source registry (tap.sources) rather than a
    per-source ternary, so a new harness needs a new row there, not a new
    branch here. The daemon is pinned to one agent for its whole life, so
    reading the source from config here is correct and keeps every existing
    caller unchanged. The shared builder takes the function as a parameter
    instead of looking it up, because an importer processing both agents'
    history in one pass cannot have a single ambient answer — see
    tap_core/transcript.build_batch_body.

    Resolved ONCE per batch (build_batch_body below), not per line — a
    5,000-line tick should do one import-cache lookup, not 5,000.

    Raises SanitizerNotAvailable (not a bare ModuleNotFoundError) when the
    current source's row names a module that isn't installed yet.
    """
    source = cfg.current_source()
    try:
        module = import_module(source.sanitizer_module)
    except ModuleNotFoundError as e:
        # e.name is the module that was actually missing. When it matches the
        # sanitizer module itself, that module has not shipped — the case
        # this exists to name. When it names something else, the sanitizer
        # module exists but one of ITS OWN imports is broken; that is a real
        # bug in shipped code, not a not-yet-shipped source, and relabelling
        # it here would bury the traceback that says what's actually broken.
        if e.name != source.sanitizer_module:
            raise
        raise SanitizerNotAvailable(
            f"{source.source_id!r} is a registered capture source but its "
            f"sanitizer module ({source.sanitizer_module}) has not shipped "
            "yet — PROBE_TAP_SOURCE is pointed at a harness whose capture "
            "path isn't built in this install."
        ) from e
    return module.sanitize_event


def build_batch_body(
    *,
    device_id: str,
    session_id: str,
    batch_seq: int,
    cwd: str,
    base_line_no: int,
    lines: list[bytes],
) -> bytes | None:
    """The wire body for this daemon's /ingest/v1/sessions/{source} route.

    Thin wrapper over the shared builder, binding this daemon's sanitizer —
    whichever source's row (tap.sources) this process is configured for.
    Returns None when every event was dropped: "nothing to ship, but advance
    the offset."
    """
    return _transcript.build_batch_body(
        device_id=device_id,
        session_id=session_id,
        batch_seq=batch_seq,
        cwd=cwd,
        base_line_no=base_line_no,
        lines=lines,
        sanitize=sanitizer_for_current_source(),
    )


build_finalize_body = _transcript.build_finalize_body


#: Meta keys holding what the NEXT SessionStart tells the researcher. The tap
#: itself has no terminal — session-start.sh spawns it detached and its only
#: stdout is `{"continue": true}` — so a redaction it performs now can only be
#: reported at the next session, through the hook's `systemMessage`.
REDACTED_COUNT_KEY = "pending_redaction_count"
REDACTED_RULES_KEY = "pending_redaction_rules"


def _record_redactions(storage: Storage, body: bytes) -> None:
    """Accumulate this batch's redaction report for the next SessionStart.

    Reads the body we are about to spool rather than taking a parameter: every
    producer reaches the wire through `enqueue`, so there is exactly one place
    to account for, and a new caller cannot forget to report.

    Best-effort by construction. A researcher not being told is bad; a tap that
    stops capturing because it could not write a counter is worse.
    """
    try:
        report = json.loads(body).get("redactions")
        if not report:
            return
        count = int(report.get("count") or 0)
        if count <= 0:
            return
        previous = int(storage.get_meta(REDACTED_COUNT_KEY) or 0)
        rules = set(filter(None, (storage.get_meta(REDACTED_RULES_KEY) or "").split(",")))
        rules.update(str(r) for r in report.get("rules") or [])
        storage.set_meta_pair(
            REDACTED_COUNT_KEY, str(previous + count),
            REDACTED_RULES_KEY, ",".join(sorted(rules)),
        )
    except Exception:  # noqa: BLE001 - never let reporting break capture
        log.debug("outbox: could not record redaction report", exc_info=True)


def redaction_notice(storage: Storage, *, clear: bool = True) -> str:
    """One line for the researcher, or "" when there is nothing to say.

    Names the RULES, not just a count: "rotate your AWS key" and "a false
    positive ate a checkpoint path" need different responses from them, and the
    rule id is the only thing that distinguishes the two.
    """
    count = int(storage.get_meta(REDACTED_COUNT_KEY) or 0)
    if count <= 0:
        return ""
    rules = [r for r in (storage.get_meta(REDACTED_RULES_KEY) or "").split(",") if r]
    if clear:
        storage.delete_meta(REDACTED_COUNT_KEY)
        storage.delete_meta(REDACTED_RULES_KEY)
    what = ", ".join(rules) if rules else "credential-shaped values"
    plural = "s" if count != 1 else ""
    return (
        f"probe: redacted {count} credential-shaped value{plural} ({what}) from your last "
        "session before upload. If any of those are live keys, rotate them now."
    )


def enqueue(
    *,
    storage: Storage,
    session_id: str,
    batch_seq: int,
    cwd: str,
    body: bytes,
    now: int,
) -> None:
    _record_redactions(storage, body)
    storage.enqueue_batch(
        session_id=session_id,
        batch_seq=batch_seq,
        cwd=cwd,
        body=body,
        created_at=now,
        next_attempt_at=now,
    )


def drain_once(
    *,
    storage: Storage,
    token: str,
    base_url: str,
    session_id: str | None,
    lease_seconds: int = 120,
) -> bool:
    """Pop the next due batch and POST it.

    `session_id=None` drains across ALL sessions — the reconciler's global pass,
    which is the only thing that ever retries a batch whose session never came
    back. It claims each row with a short lease instead of relying on the
    session scoping for mutual exclusion; `lease_seconds` is ignored in the
    session-scoped mode, where one daemon already owns the session's rows.

    Classification is identical either way: SUCCESS deletes, POISON drops, HALT
    clears + latches and raises.

    Returns True if a row was processed (caller may want to drain again),
    False if there is nothing due. Raises HaltError on 401.
    """
    now = int(time.time())
    if session_id is None:
        row = storage.next_due_batch_any(now, lease_seconds=lease_seconds)
    else:
        row = storage.next_due_batch(now, session_id)
    if row is None:
        storage.enforce_outbox_cap()
        return False

    if not token:
        storage.mark_failure(row.id, now + 30, "no ingest token")
        return True

    url = base_url + cfg.webhook_path()
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
            row.id,
            resp.status,
            resp.body[:200],
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
            "last_401_at",
            str(now),
            "last_401_token_sha256",
            token_fingerprint(token),
        )
        raise HaltError(
            "ingest token rejected (401) — fix PROBE_INGEST_TOKEN or run "
            "`probe login` with a valid ingest token"
        )

    msg = resp.error or f"http {resp.status}"
    next_at = now + int(httpclient.backoff_seconds(row.attempt_count))
    storage.mark_failure(row.id, next_at, msg)
    return True
