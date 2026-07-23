"""Client-version telemetry is explicit, bounded, and absent from generic SDK use."""

from __future__ import annotations

import httpx
import pytest

from probe.client_headers import client_version_headers
from probe.sdk.client import Client
from probe.sdk.config import Settings
from probe.sdk.surface import SURFACE_HEADER, TOOL_HEADER, Surface
from probe.sdk.transport import Transport


@pytest.mark.parametrize(
    "version",
    ["0.8.0", "0.8.0-rc.1", "1.2.3+build.4", "1.2.3-beta.2+linux.arm64"],
)
def test_supported_installed_versions_are_header_safe(version: str) -> None:
    assert client_version_headers("cli", version) == {
        "X-Probe-Client": "cli",
        "X-Probe-Client-Version": version,
    }


@pytest.mark.parametrize(
    ("kind", "version"),
    [
        ("sdk", "0.8.0"),
        ("CLI", "0.8.0"),
        ("cli", ""),
        ("cli", "latest"),
        ("cli", "0.8.0rc1"),
        ("cli", "0.0.0.dev0"),
        ("cli", "1.2.3-01"),
        ("cli", "0.8.0\nX-Evil: yes"),
        ("plugin", "0.8.0/../../"),
        ("plugin", "1" * 65),
        (None, "0.8.0"),
        ([], "0.8.0"),
        ("plugin", None),
    ],
)
def test_malformed_client_metadata_fails_open(kind: object, version: object) -> None:
    assert client_version_headers(kind, version) == {}


def test_transport_attaches_an_explicit_valid_pair() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    settings = Settings(base_url="http://test", token="probe_pat_test")
    http = httpx.Client(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handle),
    )
    with Transport(
        settings,
        client=http,
        surface=Surface.CLI.value,
        client_headers=client_version_headers("cli", "0.8.0"),
    ) as transport:
        transport.get("/v1/me")

    assert seen[0].headers[SURFACE_HEADER] == Surface.CLI.value
    assert seen[0].headers["X-Probe-Client"] == "cli"
    assert seen[0].headers["X-Probe-Client-Version"] == "0.8.0"


def test_client_headers_do_not_leak_to_presigned_storage_urls() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    settings = Settings(base_url="http://api.test", token="probe_pat_test")
    http = httpx.Client(
        base_url=settings.base_url,
        transport=httpx.MockTransport(handle),
    )
    with Transport(
        settings,
        client=http,
        client_headers=client_version_headers("cli", "0.8.0"),
    ) as transport:
        transport.put_url("https://storage.test/presigned", b"payload")

    assert seen[0].url.host == "storage.test"
    assert SURFACE_HEADER not in seen[0].headers
    assert TOOL_HEADER not in seen[0].headers
    assert "X-Probe-Client" not in seen[0].headers
    assert "X-Probe-Client-Version" not in seen[0].headers


def test_generic_sdk_does_not_claim_to_be_the_cli(client, app) -> None:
    client.me()

    request = next(row for row in app.requests if row.url.path == "/v1/me")
    assert request.headers[SURFACE_HEADER] == Surface.SDK.value
    assert "X-Probe-Client" not in request.headers
    assert "X-Probe-Client-Version" not in request.headers


def test_custom_transport_cannot_silently_drop_client_headers() -> None:
    settings = Settings(base_url="http://test", token="probe_pat_test")
    transport = Transport(
        settings,
        client=httpx.Client(
            base_url=settings.base_url,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json={"ok": True})
            ),
        ),
    )
    try:
        with pytest.raises(ValueError, match="custom Transport"):
            Client(
                settings=settings,
                transport=transport,
                client_headers=client_version_headers("cli", "0.8.0"),
            )
    finally:
        transport.close()
