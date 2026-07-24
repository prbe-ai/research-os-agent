"""SDK HTTP transport over the Probe Research v3 contract.

One thin wrapper around httpx that knows the two auth surfaces:
  * ``/v1/*``    -> ``Authorization: Bearer probe_pat_...`` (user API token)
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
from collections.abc import Mapping
from typing import Any

import httpx

from ..client_headers import (
    CLIENT_KIND_HEADER,
    CLIENT_VERSION_HEADER,
    client_version_headers,
)
from . import errors
from .config import Settings
from .surface import SURFACE_HEADER, TOOL_HEADER, Surface, current_tool

_RETRYABLE = {502, 503, 504}


def _iter_file(fh, chunk_size: int):
    """Yield an open binary file in fixed-size chunks -- a streaming upload body."""
    while True:
        chunk = fh.read(chunk_size)
        if not chunk:
            break
        yield chunk


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
        surface: str = Surface.SDK.value,
        client_headers: Mapping[str, str] | None = None,
    ):
        self.settings = settings
        self.max_retries = max_retries
        # Which product surface these requests came from (cli/sdk/mcp). Every
        # backend request carries it as `X-Probe-Surface` so analytics can
        # attribute events by surface — headers only, never a payload.
        self.surface = surface
        supplied = client_headers or {}
        self._client_headers = client_version_headers(
            supplied.get(CLIENT_KIND_HEADER),
            supplied.get(CLIENT_VERSION_HEADER),
        )
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
                raise errors.AuthError("no ingest token configured (set PROBE_INGEST_TOKEN)")
            headers["Authorization"] = f"Bearer {self.settings.ingest_token}"
            if self.settings.hmac_secret:
                sig = hmac.new(
                    self.settings.hmac_secret.encode(), raw_body, hashlib.sha256
                ).hexdigest()
                headers["X-Signature"] = f"sha256={sig}"
        else:
            if not self.settings.token:
                raise errors.AuthError("no API token configured (run `probe login`)")
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
        headers = {**self._client_headers, **self._auth_headers(path, raw_body)}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        # Surface attribution: tag every backend request with the originating
        # product surface, and (MCP only) the tool being served. Headers only.
        headers[SURFACE_HEADER] = self.surface
        tool = current_tool()
        if tool:
            headers[TOOL_HEADER] = tool

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

    def delete(self, path: str) -> Any:
        """Returns the parsed body when the route sends one (e.g. DELETE
        /v1/runs/{id} echoes the deleted run), and None on a 204."""
        resp = self.request("DELETE", path, idempotent=True)
        if not resp.content:
            return None
        try:
            return resp.json()
        except (json.JSONDecodeError, ValueError):
            # A 2xx with a non-JSON body (an ingress/CDN HTML interstitial in front of
            # the API) must not escape as a raw JSONDecodeError; the delete succeeded.
            return None

    def get_page(self, path: str, *, params: dict[str, Any] | None = None) -> Page:
        resp = self.request("GET", path, params=params, idempotent=True)
        return Page(items=resp.json(), next_cursor=resp.headers.get("X-Next-Cursor"))

    def put_url(
        self,
        url: str,
        data: bytes,
        *,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        """Raw PUT of bytes to a presigned URL (artifact upload, fold #16).

        No Authorization header: the presigned URL carries its own signature. This
        goes to an absolute URL (R2), not the API base; retried on network blips."""
        request_headers = dict(headers or {})
        if content_type:
            request_headers.setdefault("Content-Type", content_type)
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._client.put(url, content=data, headers=request_headers)
            except httpx.HTTPError as exc:
                if attempt <= self.max_retries:
                    time.sleep(min(2 ** (attempt - 1) * 0.2, 2.0))
                    continue
                raise errors.TransportError(f"PUT {url}: {exc}") from exc
            if resp.status_code in _RETRYABLE and attempt <= self.max_retries:
                time.sleep(min(2 ** (attempt - 1) * 0.2, 2.0))
                continue
            if resp.status_code >= 400:
                raise errors.error_for(resp.status_code, resp.text)
            return

    def put_file(
        self,
        url: str,
        path: str,
        size: int,
        *,
        content_type: str | None = None,
        headers: Mapping[str, str] | None = None,
        chunk_size: int = 1 << 20,
    ) -> None:
        """Stream a local file to a presigned URL without buffering it in memory.

        The sibling of :meth:`put_url` for bytes you do NOT want in memory -- an
        anchored artifact can be model weights, and reading the whole file into a
        ``bytes`` before the PUT is what OOMs the training loop. ``size`` is sent as an
        explicit ``Content-Length``: a generator body otherwise makes httpx use
        ``Transfer-Encoding: chunked``, which a presigned R2/S3 PUT rejects (411). It
        must be the same length that was fingerprinted for the presign.

        Re-opens ``path`` on every attempt: httpx does not rewind a consumed body, so a
        retry after a network blip or a retryable status must stream from byte 0 again
        -- passing a spent file handle would send a truncated body that the server then
        stores and confirms as complete. No Authorization header; the presigned URL
        carries its own signature."""
        request_headers = dict(headers or {})
        if content_type:
            request_headers.setdefault("Content-Type", content_type)
        request_headers["Content-Length"] = str(size)
        attempt = 0
        while True:
            attempt += 1
            try:
                with open(path, "rb") as fh:
                    resp = self._client.put(
                        url,
                        content=_iter_file(fh, chunk_size),
                        headers=request_headers,
                    )
            except httpx.HTTPError as exc:
                if attempt <= self.max_retries:
                    time.sleep(min(2 ** (attempt - 1) * 0.2, 2.0))
                    continue
                raise errors.TransportError(f"PUT {url}: {exc}") from exc
            if resp.status_code in _RETRYABLE and attempt <= self.max_retries:
                time.sleep(min(2 ** (attempt - 1) * 0.2, 2.0))
                continue
            if resp.status_code >= 400:
                raise errors.error_for(resp.status_code, resp.text)
            return

    def get_url(self, url: str) -> bytes:
        """Raw GET of a presigned URL (artifact download); returns the bytes.
        No Authorization header - the presigned URL carries its own signature."""
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self._client.get(url)
            except httpx.HTTPError as exc:
                if attempt <= self.max_retries:
                    time.sleep(min(2 ** (attempt - 1) * 0.2, 2.0))
                    continue
                raise errors.TransportError(f"GET {url}: {exc}") from exc
            if resp.status_code in _RETRYABLE and attempt <= self.max_retries:
                time.sleep(min(2 ** (attempt - 1) * 0.2, 2.0))
                continue
            if resp.status_code >= 400:
                raise errors.error_for(resp.status_code, resp.text)
            return resp.content

    def download_to(
        self, url: str, dest: str, *, chunk_size: int = 1 << 20
    ) -> tuple[int, str]:
        """Stream a presigned-URL GET to ``dest``; return ``(size_bytes, sha256_hex)``.

        The sibling of :meth:`get_url` for the bytes you do NOT want in memory: it
        hashes while it writes, so a caller can verify the blob against a known
        ``content_hash`` without a second pass, and never materialises the whole
        object -- an anchored artifact can be model weights. No Authorization header;
        the presigned URL carries its own signature. Idempotent, so a network blip or
        a retryable status restarts the download from byte 0 (the ``open`` truncates
        ``dest``). A partial file can be left behind only when retries are exhausted
        mid-stream -- the caller owns cleanup on error, same as any failed write."""
        attempt = 0
        while True:
            attempt += 1
            try:
                with self._client.stream("GET", url) as resp:
                    if resp.status_code in _RETRYABLE and attempt <= self.max_retries:
                        time.sleep(min(2 ** (attempt - 1) * 0.2, 2.0))
                        continue
                    if resp.status_code >= 400:
                        resp.read()  # a streamed response has no .text until read
                        raise errors.error_for(resp.status_code, resp.text)
                    hasher = hashlib.sha256()
                    size = 0
                    with open(dest, "wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size):
                            fh.write(chunk)
                            hasher.update(chunk)
                            size += len(chunk)
                return size, hasher.hexdigest()
            except httpx.HTTPError as exc:
                if attempt <= self.max_retries:
                    time.sleep(min(2 ** (attempt - 1) * 0.2, 2.0))
                    continue
                raise errors.TransportError(f"GET {url}: {exc}") from exc
