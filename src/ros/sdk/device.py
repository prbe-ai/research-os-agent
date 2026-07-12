"""RFC 8628 device-authorization login for the CLI (browser-assisted).

``exp login --device`` starts a short-lived request, opens the dashboard so a
signed-in human approves the exact scopes/team, then polls for the minted
``ros_pat``. Mirrors research-os ``/auth/device/*`` with mandatory S256 PKCE:
the browser never sees the token, and the PKCE verifier binds the exchange to
this CLI process. This is the browser alternative to the ``--token`` paste path.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass

import httpx

_START_PATH = "/auth/device/code"
_TOKEN_PATH = "/auth/device/token"
_SLOW_DOWN_BACKOFF = 5


class DeviceLoginError(Exception):
    """The device flow could not complete (denied, expired, or a transport error)."""


@dataclass
class DevicePrompt:
    """What to show the user while they approve in the browser."""

    user_code: str
    verification_uri: str
    verification_uri_complete: str


def _pkce_pair() -> tuple[str, str]:
    """A PKCE (verifier, S256 challenge) pair as unpadded base64url. The verifier
    is 43 chars from 32 random bytes, matching the API's challenge pattern."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _error_code(resp: httpx.Response) -> tuple[str | None, str | None]:
    """Pull ``{detail: {error, error_description}}`` off an error response."""
    try:
        detail = resp.json().get("detail", {})
    except ValueError:
        return None, None
    if isinstance(detail, dict):
        return detail.get("error"), detail.get("error_description")
    return None, str(detail)


def device_login(
    base_url: str,
    *,
    scopes: list[str] | None = None,
    token_name: str = "Research OS CLI",
    open_browser: bool = True,
    on_prompt: Callable[[DevicePrompt], None] | None = None,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Run the device flow and return the minted ``ros_pat`` secret.

    ``scopes=None`` mints a full-role token (read + write) for the CLI; pass e.g.
    ``["read"]`` for a read-only token. ``on_prompt`` receives the verification
    URI/code so the caller can print it; ``open_browser`` also launches it.
    """
    verifier, challenge = _pkce_pair()
    owns_client = client is None
    http = client or httpx.Client(base_url=base_url, timeout=30.0)
    try:
        start = http.post(
            _START_PATH,
            json={"token_name": token_name, "scopes": scopes, "code_challenge": challenge},
        )
        if start.status_code != 201:
            _code, desc = _error_code(start)
            raise DeviceLoginError(desc or f"could not start device login ({start.status_code})")
        data = start.json()

        prompt = DevicePrompt(
            user_code=data["user_code"],
            verification_uri=data["verification_uri"],
            verification_uri_complete=data["verification_uri_complete"],
        )
        if on_prompt is not None:
            on_prompt(prompt)
        if open_browser:
            try:
                webbrowser.open(prompt.verification_uri_complete)
            except webbrowser.Error:
                pass  # headless: the printed URL is the fallback

        device_code = data["device_code"]
        interval = max(1, int(data.get("interval", 5)))
        deadline = monotonic() + int(data.get("expires_in", 600))

        while monotonic() < deadline:
            resp = http.post(
                _TOKEN_PATH,
                json={"device_code": device_code, "code_verifier": verifier},
            )
            if resp.status_code == 200:
                return resp.json()["token"]
            code, desc = _error_code(resp)
            if code == "slow_down":
                interval += _SLOW_DOWN_BACKOFF
            elif code != "authorization_pending":
                raise DeviceLoginError(desc or f"device login failed ({code or resp.status_code})")
            sleep(interval)

        raise DeviceLoginError("device authorization expired before it was approved")
    except httpx.HTTPError as exc:
        raise DeviceLoginError(f"could not reach {base_url}: {exc}") from exc
    finally:
        if owns_client:
            http.close()
