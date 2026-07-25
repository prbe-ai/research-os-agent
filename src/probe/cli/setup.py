"""`probe wizard` — the setup wizard: one command, a menu, an end-to-end install.

THE MENU IS THE POINT. It is not a config nicety, it is the consent gate that
makes a single installer legitimate. Session capture ships every prompt, file
body and tool result off the machine, so folding it silently into an
"experiment tracking" install would be a data grant the user never knowingly
made. With an explicit menu we get one front door AND an informed choice;
without it we have to pick between a silent grant and the discovery problem
where nobody ever finds capture at all.

It is also a MANAGER, not just an installer. Re-running shows live state and
toggles in both directions -- turning capture off matters as much as on.

The flag path is the CONTRACT and the menu is a front end over it (see
`resolve_selection` for the truth table). CI, piped stdin and dumb terminals all
take the flag path, and none of them may hang waiting for a keypress.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass

from probe.cli import autoupdate
from probe.cli.capabilities import (
    MARKETPLACE,
    MARKETPLACE_REPO,
    TAP_PLUGIN_NAME,
    TRACKING_PLUGIN_NAME,
    Capabilities,
    Capability,
)
from probe.cli.capture import OffMode, clear_killswitch, turn_off

_CLAUDE_TIMEOUT_S = 180.0

#: What an omitted flag means on a FRESH machine (nothing configured yet).
#: Capture defaults OFF: opting someone into transcript egress by omission is
#: exactly the consent failure this feature exists to prevent.
FRESH_DEFAULTS: dict[Capability, bool] = {
    Capability.TRACKING: True,
    Capability.CAPTURE: False,
    Capability.AUTO_UPDATE: True,
}


@dataclass(frozen=True)
class Selection:
    """The resolved end state, after flags/menu/current state are reconciled."""

    tracking: bool
    capture: bool
    auto_update: bool

    def as_map(self) -> dict[Capability, bool]:
        return {
            Capability.TRACKING: self.tracking,
            Capability.CAPTURE: self.capture,
            Capability.AUTO_UPDATE: self.auto_update,
        }


def resolve_selection(
    caps: Capabilities,
    *,
    tracking: bool | None,
    capture: bool | None,
    auto_update: bool | None,
    configured: bool | None = None,
) -> Selection:
    """The flag truth table. An omitted flag means one thing, and only one.

        FRESH run  (nothing configured yet) -> omitted flag = FRESH_DEFAULTS
        RE-RUN     (something configured)   -> omitted flag = PRESERVE current

    PRESERVE is the load-bearing half. Without it, `probe wizard --yes` in CI --
    or any re-run that names one flag and not the others -- would silently
    revoke a developer's capture pairing or switch on auto-update behind their
    back. An omitted flag must never be read as "disable".
    """
    current = caps.enabled()
    if configured is None:
        configured = caps.configured
    fallback = current if configured else FRESH_DEFAULTS
    explicit = {
        Capability.TRACKING: tracking,
        Capability.CAPTURE: capture,
        Capability.AUTO_UPDATE: auto_update,
    }
    resolved = {
        capability: (value if value is not None else fallback[capability])
        for capability, value in explicit.items()
    }
    return Selection(
        tracking=resolved[Capability.TRACKING],
        capture=resolved[Capability.CAPTURE],
        auto_update=resolved[Capability.AUTO_UPDATE],
    )


MENU_COPY: dict[Capability, tuple[str, tuple[str, ...]]] = {
    Capability.TRACKING: (
        "Experiment tracking + MCP",
        (
            "Skills for tracking runs, and read-only search over your lab's history.",
            "Sends: nothing automatically. You call it.",
        ),
    ),
    Capability.CAPTURE: (
        "Session capture -> knowledgebase",
        (
            "Streams your Claude Code sessions so your team can search them.",
            "Sends: your prompts, file contents and tool output. Secrets are",
            "stripped on the server, not on your device. This device only.",
        ),
    ),
    Capability.AUTO_UPDATE: (
        "Keep it up to date automatically",
        (
            "Upgrades the CLI and plugins in the background at session start.",
            "Off means you keep a nudge and run the upgrade yourself.",
        ),
    ),
}


def interactive() -> bool:
    """Whether a real human can answer a prompt. Both ends must be a TTY: a
    piped stdin with a TTY stdout is a script, and must take the flag path."""
    return sys.stdin.isatty() and sys.stdout.isatty()


def _run_claude(args: list[str]) -> tuple[bool, str]:
    binary = shutil.which("claude")
    if not binary:
        return False, "`claude` not found on PATH"
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, no shell
            [binary, *args],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if completed.returncode != 0:
        return False, completed.stderr.strip() or completed.stdout.strip()
    return True, completed.stdout.strip()


def install_plugin(name: str) -> tuple[bool, str]:
    """Install one plugin, refreshing the marketplace cache FIRST.

    `marketplace add` on an already-added marketplace does NOT refresh it, so
    without the update a fresh wizard run happily installs a stale plugin
    version -- which is exactly how a newly published plugin appears to be
    missing. The dashboard's pairing modal ships `add` and `update` as separate
    commands for the same reason.
    """
    _run_claude(["plugin", "marketplace", "add", MARKETPLACE_REPO])
    _run_claude(["plugin", "marketplace", "update", MARKETPLACE])
    return _run_claude(["plugin", "install", f"{name}@{MARKETPLACE}"])


def uninstall_plugin(name: str) -> tuple[bool, str]:
    return _run_claude(["plugin", "uninstall", f"{name}@{MARKETPLACE}"])


def grants_for(selection: Selection) -> list[str]:
    """The grant set for ONE browser approval.

    `api` rides along with tracking because the CLI needs a credential to be
    useful at all; `mcp` is the separate read-only one so the MCP surface cannot
    write. A capture-only selection asks for capture alone -- deliberately, so
    someone who wanted only transcript capture is not handed read/write/delete
    they never asked for.
    """
    grants: list[str] = []
    if selection.tracking:
        grants.extend(["api", "mcp"])
    if selection.capture:
        grants.append("capture")
    return grants


def needs_authorization(caps: Capabilities, selection: Selection) -> list[str]:
    """The grants this run must actually obtain, skipping ones already held.

    Re-running with everything already working must NOT drag the user through
    another browser approval -- the wizard is a manager as well as an installer,
    and a no-op re-run should be a no-op.
    """
    wanted = grants_for(selection)
    if not wanted:
        return []
    have: set[str] = set()
    if caps.logged_in_as:
        have.add("api")
    if caps.capture_token_sources:
        have.add("capture")
    # `mcp` has no cheap local signal, so it rides along with `api`: they are
    # always requested together and always minted together.
    if "api" in have:
        have.add("mcp")
    return [grant for grant in wanted if grant not in have]


def authorize(
    grants: list[str],
    *,
    base_url: str,
    on_prompt=None,
    open_browser: bool = True,
) -> tuple[dict[str, dict], list[str]]:
    """Run ONE browser approval covering everything the user ticked, and persist
    every credential it mints.

    This is the step that makes the whole feature true. Computing the grant set
    and then telling the user to go run `probe login` would leave them with a
    PAT and no capture credential -- capture would still be off after a setup
    that said it turned it on.

    All three credentials go into a single `save_context` write: it is one
    locked read-modify-write, so a partial failure cannot leave the config with
    a PAT but no ingest token (which reads as "logged in, capture silently
    off"). `ingest_token` is exactly where the uploader looks.
    """
    from probe.sdk.config import resolve, save_context
    from probe.sdk.device import DeviceLoginError, credentials_by_grant, device_authorize

    if not grants:
        return {}, []

    try:
        minted = device_authorize(
            base_url,
            grants=grants,
            on_prompt=on_prompt,
            open_browser=open_browser,
        )
    except DeviceLoginError as exc:
        return {}, [f"browser approval failed: {exc}"]

    by_grant = credentials_by_grant(minted)
    messages: list[str] = []

    updates: dict[str, str | None] = {"base_url": resolve(base_url=base_url).base_url}
    if "api" in by_grant:
        updates["token"] = by_grant["api"]["token"]
    if "mcp" in by_grant:
        updates["mcp_token"] = by_grant["mcp"]["token"]
    if "capture" in by_grant:
        updates["ingest_token"] = by_grant["capture"]["token"]

    save_context(updates)

    for grant in grants:
        if grant not in by_grant:
            # Approved, but the backend minted nothing for it. Say so rather
            # than reporting a capability that will not work.
            messages.append(
                f"! the server did not return a '{grant}' credential — "
                "that capability is NOT active"
            )
    if "capture" in by_grant:
        messages.append(
            f"Session capture paired (device {by_grant['capture'].get('device_id', '?')})."
        )
    return by_grant, messages


def plan(caps: Capabilities, selection: Selection) -> list[str]:
    """A human-readable diff of what this run will change. Printed before
    anything is touched, so `--yes` in CI still leaves an audit trail."""
    steps: list[str] = []
    current = caps.enabled()
    wanted = selection.as_map()
    for capability, want in wanted.items():
        have = current[capability]
        if want == have:
            continue
        label = MENU_COPY[capability][0]
        steps.append(f"{'enable' if want else 'disable'} {label}")
    return steps


def apply_capture(caps: Capabilities, want: bool, *, mode: OffMode) -> list[str]:
    """Bring capture to `want` and report honestly.

    Turning it OFF goes through the verified postcondition in capture.py rather
    than deleting a file and hoping.
    """
    messages: list[str] = []
    if want:
        clear_killswitch()
        ok, detail = install_plugin(TAP_PLUGIN_NAME)
        if not ok:
            messages.append(f"could not install {TAP_PLUGIN_NAME}: {detail}")
        return messages
    if not caps.capture_on and not caps.capture_token_sources:
        return messages
    result = turn_off(mode)
    messages.append(result.summary())
    messages.extend(f"! {warning}" for warning in result.warnings)
    return messages


def apply_tracking(want: bool) -> list[str]:
    """Bring tracking to `want`.

    Turning it off removes the plugin but deliberately does NOT revoke the PAT
    or log the CLI out: the wizard is not a logout command, and silently
    destroying a credential the user may be scripting against would be a nasty
    surprise. `probe logout` remains the explicit way to do that.
    """
    messages: list[str] = []
    if want:
        ok, detail = install_plugin(TRACKING_PLUGIN_NAME)
        if not ok:
            messages.append(f"could not install {TRACKING_PLUGIN_NAME}: {detail}")
        return messages
    ok, detail = uninstall_plugin(TRACKING_PLUGIN_NAME)
    if not ok:
        messages.append(f"could not uninstall {TRACKING_PLUGIN_NAME}: {detail}")
    else:
        messages.append(
            "Removed the tracking plugin. Your login is untouched — "
            "run `probe logout` if you also want to revoke this device's token."
        )
    return messages


def apply_auto_update(want: bool, channel: autoupdate.Channel) -> list[str]:
    autoupdate.save(enabled=want, channel=channel)
    if not want:
        return ["Auto-update off. You'll still get a nudge when a release lands."]
    return [f"Auto-update on, following the `{channel}` channel."]


def run_menu(defaults: dict[Capability, bool]) -> Selection | None:
    """The checkbox menu. Returns None if the user quits.

    `questionary` is imported HERE, inside the call, and never at module scope.
    `cli/__init__.py` eagerly imports `cli/main.py`, so a module-level import
    would load a full terminal-UI stack on every `probe log` -- and `probe log`
    runs inside training loops. (updater.py is the deliberate opposite case: it
    is eager on purpose, because `uv tool upgrade` replaces the tree mid-command
    and a deferred import would fail afterwards.)
    """
    try:
        import questionary
    except ImportError:  # pragma: no cover - dependency is declared
        return None

    # Same problem, same fix: these entries are 3-4 lines each, so without a
    # blank line between them the list is unreadable.
    choices: list = []
    for index, (capability, (title, detail)) in enumerate(MENU_COPY.items()):
        if index:
            choices.append(questionary.Separator(" "))
        choices.append(
            questionary.Choice(
                title="\n     ".join((title, *detail)),
                value=capability,
                checked=defaults[capability],
            )
        )
    picked = questionary.checkbox(
        "What should Probe Research do on this device?",
        choices=choices,
        instruction="(space toggles, enter confirms)",
    ).ask()
    if picked is None:  # Ctrl-C / Ctrl-D
        return None
    chosen = set(picked)
    return Selection(
        tracking=Capability.TRACKING in chosen,
        capture=Capability.CAPTURE in chosen,
        auto_update=Capability.AUTO_UPDATE in chosen,
    )


def run_action_menu(caps: Capabilities):
    """Pick a top-level action. Returns None if the user quits.

    Only shown on a RE-RUN. A fresh machine has nothing to diagnose, update or
    remove, so it goes straight to the capability picker instead of making
    someone choose "set up" from a list where four options are no-ops.
    """
    from probe.cli.actions import ACTION_COPY, Action

    try:
        import questionary
    except ImportError:  # pragma: no cover - dependency is declared
        return Action.CONFIGURE

    # A blank Separator between entries. Without it the title of one option and
    # the description of the previous one sit on adjacent lines at similar
    # indents, and the whole menu reads as a paragraph rather than a list --
    # which is precisely the thing you have to scan quickly to choose.
    choices: list = []
    for index, (action, (title, detail)) in enumerate(ACTION_COPY.items()):
        if index:
            choices.append(questionary.Separator(" "))
        choices.append(questionary.Choice(title=f"{title}\n     {detail}", value=action))

    picked = questionary.select(
        "What do you want to do?",
        choices=choices,
        instruction="(arrow keys, enter to choose)",
    ).ask()
    return picked


def describe_state(caps: Capabilities) -> list[str]:
    """A one-glance summary printed above the menu on a re-run.

    The wizard already knows all of this, so showing it means the user picks an
    action against real state rather than guessing which one they need.
    """
    lines = []
    lines.append(
        f"  Experiment tracking + MCP   {'on' if caps.tracking_on else 'off'}"
        + (f"  ({caps.logged_in_as})" if caps.logged_in_as else "")
    )
    capture = "on" if caps.capture_on else "off"
    if caps.capture_killswitched:
        capture = "off (killswitch set)"
    lines.append(f"  Session capture             {capture}")
    auto = "on" if caps.auto_update_enabled else "off"
    if caps.auto_update_enabled and caps.auto_update_channel:
        auto = f"on ({caps.auto_update_channel})"
    lines.append(f"  Automatic updates           {auto}")
    if caps.last_update_attempt:
        lines.append(f"  Last update attempt         {caps.last_update_attempt}")
    return lines


def remove_everything(caps: Capabilities) -> list[str]:
    """Take this device back to nothing, and VERIFY the capture half.

    Replaces the page's "Remove the plugin" section, which only told you to
    uninstall -- and warned that uninstalling does not revoke credentials.
    Doing it here means we can actually clear them and prove capture stopped,
    rather than leaving the user to notice.
    """
    messages: list[str] = []
    result = turn_off(OffMode.UNINSTALL)
    messages.append(result.summary())
    messages.extend(f"! {warning}" for warning in result.warnings)

    ok, detail = uninstall_plugin(TRACKING_PLUGIN_NAME)
    if not ok and "not found" not in detail.lower():
        messages.append(f"! could not remove {TRACKING_PLUGIN_NAME}: {detail}")

    autoupdate.save(enabled=False, channel=autoupdate.DEFAULT_CHANNEL)
    messages.append(
        "Removed. Your account and any data already sent are untouched — "
        "revoke this device's tokens in Settings if you also want those gone."
    )
    return messages


def restart_notice(caps: Capabilities, selection: Selection) -> str | None:
    """Tell the user to restart Claude Code, when it actually matters.

    Plugin installs and the MCP wiring only take effect on restart -- Claude
    Code reads them at session start and `probe` cannot restart it. Without this
    line someone finishes the wizard, sees "done", and finds none of it working
    in the session they are sitting in. That is the last mile of the exact
    problem this whole feature exists to solve.

    Only shown when a plugin actually changed, so a re-run that only flipped
    auto-update does not send anyone off to restart for nothing.
    """
    current = caps.enabled()
    plugin_changed = (
        selection.tracking != current[Capability.TRACKING]
        or selection.capture != current[Capability.CAPTURE]
    )
    if not plugin_changed:
        return None
    return (
        "Restart Claude Code to finish. Plugins and the MCP are read at session "
        "start, so this session will not see them until you do."
    )
