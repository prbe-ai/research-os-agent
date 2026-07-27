"""Explicit setup-time client installation registration."""

from __future__ import annotations

from probe.cli import client_installation
from probe.cli.capabilities import Capabilities
from probe.sdk import errors
from probe.sdk.client import Client
from probe.sdk.config import Settings


def test_snapshot_reports_installation_not_runtime_health() -> None:
    installed_but_logged_out = Capabilities(
        tracking_plugin_installed=True,
        logged_in_as=None,
        auto_update_enabled=True,
    )

    assert client_installation.snapshot(installed_but_logged_out) == {
        "auto_update": "on",
        "mcp": "installed",
        "skills": "installed",
    }


def test_register_links_api_and_mcp_tokens_to_one_installation(monkeypatch) -> None:
    calls: list[tuple] = []

    class FakeClient:
        def __init__(self, **kwargs):
            self.token = kwargs["token"]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def register_client_capabilities(self, **body):
            calls.append(("register", self.token, body))
            return {"installation_id": "install-1"}

        def create_credential_attachment_grant(self, installation_id):
            calls.append(("grant", self.token, installation_id))
            return {"grant": "join-secret"}

        def attach_current_credential(self, installation_id, *, grant):
            calls.append(("attach", self.token, installation_id, grant))
            return {}

    monkeypatch.setattr(client_installation, "Client", FakeClient)
    settings = Settings(
        base_url="https://api.test",
        token="api-secret",
        mcp_token="mcp-secret",
    )

    warnings = client_installation.register(
        Capabilities(tracking_plugin_installed=True),
        settings=settings,
    )

    assert warnings == []
    assert calls == [
        (
            "register",
            "api-secret",
            {
                "auto_update": "off",
                "mcp": "installed",
                "skills": "installed",
            },
        ),
        ("grant", "api-secret", "install-1"),
        ("attach", "mcp-secret", "install-1", "join-secret"),
    ]


def test_register_is_quiet_for_an_older_backend(monkeypatch) -> None:
    class OldBackendClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def register_client_capabilities(self, **body):
            raise errors.NotFoundError("route not found", status=404)

    monkeypatch.setattr(client_installation, "Client", OldBackendClient)
    assert (
        client_installation.register(
            Capabilities(),
            settings=Settings(base_url="https://old.test", token="api-secret"),
        )
        == []
    )


def test_register_without_an_api_token_never_opens_a_client(monkeypatch) -> None:
    def fail(**kwargs):
        raise AssertionError("Client must not be constructed")

    monkeypatch.setattr(client_installation, "Client", fail)
    assert (
        client_installation.register(
            Capabilities(),
            settings=Settings(base_url="https://api.test"),
        )
        == []
    )


def test_register_without_mcp_token_stops_after_capability_snapshot(monkeypatch) -> None:
    calls = []

    class ApiOnlyClient:
        def __init__(self, **kwargs):
            calls.append(("open", kwargs["token"]))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def register_client_capabilities(self, **body):
            calls.append(("register", body))
            return {"installation_id": "install-1"}

    monkeypatch.setattr(client_installation, "Client", ApiOnlyClient)

    warnings = client_installation.register(
        Capabilities(),
        settings=Settings(base_url="https://api.test", token="api-secret"),
    )

    assert warnings == []
    assert calls == [
        ("open", "api-secret"),
        (
            "register",
            {
                "auto_update": "off",
                "mcp": "absent",
                "skills": "absent",
            },
        ),
    ]


def test_register_reports_attachment_grant_failure(monkeypatch) -> None:
    class GrantFailureClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def register_client_capabilities(self, **body):
            return {"installation_id": "install-1"}

        def create_credential_attachment_grant(self, installation_id):
            raise errors.ScopeError("write scope required", status=403)

    monkeypatch.setattr(client_installation, "Client", GrantFailureClient)

    warnings = client_installation.register(
        Capabilities(),
        settings=Settings(
            base_url="https://api.test",
            token="api-secret",
            mcp_token="mcp-secret",
        ),
    )

    assert warnings == [
        "! could not authorize the MCP credential association: write scope required"
    ]


def test_register_reports_mcp_attachment_failure(monkeypatch) -> None:
    class AttachmentFailureClient:
        def __init__(self, **kwargs):
            self.token = kwargs["token"]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return None

        def register_client_capabilities(self, **body):
            return {"installation_id": "install-1"}

        def create_credential_attachment_grant(self, installation_id):
            return {"grant": "join-secret"}

        def attach_current_credential(self, installation_id, *, grant):
            raise errors.ConflictError("already linked")

    monkeypatch.setattr(client_installation, "Client", AttachmentFailureClient)

    warnings = client_installation.register(
        Capabilities(),
        settings=Settings(
            base_url="https://api.test",
            token="api-secret",
            mcp_token="mcp-secret",
        ),
    )

    assert warnings == [
        "! could not associate the MCP credential with this installation: already linked"
    ]


def test_sdk_methods_send_only_the_allowlisted_contract() -> None:
    class RecordingTransport:
        def __init__(self):
            self.calls = []

        def put(self, path, body):
            self.calls.append(("PUT", path, body))
            return {"installation_id": "install-1"}

        def post(self, path, body=None):
            self.calls.append(("POST", path, body))
            return {"grant": "join-secret"}

        def get(self, path):
            self.calls.append(("GET", path, None))
            return {"installations": []}

    transport = RecordingTransport()
    client = Client(
        settings=Settings(base_url="https://api.test", token="secret"),
        transport=transport,
    )

    client.register_client_capabilities(
        auto_update="on",
        mcp="installed",
        skills="absent",
    )
    client.create_credential_attachment_grant("install-1")
    client.attach_current_credential("install-1", grant="join-secret")
    client.list_client_installations()

    assert transport.calls == [
        (
            "PUT",
            "/v1/client-installations/current/capabilities",
            {
                "schema_version": 1,
                "auto_update": "on",
                "mcp": "installed",
                "skills": "absent",
            },
        ),
        (
            "POST",
            "/v1/client-installations/install-1/credential-attachment-grants",
            None,
        ),
        (
            "PUT",
            "/v1/client-installations/install-1/credentials/current",
            {"grant": "join-secret"},
        ),
        ("GET", "/v1/client-installations", None),
    ]
