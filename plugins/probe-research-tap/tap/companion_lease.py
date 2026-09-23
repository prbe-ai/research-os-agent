"""The Probe daemon's write lease: `<state>/probe/sessions/<sid>.writer`.

The session's STATE says what the researcher wants (`daemon`); this file says
whether the daemon is actually doing it. The `probe` CLI's write gate, the
plugin's hooks and the status line read it through
`probe.sdk.session_marker.read_lease` / `daemon_status`; this module is its only
writer. The two packages cannot import each other, so the format is a contract
pinned by `agent/tests/test_companion_lease_contract.py`, which writes with this
module and reads with session_marker.

    {"v": 1, "writer": "daemon", "pid": 123, "expires_at": 1790000000.0,
     "renewed_at": 1789999940.0, "reason": null}

RENEWED ONLY WHEN A CYCLE FINISHES. A worker stuck in a model call stops
renewing, the lease lapses on its TTL, and the agent records again -- fall back
to the agent, never to silence. RELEASED WITH A REASON when the worker stops
on purpose (`unauthorized`, `budget`, `gateway`, `stopped`), so the handback
notice can say why.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

LEASE_VERSION = 1
LEASE_SUFFIX = ".writer"
WRITER = "daemon"

#: How long one renewal holds. Longer than one worker cycle's wall-time ceiling
#: plus its sleep, so a healthy worker never flickers to degraded between
#: cycles; short enough that a dead one hands writing back within minutes.
LEASE_TTL_SECONDS = 240.0

REASON_STOPPED = "stopped"
REASON_UNAUTHORIZED = "unauthorized"
REASON_BUDGET = "budget"
REASON_GATEWAY = "gateway"
#: A worker bug: not the gateway's fault, and the notice should not say it is.
REASON_ERROR = "error"


def sessions_dir() -> Path:
    """`probe.sdk.session_marker.sessions_dir`, restated (the tap cannot import probe)."""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "probe" / "sessions"


def lease_path(session_id: str) -> Path:
    return sessions_dir() / (session_id + LEASE_SUFFIX)


def _publish(path: Path, payload: dict) -> bool:
    """Atomic replace, so a reader never sees half a lease."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except OSError:
        return False


def renew(session_id: str, *, now: float | None = None, ttl: float = LEASE_TTL_SECONDS) -> bool:
    """Hold (or keep holding) the session's writes for `ttl` seconds."""
    at = time.time() if now is None else now
    return _publish(
        lease_path(session_id),
        {
            "v": LEASE_VERSION,
            "writer": WRITER,
            "pid": os.getpid(),
            "expires_at": at + ttl,
            "renewed_at": at,
            "reason": None,
        },
    )


def release(session_id: str, reason: str, *, now: float | None = None) -> bool:
    """Hand writing back to the agent now, saying why."""
    at = time.time() if now is None else now
    return _publish(
        lease_path(session_id),
        {
            "v": LEASE_VERSION,
            "writer": WRITER,
            "pid": os.getpid(),
            "expires_at": at,
            "renewed_at": at,
            "reason": reason,
        },
    )


def session_state(session_id: str) -> str | None:
    """The session's stored switch state (`<sid>.state`), or None.

    Only the canonical file: a session that was never moved has no `.state`, and
    such a session cannot be in `daemon` -- that word exists only in the new file
    (or as a default, which `probe session initialize` writes into `.state` at
    session start).
    """
    try:
        raw = (sessions_dir() / (session_id + ".state")).read_text(encoding="utf-8")
    except OSError:
        return None
    return raw.strip().lower() or None
