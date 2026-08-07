"""`probe doctor` — read-only diagnostic over the same state the wizard renders.

Two renderings, one state struct (see capabilities.py). The menu shows it as
toggles; this prints it.

The line that earns this command its place is LAST UPDATE ATTEMPT. Auto-update
runs detached, so it cannot report failure through the SessionStart hook that
spawned it. Without a recorded attempt, an auto-updater that has been silently
failing for a month is indistinguishable from one that works. `claude doctor`
solves it the same way.

Read-only and fail-soft throughout: this is the command someone runs when things
are already broken, so it must never be the thing that breaks.
"""

from __future__ import annotations

import shutil

from probe.cli import agent_rules, autoupdate, plugin_cli
from probe.cli.capabilities import (
    ENV_CODEX_INGEST_TOKEN,
    ENV_INGEST_TOKEN,
    TAP_PLUGIN_NAME,
    TRACKING_PLUGIN_NAME,
    Capabilities,
    TokenSource,
)

_OK = "ok"
_OFF = "off"
_MISSING = "not installed"

_SOURCE_LABEL = {
    TokenSource.PAIRED_FILE: "paired device token",
    TokenSource.ENVIRONMENT: f"{ENV_INGEST_TOKEN} environment variable",
    TokenSource.PROBE_CONFIG: "probe CLI config (ingest_token)",
}


def _row(label: str, value: str) -> str:
    return f"  {label:<24} {value}"


def render(caps: Capabilities) -> str:
    """The full report. Pure, so it can be asserted in tests without a machine."""
    lines: list[str] = ["Probe Research doctor", ""]

    lines.append("Install")
    lines.append(_row("CLI version", caps.cli_version or "unknown"))
    lines.append(_row("Install method", caps.install_method or "unknown"))
    agent_label = "Codex CLI" if caps.agent_source == "codex" else "Claude Code CLI"
    agent_available = (
        caps.codex_available if caps.agent_source == "codex" else caps.claude_available
    )
    lines.append(_row(agent_label, _OK if agent_available else _MISSING))
    lines.append("")

    lines.append("Account")
    lines.append(_row("Logged in as", caps.logged_in_as or "not logged in"))
    lines.append(_row("Endpoint", caps.base_url or "unknown"))
    lines.append("")

    lines.append("CLI + MCP")
    lines.append(
        _row(
            "Plugin",
            TRACKING_PLUGIN_NAME if caps.tracking_plugin_installed else _MISSING,
        )
    )
    if caps.agent_source == "codex":
        mcp_status = (
            "logged in"
            if caps.mcp_authenticated is True
            else "not logged in"
            if caps.mcp_authenticated is False
            else "unknown"
        )
        lines.append(_row("MCP OAuth", mcp_status))
    lines.append(_row("Status", _OK if caps.tracking_on else _OFF))
    lines.append("")

    lines.append("Session capture")
    tap_name = TAP_PLUGIN_NAME
    lines.append(_row("Plugin", tap_name if caps.capture_plugin_installed else _MISSING))
    lines.append(_row("Status", "capturing" if caps.capture_on else _OFF))
    if caps.capture_device_id:
        lines.append(_row("Paired device", caps.capture_device_id))
    if caps.capture_killswitched:
        lines.append(_row("Killswitch", "ON — capture is disabled locally"))
    # Every source is listed, not just the winning one. A user who thinks capture
    # is off deserves to see the credential that is keeping it alive.
    for source in caps.capture_token_sources:
        label = _SOURCE_LABEL[source]
        if source is TokenSource.ENVIRONMENT and caps.agent_source == "codex":
            label = f"{ENV_CODEX_INGEST_TOKEN} environment variable"
        lines.append(_row("Credential from", label))
    if caps.capture_credential_valid is False:
        lines.append(_row("Credential check", "rejected — re-run `probe wizard`"))
    elif caps.capture_token_sources and caps.capture_credential_valid is None:
        lines.append(_row("Credential check", "unknown (offline or endpoint unavailable)"))
    if not caps.capture_token_sources:
        lines.append(_row("Credential", "none — this device is not paired"))
    lines.append("")

    lines.append("Agent rules")
    lines.append(
        _row("Global CLAUDE.md", "installed" if caps.agent_rules_installed else "not added")
    )
    # Reported separately from installed/absent. A block from an older release
    # still LOADS, so it reads as working while teaching superseded wording --
    # and this file is the one copy no release can reach, so nothing else would
    # ever surface it. `probe wizard` rewrites it in place.
    if caps.agent_rules_stale:
        lines.append(_row("", "outdated wording -- re-run `probe wizard` to refresh"))
    lines.append("")

    lines.append("Auto-update")
    lines.append(_row("Enabled", "yes" if caps.auto_update_enabled else "no"))
    lines.append(_row("Last attempt", caps.last_update_attempt or "never run on this device"))
    # A SEPARATE line, never folded into the one above. Once the run lock exists,
    # a box that has been training all week is deliberately not updating -- and
    # with only a last-attempt timestamp that is byte-identical to an auto-updater
    # that died. This is the line that tells them apart.
    if caps.last_update_skip:
        lines.append(_row("Deferred", caps.last_update_skip))
    if caps.live_runs:
        shown = ", ".join(caps.live_runs[:3])
        if len(caps.live_runs) > 3:
            shown += f", +{len(caps.live_runs) - 3} more"
        lines.append(_row("Runs in flight", shown))

    lines.append("")
    lines.append("Outbox")
    if caps.outbox_status is None:
        lines.append(_row("Queue", "never used"))
    else:
        status = caps.outbox_status
        lines.append(_row("Pending", str(status.get("pending") or 0)))
        lines.append(_row("Dead-lettered", str(status.get("failed") or 0)))
        if status.get("auth_blocked_since"):
            lines.append(_row("Auth-blocked since", str(status["auth_blocked_since"])))
        if status.get("paused"):
            lines.append(_row("Paused", "yes (`probe outbox resume`)"))
        if status.get("last_error"):
            lines.append(_row("Last error", str(status["last_error"])))

    if caps.warnings:
        lines.append("")
        lines.append("Warnings")
        for warning in caps.warnings:
            lines.append(f"  ! {warning}")

    return "\n".join(lines)


