"""SDK HTTP transport over the research-os v3 contract.

One thin wrapper around httpx that knows the two auth surfaces:
  * ``/v1/*``    -> ``Authorization: Bearer ros_pat_...`` (user API token)
  * ``/ingest/*`` -> ``Authorization: Bearer ros_ing_...`` (+ optional X-Signature HMAC)

It maps HTTP status to the typed exceptions in ``errors`` and retries idempotent
calls on 5xx / network blips with capped backoff. Reads that paginate expose the
``X-Next-Cursor`` response header via :class:`Page`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx

from . import errors
from .config import Settings

_RETRYABLE = {502, 503, 504}
_DEFAULT_TIMEOUT = 30.0
_MAX_RETRIES = 3


@dataclass
class Page:
    """A list response plus the opaque keyset cursor for the next page."""

    items: list[Any]
    next_cursor: str | None


class Transport:
    def __init__(
        self,
        settings: Settings,
        *,
        timeout: float = _DEFAULT_TIMEOUT,
        client: httpx.Client | None = None,
        max_retries: int = _MAX_RETRIES,
    ):
        self.settings = settings
        self.max_retries = max_retries
        self._client = client or httpx.Client(base_url=settings.base_url, timeout=timeout)

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Transport":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- auth ---------------------------------------------------------------
    def _auth_headers(self, path: str, raw_body: bytes) -> dict[str, str]:
        headers: dict[str, str] = {}
        is_ingest = path.startswith("/ingest")
        if is_ingest:
            if not self.settings.ingest_token:
                raise errors.AuthError("no ingest token configured (set ROS_INGEST_TOKEN)")
            headers["Authorization"] = f"Bearer {self.settings.ingest_token}"
            if self.settings.hmac_secret:
                sig = hmac.new(
                    self.settings.hmac_secret.encode(), raw_body, hashlib.sha256
                ).hexdigest()
                headers["X-Signature"] = f"sha256={sig}"
        else:
            if not self.settings.token:
                raise errors.AuthError("no API token configured (run `exp login`)")
            headers["Authorization"] = f"Bearer {self.settings.token}"
        return headers

    # -- core request -------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any | None = None,
        params: dict[str, Any] | None = None,
        idempotent: bool | None = None,
    ) -> httpx.Response:
        # Serialize ourselves so the HMAC signs the exact bytes we send.
        raw_body = b"" if json_body is None else json.dumps(json_body).encode()
        headers = self._auth_headers(path, raw_body)
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        retry = idempotent if idempotent is not None else method.upper() in {"GET", "PUT"}
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._client.request(
                    method,
                    path,
                    content=raw_body if json_body is not None else None,
                    params=params,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                if retry and attempt <= self.max_retries:
                    time.sleep(min(2 ** (attempt - 1) * 0.2, 2.0))
                    continue
                raise errors.TransportError(f"{method} {path}: {exc}") from exc

            if resp.status_code in _RETRYABLE and retry and attempt <= self.max_retries:
                time.sleep(min(2 ** (attempt - 1) * 0.2, 2.0))
                continue

            if resp.status_code >= 400:
                raise self._to_error(resp)
            return resp

    def _to_error(self, resp: httpx.Response) -> errors.RosError:
        try:
            detail = resp.json().get("detail")
        except (json.JSONDecodeError, ValueError, AttributeError):
            detail = resp.text
        return errors.error_for(resp.status_code, detail)

    # -- typed helpers ------------------------------------------------------
    def get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self.request("GET", path, params=params, idempotent=True).json()

    def post(self, path: str, json_body: Any | None = None, *, idempotent: bool = False) -> Any:
        resp = self.request("POST", path, json_body=json_body, idempotent=idempotent)
        return resp.json() if resp.content else None

    def patch(self, path: str, json_body: Any) -> Any:
        return self.request("PATCH", path, json_body=json_body).json()

    def delete(self, path: str) -> None:
        self.request("DELETE", path, idempotent=True)

    def get_page(self, path: str, *, params: dict[str, Any] | None = None) -> Page:
        resp = self.request("GET", path, params=params, idempotent=True)
        return Page(items=resp.json(), next_cursor=resp.headers.get("X-Next-Cursor"))
