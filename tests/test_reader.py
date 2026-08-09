"""Reader (service-token read client) over httpx.MockTransport — no live backend.

Verifies the probe_svc_ token is the /v1 bearer, filter serialization, artifact
download negotiation (managed bytes / reference pointer / proxy), and pagination.
"""

from __future__ import annotations

import json

import httpx

from probe.sdk.config import Settings
from probe.sdk.reader import Reader, Reference
from probe.sdk.transport import Transport

_SVC = "probe_svc_" + "a" * 32


def _reader(handler) -> Reader:
    client = httpx.Client(base_url="http://test", transport=httpx.MockTransport(handler))
    settings = Settings(base_url="http://test", service_token=_SVC)
    return Reader(Transport(settings, client=client))


def test_reader_is_exported_from_top_level() -> None:
    import probe

    assert probe.Reader is Reader
    assert probe.Reference is Reference


def test_service_token_is_the_v1_bearer() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["auth"] = req.headers.get("authorization")
        return httpx.Response(200, json=[])

    _reader(handler).runs()
    assert seen["auth"] == f"Bearer {_SVC}"


def test_metrics_serializes_coord_and_label_filters() -> None:
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path
        captured["params"] = dict(req.url.params)
        return httpx.Response(200, json=[{"value": 12.0, "labels": {"sample": 2}}])

    pts = _reader(handler).metrics("run1", key="reward", labels={"sample": 2}, dimensions={"rank": 1})
    assert captured["path"] == "/v1/runs/run1/metrics"
    assert captured["params"]["key"] == "reward"
    assert json.loads(captured["params"]["labels"]) == {"sample": 2}
    assert json.loads(captured["params"]["dimensions"]) == {"rank": 1}
    assert pts[0]["value"] == 12.0


def test_download_artifact_managed_returns_bytes() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/download") and req.method == "POST":
            return httpx.Response(200, json={"download_url": "http://blob/x"})
        if str(req.url) == "http://blob/x":
            return httpx.Response(200, content=b"weights")
        return httpx.Response(404, json={"detail": "nope"})

    assert _reader(handler).download_artifact("a1") == b"weights"


def test_download_artifact_reference_returns_pointer() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"detail": {"reason": "reference", "uri": "r2://mine/x"}})

    ref = _reader(handler).download_artifact("a1")
    assert isinstance(ref, Reference) and ref.uri == "r2://mine/x"


def test_download_artifact_proxy_mode_uses_content_route() -> None:
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["path"] = req.url.path
        return httpx.Response(200, content=b"proxied")

    assert _reader(handler).download_artifact("a1", mode="proxy") == b"proxied"
    assert seen["path"] == "/v1/artifacts/a1/content"


def test_runs_paginate_across_cursor() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.params.get("cursor") is None:
            return httpx.Response(200, json=[{"id": "r1"}], headers={"X-Next-Cursor": "c2"})
        return httpx.Response(200, json=[{"id": "r2"}])

    runs = _reader(handler).runs()
    assert [x["id"] for x in runs] == ["r1", "r2"]
