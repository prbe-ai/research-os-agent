"""Performing an update, as a function the wizard can call.

This was the body of a top-level `probe update` command. It moved here because
the wizard's Update action used to print "Run: probe update" and exit — which
is absurd. The wizard's whole job is to DO the thing; bouncing the user back to
a shell to type a command themselves is the failure it exists to remove.

There is still a hidden `probe update` bound to this, because the plugin's
SessionStart hook spawns it and plugins update on the USER's schedule, not
ours. Deleting the command outright would silently break auto-update on every
machine whose plugin has not been refreshed yet.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from probe import __version__
from probe.cli import autoupdate, updater


@dataclass
class UpdateOutcome:
    lines: list[str]
    ok: bool
    restart_needed: bool


def perform_update(
    *,
    base_url: str,
    include_plugin: bool = True,
    confirm=None,
) -> UpdateOutcome:
    """Upgrade the CLI and (optionally) the plugins, and record the attempt.

    `confirm` is an optional callable asked before touching the CLI. The wizard
    passes None when running non-interactively; passing it keeps the old
    command's "Upgrade the CLI now?" prompt available.
    """
    lines: list[str] = []

    # A failed manifest fetch must never block the upgrade -- being unable to
    # ask "what is latest" is not a reason to refuse to move.
    try:
        manifest = updater.fetch_latest(base_url)
    except Exception:  # noqa: BLE001
        manifest = {}
    plugin_target = updater.plugin_latest(manifest)
    cli_target = updater.cli_latest(manifest)

    install = updater.detect_install()
    lines.append(f"Probe Research CLI {__version__}  (installed via: {install.method})")

    res: updater.CliResult | None = None
    if install.method is updater.Method.EPHEMERAL:
        # Running via `npx probe-research` / uvx: this environment is discarded
        # on exit, so there is nothing here to upgrade. The persistent install
        # is what matters, and bootstrap owns that.
        from probe.cli.bootstrap import ensure_persistent_install

        boot = ensure_persistent_install()
        lines.append(
            "  running from a temporary environment — upgrading your installed copy instead"
        )
        if boot.message:
            lines.append(f"  {boot.message}")
    elif install.method in (
        updater.Method.EDITABLE,
        updater.Method.MANAGED,
        updater.Method.UNKNOWN,
    ):
        # Running a package-manager upgrade against a source checkout or a
        # lockfile-managed environment would trash someone's working tree.
        lines.append(
            f"  skipping auto-upgrade: "
            f"{updater.upgrade_cli(install, __version__, cli_target).message}"
        )
    elif confirm is not None and not confirm():
        lines.append("  skipped.")
    else:
        res = updater.upgrade_cli(install, __version__, cli_target)
        lines.append(f"  {res.message}")

    restart_needed = False
    if include_plugin:
        lines.append("Claude Code plugins:")
        pres = updater.update_plugin(plugin_target)
        lines.append(f"  {pres.message}")
        if pres.confirmed and pres.changed:
            restart_needed = True
        elif not pres.confirmed:
            lines.append("  update them manually, then restart Claude Code:")
            lines.extend(f"    {line}" for line in updater.manual_plugin_commands().split("\n"))

    # The ONLY way a detached auto-update can report failure: it runs with no
    # terminal attached, so without this an updater that has been broken for a
    # month is indistinguishable from one that works. `probe doctor` prints it.
    autoupdate.record_attempt(
        autoupdate.Attempt(
            at=int(time.time()),
            # No `res` means the install method was one we refuse to auto-upgrade.
            # That is a deliberate skip, not a failure.
            ok=res.ok if res is not None else True,
            detail="" if res is None or res.ok else res.message,
            from_version=__version__,
            to_version=(res.after if res is not None else None) or cli_target,
        )
    )

    return UpdateOutcome(
        lines=lines,
        ok=res.ok if res is not None else True,
        restart_needed=restart_needed,
    )
