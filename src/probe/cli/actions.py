"""Everything the onboarding page used to hide in collapsed sections.

The dashboard had five `<details>` blocks -- set up manually, if something is
not working, update, remove, without Claude Code -- each duplicating commands
that also lived in two other places. All of it belongs HERE, next to the state
it acts on: the wizard already knows what is installed, what is paired, and
whether the last update worked, so it can do these instead of describing them.

The page keeps one command. This module is what that command opens into.

stdlib only, and imported lazily from cli/main.py -- `probe log` runs inside
training loops and must not pay for any of this.
"""

from __future__ import annotations

from enum import StrEnum

from probe.cli.capabilities import (
    AGENT_INSTALL,
    MARKETPLACE_REPO,
    PLUGIN_ID,
    TAP_PLUGIN_ID,
    Capabilities,
)


class Action(StrEnum):
    """A top-level thing the wizard can do, in menu order.

    Install sits first because it is what most people arrive to do, and
    Uninstall sits directly under it: they are the same decision in two
    directions, so separating them across the list makes both harder to find.
    """

    CONFIGURE = "configure"
    UNINSTALL = "uninstall"
    BACKFILL = "backfill"
    UPDATE = "update"
    DIAGNOSE = "diagnose"
    EXIT = "exit"

    #: Reachable via `--action manual`, deliberately NOT in the menu. The
    #: dashboard points air-gapped users at it, but it is the rarest path and
    #: it made the menu longer for everyone else.
    MANUAL = "manual"


ACTION_COPY: dict[Action, tuple[str, str]] = {
    Action.CONFIGURE: (
        "★ Install Probe",
        "Set up experiment tracking, session capture and updates.",
    ),
    Action.UNINSTALL: (
        "Uninstall Probe",
        "Stops capture, removes the plugins, clears local credentials.",
    ),
    Action.BACKFILL: (
        "Import existing work",
        "Point an agent at a folder; it uploads and describes what it finds.",
    ),
    Action.UPDATE: (
        "Update to the latest version",
        "Upgrades the CLI and the plugins.",
    ),
    Action.DIAGNOSE: (
        "Diagnose a problem",
        "What's installed, which credentials resolve, whether updates work.",
    ),
    Action.EXIT: (
        "Exit",
        "Leave the wizard. Nothing else changes.",
    ),
}
"""Menu order IS this dict's order. MANUAL is absent on purpose."""


def manual_steps(
    *,
    base_url: str,
    agent_source: str | tuple[str, ...] | list[str] = "claude_code",
) -> str:
    """Every command the wizard runs, printed.

    This replaces the page's "Set it up manually" section. Generating it from
    the same constants the wizard uses is the point: the page's copy had already
    drifted from the commands beside it, and a printed script cannot drift from
    the code that prints it.
    """
    requested = (agent_source,) if isinstance(agent_source, str) else tuple(agent_source)
    sources = tuple(source for source in ("claude_code", "codex") if source in requested)
    if not sources:
        sources = ("claude_code",)

    marketplace_commands: list[str] = []
    install_commands: list[str] = []
    for source in sources:
        codex_source = source == "codex"
        binary = "codex" if codex_source else "claude"
        label = "Codex" if codex_source else "Claude Code"
        refresh = "upgrade" if codex_source else "update"
        install = "add" if codex_source else "install"
        if len(sources) > 1:
            marketplace_commands.append(f"# {label}")
            install_commands.append(f"# {label}")
        marketplace_commands.extend(
            (
                f"{binary} plugin marketplace add {MARKETPLACE_REPO}",
                f"{binary} plugin marketplace {refresh} research-os-agent",
            )
        )
        install_commands.extend(
            (
                f"{binary} plugin {install} {PLUGIN_ID}          # experiment tracking + MCP",
                f"{binary} plugin {install} {TAP_PLUGIN_ID}      # session capture",
            )
        )

    codex = "codex" in sources
    mcp_login = (
        (
            "",
            "# 5. Complete Codex's host-owned OAuth for the read-only MCP.",
            "codex mcp login probe-research",
        )
        if codex
        else ()
    )
    confirm_step = 6 if codex else 5
    return "\n".join(
        (
            "# Everything the Probe Research setup wizard does, as individual commands.",
            "# Run the ones you want. Needs network access and a browser to approve.",
            "",
            "# 1. Install the CLI (skip if you already have `probe`)",
            f"uv tool install --force {AGENT_INSTALL}",
            "",
            "# 2. Add the plugin marketplace and refresh it.",
            "#    `add` alone does NOT refresh an already-added marketplace, which is",
            "#    how a freshly published plugin appears to be missing.",
            *marketplace_commands,
            "",
            "# 3. Install only what you want.",
            *install_commands,
            "",
            "# 4. Approve this device in your browser. One approval, all credentials.",
            f"probe login --base-url {base_url}",
            *mcp_login,
            "",
            f"# {confirm_step}. Confirm.",
            "probe doctor",
        )
    )


