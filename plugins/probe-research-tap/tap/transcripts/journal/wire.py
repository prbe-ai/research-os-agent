"""the ingest routes and the receipts read, with the paired credential and this environment's TLS trust."""

from __future__ import annotations
import json
import os
import ssl
import urllib.error
import urllib.request
from collections.abc import Callable


_ENV_CONTEXTS: dict = {}


def _env_context() -> ssl.SSLContext:
    """TLS trust from THIS environment's SSL_CERT_FILE / SSL_CERT_DIR (cached by
    them). OpenSSL reads those from the process's C environment, which a
    recorder hosted by the Probe service does not have (its session's lives in
    `os.environ`, per thread); passing a context also builds the request's
    proxy handler from the same environment, never a process-wide cache."""
    key = (os.environ.get("SSL_CERT_FILE") or None, os.environ.get("SSL_CERT_DIR") or None)
    context = _ENV_CONTEXTS.get(key)
    if context is None:
        try:
            context = ssl.create_default_context(cafile=key[0], capath=key[1])
        except (OSError, ssl.SSLError, ValueError):
            context = ssl.create_default_context()
        _ENV_CONTEXTS[key] = context
    return context


class Wire:
    """The existing ingest routes and receipt read, using the paired credential."""

    def __init__(
        self, base_url: str, token: str, source: str, timeout: float = 30,
        context: ssl.SSLContext | None = None,
        on_refusal: Callable[[str, int, dict], None] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.source = source
        self.timeout = timeout
        # TLS trust. The CLI passes probe.foundation.tls.tls.ssl_context() (REQUESTS_CA_BUNDLE
        # and friends); the tap's vendored copy cannot import it and passes None,
        # which is urllib's default.
        self.context = context
        # Told every refusal of a session's request (session id, status, error body): the tap
        # and the Probe service tell the session's agent one the server's registry wrote (R12).
        self.on_refusal = on_refusal
        self.last_request_retryable = False
        self.last_request_error = None

    def _forward_refusal(self, code: int, body: dict, path: str, sent: bytes | None) -> None:
        """`on_refusal`, never raising. The session is the route's (`/sessions/<source>/<sid>/...`)
        or, for a batch, its body's, read only for a message from the server's registry."""
        on_refusal = getattr(self, "on_refusal", None)  # a subclass from before it never set it
        detail = body.get("detail")
        if on_refusal is None or code < 400 or not (
                "message_id" in body or (isinstance(detail, dict) and "message_id" in detail)):
            return
        try:
            route = path.split("?", 1)[0].split("/")
            session_id = route[5] if len(route) > 6 and route[3] == "sessions" else None
            if session_id is None and sent:
                session_id = json.loads(sent).get("session_id")
            if isinstance(session_id, str) and session_id:
                on_refusal(session_id, code, body)
        except Exception:  # noqa: BLE001 -- telling the agent is best effort; delivery goes on
            log.debug("refusal not forwarded", exc_info=True)

    def _request(self, path: str, body: bytes | None = None) -> tuple[int, dict]:
        sent = body
        self.last_request_retryable = False
        self.last_request_error = None
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "probe-transcript/2",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.context or _env_context()
            ) as response:
                body = json.loads(response.read())
                if not isinstance(body, dict):
                    raise ValueError("transcript endpoint returned a non-object response")
                return response.status, body
        except urllib.error.HTTPError as exc:
            self.last_request_retryable = _retryable_status(exc.code)
            try:
                body = json.loads(exc.read(8192))
            except (OSError, ValueError):
                return exc.code, {"detail": "invalid error response"}
            if not isinstance(body, dict):
                return exc.code, {"detail": "invalid error response"}
            self._forward_refusal(exc.code, body, path, sent)
            return exc.code, body
        except (OSError, ValueError) as exc:
            self.last_request_retryable = _network_interruption(exc)
            cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            self.last_request_error = type(cause).__name__
            return 0, {"detail": str(exc)}

    def receipts(self, session_id: str, *, after: int = -1, limit: int | None = None) -> dict:
        query = f"?after={after}&limit={limit}" if limit is not None else ""
        code, body = self._request(
            f"/ingest/v1/sessions/{self.source}/{session_id}/receipts{query}"
        )
        if deleted_response(code, body, session_id):
            raise SessionDeleted(session_id)
        # `protocol_version` is the stream's own (2 or 3), and 2 for a session
        # the server holds no stream for; `accepts` says what a new one may use.
        if code != 200 or body.get("protocol_version") not in RECEIPTED_PROTOCOLS:
            raise DeliveryPending(
                f"stream receipts unavailable (http {code}); nothing newly staged",
                retryable=_retryable_status(code) or (code == 0 and self.last_request_retryable),
                safe_message=(
                    "server returned an unsupported receipt protocol"
                    if code == 200 else _request_failure(self, code)
                ),
            )
        if (
            body.get("session_id") != session_id
            or body.get("source") != self.source
            or not body.get("customer_id")
        ):
            raise ReconciliationRequired("receipt identity does not match the requested transcript")
        return body

    def post(self, body: bytes) -> tuple[int, dict]:
        return self._request(f"/ingest/v1/sessions/{_route(self.source)}", body)

    def title(self, session_id: str, title: str) -> tuple[int, dict]:
        """Tell the gateway the name the agent gave this session (research-os
        `capture_session_title`): `{"status": "ok"}`."""
        body = json.dumps({"title": title}, separators=(",", ":"), ensure_ascii=False).encode()
        return self._request(f"/ingest/v1/sessions/{self.source}/{session_id}/title", body)

    def media(self, session_id: str, sha256: str, size_bytes: int, media_type: str) -> tuple[int, dict]:
        """Ask the gateway's media door where one pasted image goes
        (research-os `capture_session_media`): `{"status": "have"}`, or
        `{"status": "upload", "method": "PUT", "url", "headers", ...}`."""
        body = json.dumps(
            {"sha256": sha256, "size_bytes": size_bytes, "media_type": media_type},
            separators=(",", ":"),
        ).encode()
        return self._request(f"/ingest/v1/sessions/{self.source}/{session_id}/media", body)

    def put(self, url: str, data: bytes, headers: dict[str, str]) -> int:
        """PUT an image's bytes to the presigned storage URL the media door
        gave, with exactly its headers (the store refuses a body, length, type
        or checksum other than the signed ones). Never the bearer token: the
        URL is the credential. The HTTP status, or 0 without a response."""
        self.last_request_retryable = False
        self.last_request_error = None
        request = urllib.request.Request(url, data=data, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.context or _env_context()
            ) as response:
                response.read(8192)
                return response.status
        except urllib.error.HTTPError as exc:
            self.last_request_retryable = _retryable_status(exc.code)
            exc.close()
            return exc.code
        except (OSError, ValueError) as exc:
            self.last_request_retryable = _network_interruption(exc)
            cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            self.last_request_error = type(cause).__name__
            return 0
from .errors import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    DeliveryPending,
    ReconciliationRequired,
    SessionDeleted,
    _network_interruption,
    _request_failure,
    _retryable_status,
    deleted_response,
)
from .media.target import log  # noqa: E402 -- pieces need each other; the front door loads this one first
from .stream_protocol import RECEIPTED_PROTOCOLS, _route  # noqa: E402 -- pieces need each other; the front door loads this one first
