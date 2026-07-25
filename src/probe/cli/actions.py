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
    """A top-level thing the wizard can do.

    Shown as a menu only on a RE-RUN. A fresh machine has nothing to diagnose,
    update or remove, so it goes straight to the capability picker rather than
    making someone choose "set up" from a list of five when four are no-ops.
    """

    CONFIGURE = "configure"
    DIAGNOSE = "diagnose"
    UPDATE = "update"
    MANUAL = "manual"
    REMOVE = "remove"


ACTION_COPY: dict[Action, tuple[str, str]] = {
    Action.CONFIGURE: (
        "Set up or change what's enabled",
        "Turn tracking, session capture or auto-update on and off.",
    ),
    Action.DIAGNOSE: (
        "Diagnose a problem",
        "What's installed, which credentials resolve, whether updates work.",
    ),
    Action.UPDATE: (
        "Update to the latest version",
        "Upgrades the CLI and the plugins.",
    ),
    Action.MANUAL: (
        "Show the manual steps",
        "Every command this wizard runs, printed so you can run them yourself.",
    ),
    Action.REMOVE: (
        "Remove Probe from this device",
        "Stops capture, removes the plugins, clears local credentials.",
    ),
}


def manual_steps(*, base_url: str) -> str:
    """Every command the wizard runs, printed.

    This replaces the page's "Set it up manually" section. Generating it from
    the same constants the wizard uses is the point: the page's copy had already
    drifted from the commands beside it, and a printed script cannot drift from
    the code that prints it.
    """
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
            f"claude plugin marketplace add {MARKETPLACE_REPO}",
            "claude plugin marketplace update research-os-agent",
            "",
            "# 3. Install only what you want.",
            f"claude plugin install {PLUGIN_ID}          # experiment tracking + MCP",
            f"claude plugin install {TAP_PLUGIN_ID}      # session capture",
            "",
            "# 4. Approve this device in your browser. One approval, all credentials.",
            f"probe login --base-url {base_url}",
            "",
            "# 5. Confirm.",
            "probe doctor",
        )
    )


def self_host_notes(*, base_url: str, mcp_endpoint: str) -> str:
    """Replaces the page's "Without Claude Code or self-hosting" section."""
    return "\n".join(
        (
            "# Running the MCP yourself, or without Claude Code.",
            "",
            "# A local, read-only MCP server pointed at your own API:",
            f"PROBE_MCP_TOKEN=YOUR_READ_TOKEN PROBE_BASE_URL={base_url} \\",
            f"  uvx --from {AGENT_INSTALL} probe-research-mcp",
            "",
            f"# Hosted MCP endpoint: {mcp_endpoint}",
            "",
            "# The CLI works on its own -- no Claude Code required:",
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

    if not caps.claude_available:
        notes.append(
            "`claude` is not on PATH, so plugin install/update cannot run. The CLI "
            "and login still work; install Claude Code to enable the plugins."
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
            "Auto-update is on but has never run on this device. It fires at Claude "
            "Code session start, so it will not have run yet if you have not opened "
            "a session since enabling it."
        )
    return notes
