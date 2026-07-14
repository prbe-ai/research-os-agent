"""Hosted MCP OAuth discovery: RFC 9728 metadata + WWW-Authenticate challenge."""

from __future__ import annotations

import asyncio
import json

import pytest

from probe.mcp.server import with_auth_and_health


async def _inner(scope: dict, receive, send) -> None:
    """A stand-in for the real MCP app: 200 means the request passed the wrapper."""
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b'{"ok":true}'})


def _call(app, path: str, headers: list | None = None) -> dict:
    out: dict = {}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            out["status"] = msg["status"]
            out["headers"] = {k.decode(): v.decode() for k, v in msg["headers"]}
        elif msg["type"] == "http.response.body":
            out["body"] = msg.get("body", b"")

    scope = {"type": "http", "method": "GET", "path": path, "headers": headers or []}
    asyncio.run(app(scope, receive, send))
    return out


@pytest.fixture
def discovery_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROS_MCP_OAUTH", "1")
    monkeypatch.setenv("ROS_MCP_RESOURCE_URL", "https://mcp.test")
    monkeypatch.setenv("ROS_MCP_AUTH_SERVER", "https://api.test")


def test_protected_resource_metadata(discovery_env) -> None:
    app = with_auth_and_health(_inner)
    res = _call(app, "/.well-known/oauth-protected-resource")
    assert res["status"] == 200
    meta = json.loads(res["body"])
    assert meta["resource"] == "https://mcp.test"
    assert meta["authorization_servers"] == ["https://api.test"]
    assert meta["scopes_supported"] == ["research:read"]


def test_unauthenticated_mcp_request_gets_challenge(discovery_env) -> None:
    app = with_auth_and_health(_inner, mcp_path="/mcp")
    res = _call(app, "/mcp")
    assert res["status"] == 401
    challenge = res["headers"]["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert 'resource_metadata="https://mcp.test/.well-known/oauth-protected-resource"' in challenge
    assert 'scope="research:read"' in challenge


def test_bearer_request_passes_through(discovery_env) -> None:
    app = with_auth_and_health(_inner, mcp_path="/mcp")
    res = _call(app, "/mcp", headers=[(b"authorization", b"Bearer ros_pat_abc")])
    assert res["status"] == 200  # reached the inner app
    assert json.loads(res["body"]) == {"ok": True}


def test_healthz_always_ok(discovery_env) -> None:
    app = with_auth_and_health(_inner)
    res = _call(app, "/healthz")
    assert res["status"] == 200
    assert json.loads(res["body"]) == {"status": "ok"}


def test_discovery_disabled_skips_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ROS_MCP_OAUTH", "0")
    app = with_auth_and_health(_inner, mcp_path="/mcp")
    # No challenge: an unauthenticated request falls through to the inner app.
    assert _call(app, "/mcp")["status"] == 200
    # Metadata endpoint is not served either (falls through to inner).
    assert json.loads(_call(app, "/.well-known/oauth-protected-resource")["body"]) == {"ok": True}
