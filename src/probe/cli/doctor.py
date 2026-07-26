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

from probe.cli import autoupdate
from probe.cli.capabilities import (
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
    lines.append(_row("Claude Code CLI", _OK if caps.claude_available else _MISSING))
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
    lines.append(_row("Status", _OK if caps.tracking_on else _OFF))
    lines.append("")

    lines.append("Session capture")
    lines.append(
        _row("Plugin", TAP_PLUGIN_NAME if caps.capture_plugin_installed else _MISSING)
    )
    lines.append(_row("Status", "capturing" if caps.capture_on else _OFF))
    if caps.capture_device_id:
        lines.append(_row("Paired device", caps.capture_device_id))
    if caps.capture_killswitched:
        lines.append(_row("Killswitch", "ON — capture is disabled locally"))
    # Every source is listed, not just the winning one. A user who thinks capture
    # is off deserves to see the credential that is keeping it alive.
    for source in caps.capture_token_sources:
        lines.append(_row("Credential from", _SOURCE_LABEL[source]))
    if not caps.capture_token_sources:
        lines.append(_row("Credential", "none — this device is not paired"))
    lines.append("")

    lines.append("Auto-update")
    lines.append(_row("Enabled", "yes" if caps.auto_update_enabled else "no"))
    lines.append(_row("Channel", caps.auto_update_channel or str(autoupdate.DEFAULT_CHANNEL)))
    lines.append(
        _row("Last attempt", caps.last_update_attempt or "never run on this device")
    )

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
        TAP_PLUGIN_NAME as TAP,
    )
    from probe.cli.capabilities import (
        TRACKING_PLUGIN_NAME as TRACK,
    )
    from probe.cli.capabilities import (
        capture_device_id,
        capture_token_sources,
        installed_plugins,
        tap_plugin_dir,
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

    plugins = installed_plugins()
    sources = capture_token_sources()
    settings_autoupdate = autoupdate.load()

    if TokenSource.ENVIRONMENT in sources:
        warnings.append(
            f"{ENV_INGEST_TOKEN} is set in this shell. It overrides local pairing "
            "state, so capture can appear off in the menu yet still be on."
        )

    return Capabilities(
        cli_version=__version__,
        install_method=install_method,
        claude_available=shutil.which("claude") is not None,
        logged_in_as=logged_in_as or None,
        base_url=base_url,
        tracking_plugin_installed=TRACK in plugins,
        capture_plugin_installed=TAP in plugins,
        capture_token_sources=sources,
        capture_killswitched=(tap_plugin_dir() / ".disabled").exists(),
        capture_device_id=capture_device_id(),
        auto_update_enabled=settings_autoupdate.enabled,
        auto_update_channel=str(settings_autoupdate.channel),
        last_update_attempt=(
            settings_autoupdate.last_attempt.describe()
            if settings_autoupdate.last_attempt
            else None
        ),
        warnings=warnings,
    )
