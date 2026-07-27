"""Publish the setup wizard's allowlisted local capability snapshot.

This is an explicit registration after setup changes, not recurring telemetry.
Normal API and MCP requests continue to send only their bearer token; the
backend resolves that credential to the durable installation row.
"""

from __future__ import annotations

from probe.cli.capabilities import Capabilities
from probe.sdk import errors
from probe.sdk.client import Client
from probe.sdk.config import Settings, resolve
from probe.sdk.surface import Surface


def snapshot(caps: Capabilities) -> dict[str, str]:
    """Return schema v1 using installed state, not runtime health."""
    plugin = "installed" if caps.tracking_plugin_installed else "absent"
    return {
        "auto_update": "on" if caps.auto_update_enabled else "off",
        "mcp": plugin,
        "skills": plugin,
    }


def register(
    caps: Capabilities,
    *,
    settings: Settings | None = None,
) -> list[str]:
    """Register actual post-action state and adopt a legacy MCP PAT if present.

    Returns human-facing warnings. A 404 is ignored so a newly released wizard
    stays compatible with an older or self-hosted backend during rollout.
    """
    settings = settings or resolve()
    if not settings.token:
        return []

    state = snapshot(caps)
    try:
        with Client(
            base_url=settings.base_url,
            token=settings.token,
            fail_open=False,
            surface=Surface.CLI.value,
        ) as client:
            registration = client.register_client_capabilities(**state)
    except errors.NotFoundError:
        return []
    except errors.RosError as exc:
        return [f"! could not register this installation with the server: {exc}"]

    installation_id = registration.get("installation_id")
    if not settings.mcp_token or not installation_id:
        return []

    try:
        with Client(
            base_url=settings.base_url,
            token=settings.token,
            fail_open=False,
            surface=Surface.CLI.value,
        ) as client:
            attachment = client.create_credential_attachment_grant(
                str(installation_id)
            )
    except errors.NotFoundError:
        return []
    except errors.RosError as exc:
        return [f"! could not authorize the MCP credential association: {exc}"]
    grant = attachment.get("grant")
    if not isinstance(grant, str) or not grant:
        return ["! the server returned an invalid MCP credential attachment grant"]

    try:
        with Client(
            base_url=settings.base_url,
            token=settings.mcp_token,
            fail_open=False,
            surface=Surface.CLI.value,
        ) as client:
            client.attach_current_credential(str(installation_id), grant=grant)
    except errors.NotFoundError:
        return []
    except errors.RosError as exc:
        return [f"! could not associate the MCP credential with this installation: {exc}"]
    return []
