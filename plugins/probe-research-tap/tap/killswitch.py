"""Client-side killswitch check.

Polls the backend's /ingest/v1/sessions/status before each tick, expecting
{"ingest_enabled": bool, "reason": str|null}. When the server says ingestion
is paused, the daemon skips the entire tick: no tail, no enqueue, no drain.
byte_offset stays put so the next enabled tick catches up automatically.

Caches the response for KILLSWITCH_TTL_S to keep poll volume bounded.
On fetch error: returns the last-known value if fresh, else fails OPEN
(continues ingesting). The killswitch is for graceful pause, not
fail-secure — losing reachability to the backend already breaks ingestion,
so failing closed would just amplify a network hiccup into a full halt.

The defense-in-depth path is server-side: the ingest endpoint checks the
same killswitch and rejects when off, so even an old plugin (or one with
a stale cache) gets stopped at the gateway.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from tap import httpclient

log = logging.getLogger("probe-research-tap.killswitch")

# How long to trust a fresh poll. Matches the server's `next_check_after_s=300`
# so a flip propagates within 5 minutes through the proactive path.
KILLSWITCH_TTL_S = 300

# When a fetch fails, we keep using the last cached value as long as it's
# younger than this. Older than this AND failing → fail OPEN.
STALE_FALLBACK_LIMIT_S = 1800

PATH = "/ingest/v1/sessions/status"


@dataclass
class _Cached:
    enabled: bool
    reason: str | None
    fetched_at: float
    fetch_succeeded: bool


_cache: _Cached | None = None


def is_ingestion_enabled(*, token: str, base_url: str) -> tuple[bool, str | None]:
    """Returns (enabled, reason). Cheap to call every tick (cached).

    Network failures fall back to the last-known good value if it's fresh
    enough; otherwise fail OPEN.
    """
    global _cache
    now = time.monotonic()

    if (
        _cache is not None
        and _cache.fetch_succeeded
        and (now - _cache.fetched_at) < KILLSWITCH_TTL_S
    ):
        return _cache.enabled, _cache.reason

    url = base_url.rstrip("/") + PATH
    resp = httpclient.get_json(url, bearer=token)

    if resp.classification == httpclient.Classification.SUCCESS:
        body = httpclient.parse_json(resp)
        enabled = bool(body.get("ingest_enabled", True))
        reason = body.get("reason")
        _cache = _Cached(
            enabled=enabled,
            reason=reason,
            fetched_at=now,
            fetch_succeeded=True,
        )
        return enabled, reason

    # Non-success. Includes HALT (401 — bad token), POISON (400/403/404),
    # RETRY (network error or 5xx). For all of these we'd rather fail OPEN
    # than halt ingestion over a transient backend issue.
    log.warning(
        "killswitch fetch failed: status=%s class=%s err=%s",
        resp.status,
        resp.classification.value,
        resp.error[:200] if resp.error else "",
    )

    if _cache is not None and (now - _cache.fetched_at) < STALE_FALLBACK_LIMIT_S:
        # Use last-known value (it could be the cached fail-open from a
        # prior failure — that's fine, we're still failing open).
        return _cache.enabled, _cache.reason

    # Stale or never fetched + failure → fail open.
    fallback = _Cached(
        enabled=True,
        reason=None,
        fetched_at=now,
        fetch_succeeded=False,
    )
    _cache = fallback
    return True, None


def reset_cache() -> None:
    """Test helper. Production callers rely on the TTL."""
    global _cache
    _cache = None
