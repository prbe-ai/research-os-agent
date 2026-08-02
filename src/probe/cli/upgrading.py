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

import os
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
    *, base_url: str, include_plugin: bool = True, force: bool = False
) -> UpdateOutcome:
    """Upgrade the CLI and (optionally) the plugins, and record the attempt.

    There is no confirmation hook. Both callers -- the wizard's Update action
    and the plugin's SessionStart hook -- have already been told to update by
    the time they get here, and the one that has a terminal must not stop to
    ask a second time.

    ``force`` skips the run-lock check. Reserved for a human who has explicitly
    asked to upgrade now and can see what is running; nothing automatic sets it.
    """
    lines: list[str] = []

    # WAIT FOR THE PROCESS THAT SPAWNED US, if it named itself.
    #
    # Only the detached auto-update path sets this; the wizard's interactive
    # Update action has no parent to outlive and skips the wait. Replacing the
    # installed tree while the triggering command is still lazily importing from
    # it is how you get a ModuleNotFoundError out of a command that has worked
    # for a year (see autoupdate.wait_for_pid_exit).
    parent = os.environ.get(autoupdate.WAIT_FOR_PID_ENV)
    if parent:
        try:
            autoupdate.wait_for_pid_exit(int(parent))
        except (TypeError, ValueError):
            pass

    # THE RUN-LOCK CHECK IS UNCONDITIONAL, and deliberately outside the block
    # above. It used to sit inside it, which meant it only ran for spawns that
    # named a parent -- and the plugin's SessionStart hook does not: it runs
    # `probe wizard --action update --yes`, which lands here with no parent pid
    # and would have upgraded straight through a live training run. The hook is
    # the OLDEST caller of this function, so scoping the check to the new one
    # protected everything except the path that already existed.
    #
    # For the spawned path this is also a RE-check: the gate that allowed the
    # spawn ran before the wait, and a run started during it must not be
    # upgraded into by a decision made minutes earlier.
    if not force:
        try:
            from probe.cli import run_lock

            if run_lock.any_live():
                autoupdate.record_skip(autoupdate.SKIP_RUN_IN_FLIGHT)
                return UpdateOutcome(
                    lines=[
                        "a run is in flight on this machine; upgrade deferred "
                        "(`probe doctor` shows what is holding it)"
                    ],
                    ok=True,
                    restart_needed=False,
                )
        except Exception:  # noqa: BLE001 -- an unreadable lock must not strand the upgrade
            pass

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
    else:
        res = updater.upgrade_cli(install, __version__, cli_target)
        lines.append(f"  {res.message}")

    restart_needed = False
    pres: updater.PluginResult | None = None
    if include_plugin:
        lines.append("Claude Code plugins:")
        pres = updater.update_plugin(plugin_target)
        lines.append(f"  {pres.message}")
        if pres.confirmed and pres.changed:
            restart_needed = True
        elif not pres.confirmed:
            lines.append("  update them manually, then restart Claude Code:")
            lines.extend(f"    {line}" for line in updater.manual_plugin_commands().split("\n"))

    # `attempted` is what separates "the plugin update failed" from "there was no
    # Claude Code to update" -- a CLI-only user has no `claude` on PATH, and
    # recording that as a failure every session would train everyone to ignore
    # the one line that is supposed to mean something.
    plugin_ok = True if pres is None else (pres.confirmed or not pres.attempted)
    plugin_detail = "" if pres is None or pres.confirmed else pres.message
    plugin_version = pres.after if pres is not None else None

    # The ONLY way a detached auto-update can report failure: it runs with no
    # terminal attached, so without this an updater that has been broken for a
    # month is indistinguishable from one that works. `probe doctor` prints it.
    # BOTH halves are recorded -- the plugin's outcome used to live only in
    # `lines`, which a detached run sends straight to /dev/null.
    autoupdate.record_attempt(
        autoupdate.Attempt(
            at=int(time.time()),
            # No `res` means the install method was one we refuse to auto-upgrade.
            # That is a deliberate skip, not a failure.
            ok=res.ok if res is not None else True,
            detail="" if res is None or res.ok else res.message,
            from_version=__version__,
            to_version=(res.after if res is not None else None) or cli_target,
            plugin_ok=plugin_ok,
            plugin_detail=plugin_detail,
            plugin_version=plugin_version,
        )
    )

    return UpdateOutcome(
        lines=lines,
        # Both halves, so `probe update`'s exit code means "the update worked",
        # not "the CLI half worked".
        ok=(res.ok if res is not None else True) and plugin_ok,
        restart_needed=restart_needed,
    )