def collect() -> Capabilities:
    """Snapshot this device. Every probe is individually fail-soft."""
    from probe import __version__
    from probe.cli import updater
    from probe.cli.capabilities import (
        TRACKING_PLUGIN_NAME as TRACK,
    )
    from probe.cli.capabilities import (
        capture_device_id,
        capture_plugin_name,
        capture_token_sources,
        verify_capture_credential,
        agent_source,
        installed_plugins,
        tap_plugin_dir,
        CODEX_MCP_NAME,
        LEGACY_CODEX_TAP_PLUGIN_ID,
    )

    warnings: list[str] = []

    try:
        install_method = updater.detect_install().method
    except Exception:  # noqa: BLE001 - a diagnostic must never crash
        install_method = None

    logged_in_as: str | None = None
    base_url: str | None = None
    try:
        from probe.sdk.config import resolve

        settings = resolve()
        base_url = settings.base_url
        if settings.token:
            from probe.sdk.client import Client

            with Client(settings=settings) as client:
                logged_in_as = str(client.me().get("email") or "")
    except Exception as exc:  # noqa: BLE001
        if base_url:
            warnings.append(f"could not verify login against {base_url}: {exc}")

    selected_agent = agent_source()
    selected_tap = capture_plugin_name(selected_agent)
    plugins = installed_plugins(source=selected_agent)
    sources = capture_token_sources(selected_agent)
    credential_valid = verify_capture_credential(selected_agent) if sources else None
    settings_autoupdate = autoupdate.load()

    if credential_valid is False:
        warnings.append(
            "the capture credential was rejected by the ingest service; "
            "re-run `probe wizard` to pair this device again"
        )
    elif sources and credential_valid is None:
        warnings.append("could not verify the capture credential (offline or endpoint unavailable)")
    if LEGACY_CODEX_TAP_PLUGIN_ID in plugins:
        warnings.append(
            f"legacy {LEGACY_CODEX_TAP_PLUGIN_ID} is still installed and can race the unified tap; "
            "re-run `probe wizard` to remove it"
        )

    mcp_authenticated: bool | None = None
    if selected_agent == "codex" and TRACK in plugins:
        status = plugin_cli.codex_mcp_auth_status(CODEX_MCP_NAME)
        if status is not None:
            mcp_authenticated = status in {"o_auth", "bearer_token"}
        if mcp_authenticated is False:
            warnings.append(
                "the probe-research MCP is installed but not logged in; "
                "re-run `probe wizard` to complete Codex OAuth"
            )

    if TokenSource.ENVIRONMENT in sources:
        token_env = ENV_CODEX_INGEST_TOKEN if selected_agent == "codex" else ENV_INGEST_TOKEN
        warnings.append(
            f"{token_env} is set in this shell. It overrides local pairing "
            "state, so capture can appear off in the menu yet still be on."
        )

    return Capabilities(
        cli_version=__version__,
        install_method=install_method,
        claude_available=shutil.which("claude") is not None,
        codex_available=shutil.which("codex") is not None,
        agent_source=selected_agent,
        logged_in_as=logged_in_as or None,
        base_url=base_url,
        tracking_plugin_installed=TRACK in plugins,
        capture_plugin_installed=selected_tap in plugins,
        legacy_capture_plugin_installed=LEGACY_CODEX_TAP_PLUGIN_ID in plugins,
        plugins_verified=plugins.verified,
        capture_token_sources=sources,
        capture_credential_valid=credential_valid,
        capture_killswitched=(tap_plugin_dir(selected_agent) / ".disabled").exists(),
        capture_device_id=capture_device_id(selected_agent),
        mcp_authenticated=mcp_authenticated,
        agent_rules_installed=agent_rules.is_installed(),
        agent_rules_stale=(agent_rules.is_installed() and not agent_rules.is_current()),
        auto_update_enabled=settings_autoupdate.enabled,
        last_update_attempt=(
            settings_autoupdate.last_attempt.describe()
            if settings_autoupdate.last_attempt
            else None
        ),
        last_update_skip=(
            settings_autoupdate.last_skip.describe() if settings_autoupdate.last_skip else None
        ),
        live_runs=_live_runs(),
        outbox_status=_outbox_status(),
        warnings=warnings,
    )


def _live_runs() -> list[str]:
    try:
        from probe.cli import run_lock

        return run_lock.live_runs()
    except Exception:  # noqa: BLE001 - a diagnostic must never crash
        return []


def _outbox_status() -> dict | None:
    try:
        from probe.sdk.journal import Journal

        return Journal.read_status()
    except Exception:  # noqa: BLE001 - a diagnostic must never crash
        return None
