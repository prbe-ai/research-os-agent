"""Device-authorization login (RFC 8628) against a mock research-os device API."""

from __future__ import annotations

import httpx
import pytest

from ros.sdk.device import DeviceLoginError, DevicePrompt, device_login

_START = {
    "device_code": "dev-abc",
    "user_code": "WXYZ-1234",
    "verification_uri": "https://dash.test/authorize",
    "verification_uri_complete": "https://dash.test/authorize?code=WXYZ-1234",
    "expires_in": 600,
    "interval": 2,
}


def _client(handler) -> httpx.Client:
    return httpx.Client(base_url="https://api.test", transport=httpx.MockTransport(handler))


def _pending(desc: str = "waiting for browser approval") -> httpx.Response:
    return httpx.Response(400, json={"detail": {"error": "authorization_pending", "error_description": desc}})


def test_device_login_polls_then_returns_token() -> None:
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/device/code":
            return httpx.Response(201, json=_START)
        if request.url.path == "/auth/device/token":
            polls["n"] += 1
            if polls["n"] == 1:
                return _pending()
            return httpx.Response(200, json={"token": "ros_pat_deadbeef"})
        return httpx.Response(404)

    prompts: list[DevicePrompt] = []
    slept: list[float] = []
    token = device_login(
        "https://api.test",
        client=_client(handler),
        open_browser=False,
        on_prompt=prompts.append,
        sleep=slept.append,
    )

    assert token == "ros_pat_deadbeef"
    assert polls["n"] == 2
    assert prompts and prompts[0].user_code == "WXYZ-1234"
    assert slept == [2]  # one interval wait between the pending poll and success


def test_device_login_backs_off_on_slow_down() -> None:
    polls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/device/code":
            return httpx.Response(201, json=_START)
        polls["n"] += 1
        if polls["n"] == 1:
            return httpx.Response(429, json={"detail": {"error": "slow_down", "error_description": "too fast"}})
        return httpx.Response(200, json={"token": "ros_pat_ok"})
    slept: list[float] = []

    token = device_login(
        "https://api.test", client=_client(handler), open_browser=False, sleep=slept.append
    )

    assert token == "ros_pat_ok"
    assert slept == [7]  # interval 2 + 5 back-off


def test_device_login_raises_on_denied() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/device/code":
            return httpx.Response(201, json=_START)
        return httpx.Response(
            400, json={"detail": {"error": "access_denied", "error_description": "the user denied this request"}}
        )

    with pytest.raises(DeviceLoginError, match="denied"):
        device_login("https://api.test", client=_client(handler), open_browser=False, sleep=lambda _s: None)


def test_device_login_raises_when_start_fails() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"detail": {"error": "slow_down", "error_description": "too many requests"}})

    with pytest.raises(DeviceLoginError, match="too many requests"):
        device_login("https://api.test", client=_client(handler), open_browser=False, sleep=lambda _s: None)
