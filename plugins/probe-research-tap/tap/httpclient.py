"""HTTP client + classification.

Stdlib-only (urllib) so the plugin ships zero runtime deps. Classification:
Success / Poison / Halt / Retry. Backoff is exponential with jitter.
"""

from __future__ import annotations

import json
import platform
import random
import secrets
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum

from tap import __version__

DEFAULT_TIMEOUT_SECONDS = 30.0
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_CAP_SECONDS = 5 * 60.0


class Classification(StrEnum):
    SUCCESS = "success"
    POISON = "poison"
    HALT = "halt"
    RETRY = "retry"


@dataclass
class Response:
    status: int
    body: bytes
    classification: Classification
    error: str = ""


def classify(status: int, err: bool) -> Classification:
    """Map an HTTP status (or a transport error) to a retry disposition.

    - 2xx                -> SUCCESS
    - 401                -> HALT (dead ingest token; fixed via PROBE_INGEST_TOKEN
                            / `probe login`, checked ahead of the 4xx bucket)
    - any other 4xx      -> POISON (drop + log). A 4xx is a client-side defect the
                            SAME batch can never fix on retry: 400/404 malformed or
                            unroutable, 403 a QUARANTINED session, 413 a body over
                            the gateway's 2MB cap, 422 a schema rejection. The tap
                            has no client-side batch-splitting, so retrying a 413/422
                            forever just re-POSTs a doomed body until the outbox byte
                            cap reaps it — silent data loss + wasted bandwidth. Drop
                            it and keep the daemon running instead.
    - transport error / 5xx / anything else -> RETRY
    """
    if err:
        return Classification.RETRY
    if 200 <= status < 300:
        return Classification.SUCCESS
    if status == 401:
        return Classification.HALT
    if 400 <= status < 500:
        return Classification.POISON
    return Classification.RETRY


def user_agent() -> str:
    return f"probe-research-tap/{__version__} ({platform.system().lower()}/{platform.machine()})"


def trace_id() -> str:
    return secrets.token_hex(16)


def post_json(
    url: str,
    body: bytes,
    *,
    bearer: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Response:
    """POST a JSON body. Returns Response with classification + raw body.

    Network errors return Classification.RETRY with status=0 and the
    exception text in `error`. HTTP errors classify by status code.
    """
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", user_agent())
    req.add_header("X-Trace-Id", trace_id())
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            status = resp.status
            return Response(status=status, body=data, classification=classify(status, False))
    except urllib.error.HTTPError as e:
        # Non-2xx with a response body still goes through here.
        try:
            data = e.read()
        except Exception:
            data = b""
        return Response(
            status=e.code,
            body=data,
            classification=classify(e.code, False),
            error=str(e),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return Response(
            status=0,
            body=b"",
            classification=Classification.RETRY,
            error=str(e),
        )


def get_json(
    url: str,
    *,
    bearer: str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Response:
    """GET a URL expecting JSON back. Same Response + Classification shape
    as post_json so callers can use the same retry/halt logic if needed."""
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", user_agent())
    req.add_header("X-Trace-Id", trace_id())
    if bearer:
        req.add_header("Authorization", f"Bearer {bearer}")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            status = resp.status
            return Response(status=status, body=data, classification=classify(status, False))
    except urllib.error.HTTPError as e:
        try:
            data = e.read()
        except Exception:
            data = b""
        return Response(
            status=e.code,
            body=data,
            classification=classify(e.code, False),
            error=str(e),
        )
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return Response(
            status=0,
            body=b"",
            classification=Classification.RETRY,
            error=str(e),
        )


def parse_json(resp: Response) -> dict:
    if not resp.body:
        return {}
    try:
        return json.loads(resp.body)
    except (ValueError, UnicodeDecodeError):
        return {}


def backoff_seconds(attempt: int) -> float:
    """min(2^attempt * 1s, 5min) + jitter ∈ [0, 1s)."""
    if attempt < 0:
        attempt = 0
    # Avoid overflow on huge attempts.
    if attempt > 30:
        exp = BACKOFF_CAP_SECONDS
    else:
        exp = BACKOFF_BASE_SECONDS * (1 << attempt)
        if exp > BACKOFF_CAP_SECONDS:
            exp = BACKOFF_CAP_SECONDS
    return exp + random.random()
