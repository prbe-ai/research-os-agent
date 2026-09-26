"""The Probe daemon's network: the model gateway and the write routes. Stdlib only.

Authenticates with the context's `companion_token` -- the daemon's own
`[read, write]` credential, minted by device approval (`probe companion
authorize`, or choosing `daemon` in the wizard). Never the capture token (that
can only upload transcripts) and never the CLI's PAT (that can delete).

Every write carries `Idempotency-Key`: the server stores the first result for a
key and replays it, so the worker's crash-replay of a persisted proposal can
never double-write. Failures are sorted into the few classes the worker acts on:

    401 .................................. Unauthorized  -> release lease, stop until the key changes
    403 .................................. Forbidden     -> a read: hold that proposal;
                                                            the gateway: hand back, long cooldown
    429 {"code": "budget_exhausted"} ..... Budget        -> release lease, back off
    429 (other), 409 in-progress ......... Retryable     -> retry within the cycle
    5xx, timeout, connection ............. Retryable
    other 4xx ............................ Rejected      -> that proposal fails
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tap import config as cfg

GATEWAY_PATH = "/v1/companion/complete"
JUDGE_PATH = "/v1/companion/judge"
DEFAULT_TIMEOUT = 30.0
GATEWAY_TIMEOUT = 120.0
USER_AGENT = "probe-companion/1"


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: Any = None, message: str = "") -> None:
        super().__init__(message or f"HTTP {status}: {detail!r}"[:500])
        self.status = status
        self.detail = detail


class Unauthorized(ApiError):
    pass


class Forbidden(ApiError):
    pass


class Budget(ApiError):
    pass


class Retryable(ApiError):
    pass


class Rejected(ApiError):
    pass


def companion_token() -> str | None:
    value = cfg._read_probe_config().get("companion_token")
    return value.strip() if isinstance(value, str) and value.strip() else None


def error_code(detail: Any) -> str | None:
    return _code(detail)


def _code(detail: Any) -> str | None:
    if isinstance(detail, dict):
        inner = detail.get("detail", detail.get("error", detail))
        if isinstance(inner, dict) and isinstance(inner.get("code"), str):
            return inner["code"]
    return None


def _classify(status: int, detail: Any) -> ApiError:
    if status == 401:
        return Unauthorized(status, detail)
    if status == 403:
        return Forbidden(status, detail)
    if status == 429:
        # `daemon_fuse_tripped`: the team's daily daemon fuse (server S2) -- a budget
        # stop until midnight UTC, never "retry now".
        return (Budget if _code(detail) in ("budget_exhausted", "daemon_fuse_tripped") else Retryable)(status, detail)
    if status in (409, 503) and _code(detail) in ("idempotency_in_progress", "idempotency_unavailable"):
        return Retryable(status, detail)
    if status >= 500:
        return Retryable(status, detail)
    return Rejected(status, detail)


class Api:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        #: Wall-clock instant no request may outlive (the worker sets it to the
        #: lease's expiry minus a margin before it writes). None: unbounded.
        self.deadline: float | None = None

    def _bounded(self, timeout: float) -> float:
        if self.deadline is None:
            return timeout
        left = self.deadline - time.time()
        if left <= 1:
            raise Retryable(0, None, "no lease left to finish a request")
        return min(timeout, left)

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        idem_key: str | None = None,
        params: dict | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Any:
        url = self.base_url + path
        if params:
            from urllib.parse import urlencode

            url += "?" + urlencode({k: v for k, v in params.items() if v is not None})
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        if idem_key:
            headers["Idempotency-Key"] = idem_key
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        timeout = self._bounded(timeout)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https base
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read() or b""
            try:
                detail = json.loads(raw)
            except ValueError:
                detail = raw[:300].decode("utf-8", "replace")
            raise _classify(exc.code, detail) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise Retryable(0, None, f"{type(exc).__name__}: {exc}") from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def get(self, path: str, **params: Any) -> Any:
        return self.request("GET", path, params=params or None)

    def complete(
        self, messages: list[dict], *, max_tokens: int | None = None, timeout: float = GATEWAY_TIMEOUT
    ) -> dict:
        """One gateway call; returns `{"content", "model", "usage", "finish_reason"}`."""
        body: dict[str, Any] = {"messages": messages, "json": True}
        if max_tokens:
            body["max_tokens"] = max_tokens
        out = self.request("POST", GATEWAY_PATH, body, timeout=timeout)
        if not isinstance(out, dict) or not isinstance(out.get("content"), str):
            raise Retryable(502, out, "gateway answered without content")
        return out

    def judge(self, text: str, questions: dict[str, str], *, notes: list[str] | None = None,
              timeout: float = 30) -> dict:
        """Jev's probability per yes/no question about `text`:
        `{"answers", "model", "error", "elapsed_ms"}`. A Jev failure is `error`
        with no answers (a 200), never a zero probability."""
        body = {"state": {"text": text, "notes": list(notes or [])}, "questions": questions}
        out = self.request("POST", JUDGE_PATH, body, timeout=timeout)
        if not isinstance(out, dict):
            raise Retryable(502, out, "the judge answered without a body")
        return out

    def put_file(self, url: str, path: Path, *, content_type: str, headers: dict | None = None) -> None:
        """PUT bytes to a presigned upload URL, streamed (no auth header: the URL is the grant)."""
        size = path.stat().st_size
        with path.open("rb") as handle:
            req = urllib.request.Request(
                url,
                data=handle,
                method="PUT",
                headers={"Content-Type": content_type, "Content-Length": str(size), **(headers or {})},
            )
            try:
                with urllib.request.urlopen(req, timeout=self._bounded(300)) as resp:  # noqa: S310 - server-presigned URL
                    resp.read()
            except urllib.error.HTTPError as exc:
                # The presigned URL is the grant, not the daemon's key: a 4xx here
                # (expired or refused URL) fails this upload only.
                cls = Retryable if exc.code >= 500 else Rejected
                raise cls(exc.code, exc.read()[:300]) from None
            except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
                raise Retryable(0, None, f"{type(exc).__name__}: {exc}") from None