def self_host_notes(*, base_url: str, mcp_endpoint: str) -> str:
    """Explain the agent-independent CLI and self-hosted MCP paths."""
    return "\n".join(
        (
            "# Running the MCP yourself, or without a coding-agent plugin.",
            "",
            "# A local, read-only MCP server pointed at your own API:",
            f"PROBE_MCP_TOKEN=YOUR_READ_TOKEN PROBE_BASE_URL={base_url} \\",
            f"  uvx --from {AGENT_INSTALL} probe-research-mcp",
            "",
            f"# Hosted MCP endpoint: {mcp_endpoint}",
            "",
            "# The CLI works on its own -- no coding agent required:",
            f"uv tool install --force {AGENT_INSTALL}",
            f"probe login --base-url {base_url}",
        )
    )


def troubleshooting(caps: Capabilities) -> list[str]:
    """The page's "If something is not working" section, but state-aware.

    A static list makes the reader work out which item applies to them. Here we
    already know, so only the relevant lines are printed -- and the ones that
    depend on live state say what that state actually is.
    """
    notes: list[str] = []

    agent_source = caps.agent_source
    agent_binary = "codex" if agent_source == "codex" else "claude"
    agent_name = "Codex" if agent_source == "codex" else "Claude Code"
    agent_available = caps.codex_available if agent_source == "codex" else caps.claude_available
    if not agent_available:
        notes.append(
            f"`{agent_binary}` is not on PATH, so plugin install/update cannot run. The CLI "
            f"and login still work; install {agent_name} to enable the plugins."
        )
    if caps.cli_version is None:
        notes.append(
            "`probe` is not resolving. Re-run the install to relink the binaries and "
            "make sure ~/.local/bin is on your PATH."
        )
    if caps.tracking_plugin_installed and not caps.logged_in_as:
        notes.append(
            "The tracking plugin is installed but this device is not logged in, so "
            "the MCP has no credential. Run `probe wizard` and pick experiment tracking."
        )
    if agent_source == "codex":
        notes.append(
            "If MCP tools are missing after a restart, run `codex mcp list --json`. "
            "If `probe-research` is not authenticated, run "
            "`codex mcp login probe-research`."
        )
    else:
        # The footgun that cannot heal itself: an exported token beats the stored one
        # forever, and nothing in the product can clear a variable in the user's shell.
        notes.append(
            "If MCP tools are missing after a restart: a stale PROBE_MCP_TOKEN exported "
            "in your shell profile SHADOWS the stored token and can never heal. Delete "
            "that line, then run `probe mcp status` to see where the token came from."
        )
    notes.append(
        "Do not register the MCP by hand while the plugin is installed -- the plugin "
        "already wires the `probe-research` server, and a second server with the same "
        "name breaks the connection."
    )
    if caps.auto_update_enabled and caps.last_update_attempt is None:
        notes.append(
            f"Auto-update is on but has never run on this device. It fires at {agent_name} "
            "session start, so it will not have run yet if you have not opened "
            "a session since enabling it."
        )
    return notes
