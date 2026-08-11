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

import json
import os
import sys
from dataclasses import dataclass

from probe.cli import agent_rules, autoupdate, claude_cli, codex_config, plugin_cli
from probe.cli.capabilities import (
    CODEX_MCP_NAME,
    CODEX_TAP_PLUGIN_NAME,
    LEGACY_CODEX_TAP_PLUGIN_ID,
    MARKETPLACE,
    MARKETPLACE_REPO,
    TAP_PLUGIN_NAME,
    TRACKING_PLUGIN_NAME,
    Capabilities,
    Capability,
    agent_source,
    tap_plugin_dir,
)
from probe.cli.capture import OffMode, clear_killswitch, turn_off

#: How long the whole apply phase may spend before it stops STARTING new work.
#: Not a kill switch: a `claude plugin install` cut off mid-write is how a
#: plugin cache gets corrupted, so an in-flight step always runs to its own
#: timeout. This only refuses to begin the next one.
PHASE_BUDGET_S = 300.0

#: What an omitted flag means on a FRESH machine (nothing configured yet).
#:
#: EVERYTHING ON, capture included. This reverses the original default and the
#: reversal is deliberate, so the reasoning it replaces is recorded here rather
#: than deleted: capture used to default OFF because opting someone into
#: transcript egress by omission was judged the consent failure the menu exists
#: to prevent.
#:
#: What now carries that weight instead is the menu itself. Capture is a TICKED,
#: LABELLED row that says what it sends and where -- "Sends this device's Claude
#: Code sessions so your team can search them" -- sitting under the cursor
#: before the Next row is reachable, and one keystroke unticks it. The grant is
#: on screen and refusable; it is no longer inferred from silence.
#:
#: The `--yes` path has NO screen, so it is the one that changed most: a
#: scripted `probe wizard --yes` on a fresh box now enables capture where it
#: previously would not. Anyone automating an install who does not want that
#: passes `--no-capture`, and a RE-RUN still PRESERVES rather than defaults
#: (see resolve_selection), so this can never switch capture on behind someone
#: who already turned it off.
FRESH_DEFAULTS: dict[Capability, bool] = {
    Capability.TRACKING: True,
    Capability.CAPTURE: True,
    Capability.AUTO_UPDATE: True,
    Capability.AGENT_RULES: True,
}

AGENT_LABELS = {
    "claude_code": "Claude Code",
    "codex": "Codex",
}


def agent_label(sources: tuple[str, ...] | list[str] | str) -> str:
    """User-facing name for the exact agents selected in this wizard run."""
    normalized = (sources,) if isinstance(sources, str) else tuple(sources)
    labels = [AGENT_LABELS[source] for source in AGENT_LABELS if source in normalized]
    if not labels:
        return "coding agent"
    if len(labels) == 1:
        return labels[0]
    return " and ".join(labels)


def instruction_files(sources: tuple[str, ...] | list[str] | str) -> str:
    """Name the real global instruction files for the selected agents."""
    normalized = (sources,) if isinstance(sources, str) else tuple(sources)
    names = []
    if "claude_code" in normalized:
        names.append("CLAUDE.md")
    if "codex" in normalized:
        names.append("AGENTS.md")
    return " + ".join(names) or "agent instructions"


def run_agent_menu(defaults: tuple[str, ...]):
    """Choose every coding agent this one onboarding run should configure."""
    import questionary

    from probe.cli import tui

    choices = [
        questionary.Choice(
            title=f"{label}\n{tui.body_indent()}  Install plugins and pair source-bound capture.",
            value=source,
            checked=source in defaults,
        )
        for source, label in AGENT_LABELS.items()
    ]
    message = tui.framed(
        "One setup can configure either or both.",
        [],
        "Which coding agents should Probe Research connect?",
    )
    picked = tui.ask(
        questionary.checkbox(
            message,
            choices=choices,
            instruction="(space to toggle, enter to continue)",
            style=tui.style(),
            qmark=tui.qmark(),
            pointer=tui.pointer(),
            validate=lambda answer: bool(answer) or "Choose at least one coding agent.",
        ),
        height=tui.content_height(message, choices),
    )
    if picked is None or picked is tui.BACK:
        return picked
    return tuple(source for source in AGENT_LABELS if source in picked)


@dataclass(frozen=True)
class Selection:
    """The resolved end state, after flags/menu/current state are reconciled."""

    tracking: bool
    capture: bool
    auto_update: bool
    agent_rules: bool

    def as_map(self) -> dict[Capability, bool]:
        return {
            Capability.TRACKING: self.tracking,
            Capability.CAPTURE: self.capture,
            Capability.AUTO_UPDATE: self.auto_update,
            Capability.AGENT_RULES: self.agent_rules,
        }


def resolve_selection(
    caps: Capabilities,
    *,
    tracking: bool | None,
    capture: bool | None,
    auto_update: bool | None,
    agent_rules: bool | None = None,
    configured: bool | None = None,
    current_override: dict[Capability, bool] | None = None,
) -> Selection:
    """The flag truth table. An omitted flag means one thing, and only one.

        FRESH run  (nothing configured yet) -> omitted flag = FRESH_DEFAULTS
        RE-RUN     (something configured)   -> omitted flag = PRESERVE current

    PRESERVE is the load-bearing half. Without it, `probe wizard --yes` in CI --
    or any re-run that names one flag and not the others -- would silently
    revoke a developer's capture pairing or switch on auto-update behind their
    back. An omitted flag must never be read as "disable".

    `current_override` supplies "current" when one snapshot cannot express it.
    A run configuring BOTH agents has two snapshots, and what PRESERVE has to
    keep is what the DEVICE has -- the union -- not what the agents agree on.
    Deriving it from the intersection instead reads a machine with Claude Code
    set up and Codex fresh as having nothing on, and PRESERVE then faithfully
    preserves nothing: every box unticked, and the apply path turns Claude
    Code's capture off on the way to installing Codex.
    """
    current = current_override if current_override is not None else caps.enabled()
    if configured is None:
        configured = caps.configured
    fallback = current if configured else FRESH_DEFAULTS
    explicit = {
        Capability.TRACKING: tracking,
        Capability.CAPTURE: capture,
        Capability.AUTO_UPDATE: auto_update,
        Capability.AGENT_RULES: agent_rules,
    }
    resolved = {
        capability: (value if value is not None else fallback[capability])
        for capability, value in explicit.items()
    }
    return Selection(
        tracking=resolved[Capability.TRACKING],
        capture=resolved[Capability.CAPTURE],
        auto_update=resolved[Capability.AUTO_UPDATE],
        agent_rules=resolved[Capability.AGENT_RULES],
    )


def menu_copy(
    sources: tuple[str, ...] | list[str] | str,
) -> dict[Capability, tuple[str, tuple[str, ...]]]:
    """Capability copy scoped to the agents this run will actually configure."""
    agents = agent_label(sources)
    rules = instruction_files(sources)
    return {
        Capability.TRACKING: (
            "CLI + MCP  (recommended)",
            (f"Track runs in {agents}; search lab history read-only.",),
        ),
        Capability.CAPTURE: (
            "Session capture -> knowledgebase",
            (f"Sends this device's {agents} sessions to your team's search.",),
        ),
        Capability.AGENT_RULES: (
            f"Rules in your global {rules}  (recommended)",
            (f"Prompts {agents} to search and track research in Probe.",),
        ),
    }


# Backwards-compatible default for non-interactive consumers that only need
# capability titles. Interactive callers always request copy for their targets.
MENU_COPY = menu_copy(("claude_code", "codex"))
# The picker is a picker. The full disclosure of what leaves the machine --
# prompts, file contents, tool output, server-side secret stripping -- lives on
# the BROWSER APPROVAL screen, which is where the grant is actually made and
# where research-os asserts the wording verbatim. Repeating three lines of it
# here made the shortest menu in the product the densest thing to read.


#: Asked as its OWN step, after the capabilities. It is not a capability -- it
#: is a policy about the ones you just chose -- and mixing it into the same
#: checkbox list made a three-item menu where two items were about what Probe
#: does and one was about how it maintains itself.
def auto_update_copy(sources: tuple[str, ...] | list[str] | str) -> tuple[str, str]:
    agents = agent_label(sources)
    timing = (
        "when either Claude Code or Codex starts a session"
        if agents == "Claude Code and Codex"
        else f"when {agents} starts a session"
    )
    return (
        "Keep it up to date automatically?  (recommended)",
        f"Upgrades the CLI and plugins in the background {timing}.",
    )


AUTO_UPDATE_COPY = auto_update_copy(("claude_code", "codex"))

#: How `plan()` names each capability in "This run will: - enable X".
#:
#: MUST stay total over `Capability`. It is a SEPARATE map from MENU_COPY
#: because the plan covers every capability while MENU_COPY only holds the
#: checkbox rows -- reading the label out of MENU_COPY crashed the wizard on
#: every fresh install, since auto-update is asked outside that list and always
#: changes state on a machine that has never been set up. The phrasings differ
#: too: a menu row is a question ("Keep it up to date automatically?"), a plan
#: step is a noun ("enable automatic updates").
PLAN_LABELS: dict[Capability, str] = {
    Capability.TRACKING: MENU_COPY[Capability.TRACKING][0],
    Capability.CAPTURE: MENU_COPY[Capability.CAPTURE][0],
    Capability.AUTO_UPDATE: "automatic updates",
    # Its own noun, not the menu title: the concrete filename is selected later
    # because Claude Code and Codex load different global instruction files.
    Capability.AGENT_RULES: "the global agent guidance rules",
}

#: What the step reads when the PLUGIN is already installed and only the
#: credential is missing. Distinct from PLAN_LABELS because "enable CLI + MCP"
#: on such a machine promises an install that will not happen: the run's whole
#: job there is the browser approval.
SIGN_IN_LABELS: dict[Capability, str] = {
    Capability.TRACKING: "sign in (the CLI + MCP plugin is already installed)",
    Capability.CAPTURE: "sign in to pair Session capture (the plugin is already installed)",
}


def interactive() -> bool:
    """Whether a real human can answer a prompt. Both ends must be a TTY: a
    piped stdin with a TTY stdout is a script, and must take the flag path."""
    return sys.stdin.isatty() and sys.stdout.isatty()


#: How an apply_* message announces that something went wrong. The wizard's
#: progress screen has to tell a failed step from a successful one, and these
#: helpers report failure in PROSE rather than by raising -- so the markers live
#: here, beside the code that emits them. A copy of this tuple in main.py would
#: drift the first time someone reworded a message, and the tick would quietly
#: go green on a step that failed.
_FAILURE_MARKERS = ("could not", "!")


def reports_failure(message: str) -> bool:
    """Whether an apply_* message is reporting a failure or a warning."""
    return message.startswith(_FAILURE_MARKERS)


def refresh_marketplace(*, source: str | None = None) -> claude_cli.Result:
    """Bring the local marketplace copy up to date. ONCE per wizard run.

    `marketplace add` on an already-added marketplace does NOT refresh it, so
    without the update a fresh wizard run happily installs a stale plugin
    version -- which is exactly how a newly published plugin appears to be
    missing. The dashboard's pairing modal ships `add` and `update` as separate
    commands for the same reason.

    Hoisted OUT of install_plugin, which used to do it per plugin: a run that
    installs both plugins refreshed the same marketplace twice, and threw both
    results away. Discarding them is why a failed refresh surfaced as Claude's
    downstream "not found in marketplace" -- an error whose suggested fix is
    the very command the wizard had just silently failed at.

    `add` failing is NOT fatal and is not reported: the common case is "already
    on disk", which exits non-zero on some versions. `update` is the one whose
    success decides whether the catalog we install from is current.
    """
    selected = source or agent_source()
    plugin_cli.add_marketplace(selected, MARKETPLACE_REPO)
    return plugin_cli.refresh_marketplace(selected, MARKETPLACE)


def install_plugin(name: str, *, source: str | None = None, on_retry=None) -> claude_cli.Result:
    """Install one plugin. Retries ONCE, after a refresh, on ANY failure.

    Deliberately NOT gated on matching Claude's error text. A retry that fires
    only when the message contains "not found in marketplace" is a guard that
    certifies its own rot: Anthropic rewords the string, the retry silently
    stops firing, and every test stays green. One plain rule instead -- it
    failed, so refresh and try once more -- costs one extra attempt on a
    genuinely broken machine and cannot fall out of sync with anyone's prose.

    `on_retry` is the caller's retry BUDGET, not a notification: it returns
    False once the run has already spent its single retry, so two plugins
    failing cannot cost two refreshes and two reinstalls.
    """
    selected = source or agent_source()
    result = plugin_cli.install(selected, f"{name}@{MARKETPLACE}")
    if result.ok or on_retry is None or not on_retry():
        return result
    refresh_marketplace(source=selected)
    return plugin_cli.install(selected, f"{name}@{MARKETPLACE}")


def uninstall_plugin(name: str, *, source: str | None = None) -> claude_cli.Result:
    selected = source or agent_source()
    return plugin_cli.uninstall(selected, f"{name}@{MARKETPLACE}")


#: What each capability's plugin cannot work without.
#:
#: `api` rides along with tracking because the CLI needs a credential to be useful
#: at all; `mcp` is the separate read-only one so the MCP surface cannot write. A
#: capture-only selection asks for capture alone -- deliberately, so someone who
#: wanted only transcript capture is not handed read/write/delete they never asked
#: for.
#:
#: ONE table, read by both `grants_for` (what to request) and
#: `blocked_by_missing_grants` (what may install once the answer is back). They were
#: two hardcoded lists for about a day, which is exactly long enough for a third
#: grant to be added to one and not the other -- and the failure mode of that skew
#: is an install gated on a credential nobody asked for, or worse, not gated at all.
CAPABILITY_GRANTS: dict[Capability, tuple[str, ...]] = {
    Capability.TRACKING: ("api", "mcp"),
    Capability.CAPTURE: ("capture",),
}


def grants_for(selection: Selection) -> list[str]:
    """The grant set for ONE browser approval."""
    grants: list[str] = []
    for capability, wanted in CAPABILITY_GRANTS.items():
        if selection.as_map()[capability]:
            grants.extend(wanted)
    return grants


def blocked_by_missing_grants(
    capability: Capability, *, needed: list[str], granted: dict
) -> list[str]:
    """The grants this capability requires that the run TRIED and FAILED to get.

    Only grants in `needed` count. A re-run that already holds `api`/`mcp` never
    asks for them again, so they are absent from `granted` for the good reason --
    reading that as failure would refuse to install on every healthy machine.
    """
    return [
        grant
        for grant in CAPABILITY_GRANTS.get(capability, ())
        if grant in needed and grant not in granted
    ]


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
    if caps.capture_token_sources and caps.capture_credential_valid is not False:
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
    capture_sources: list[str] | None = None,
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
    from probe.sdk.device import (
        DeviceLoginError,
        capture_credentials_by_source,
        credentials_by_grant,
        device_authorize,
    )

    if not grants:
        return {}, []

    try:
        source = agent_source()
        requested_sources = list(dict.fromkeys(capture_sources or [source]))
        source_args = {}
        if "capture" in grants:
            source_args = (
                {"capture_source": requested_sources[0]}
                if len(requested_sources) == 1
                else {"capture_sources": requested_sources}
            )
        minted = device_authorize(
            base_url,
            grants=grants,
            **source_args,
            on_prompt=on_prompt,
            open_browser=open_browser,
        )
    except DeviceLoginError as exc:
        return {}, [f"browser approval failed: {exc}"]

    by_grant = credentials_by_grant(minted)
    captures = capture_credentials_by_source(minted)
    raw_capture_entries = [
        entry for entry in (minted.get("grants") or []) if entry.get("grant") == "capture"
    ]
    if (
        len(requested_sources) == 1
        and len(raw_capture_entries) == 1
        and not raw_capture_entries[0].get("capture_source")
    ):
        # A single-source backend predating the response discriminator is still
        # unambiguous because the request carried exactly one source.
        captures = {requested_sources[0]: raw_capture_entries[0]}
    if captures:
        by_grant["capture"] = next(iter(captures.values()))
    messages: list[str] = []

    updates: dict[str, str | None] = {"base_url": resolve(base_url=base_url).base_url}
    if "api" in by_grant:
        updates["token"] = by_grant["api"]["token"]
    if "mcp" in by_grant:
        updates["mcp_token"] = by_grant["mcp"]["token"]
    if "claude_code" in captures:
        updates["ingest_token"] = captures["claude_code"]["token"]

    save_context(updates)

    if "codex" in captures:
        state_dir = tap_plugin_dir("codex")
        state_dir.mkdir(parents=True, exist_ok=True)
        token_path = state_dir / ".token"
        token_tmp = state_dir / ".token.tmp"
        token_tmp.write_text(captures["codex"]["token"], encoding="utf-8")
        token_tmp.chmod(0o600)
        os.replace(token_tmp, token_path)

        # The source-bound device grant is an opaque ros_ing token, not the
        # pairing JWT whose `iss` the standalone tap can inspect. Pin the same
        # API origin used for authorization so the unified tap never falls back
        # to a guessed production host (and self-hosted Codex keeps working).
        config_path = state_dir / ".config"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        config["api_base_url"] = base_url.rstrip("/")
        config_tmp = state_dir / ".config.tmp"
        config_tmp.write_text(json.dumps(config, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(config_tmp, config_path)

    for grant in grants:
        if grant not in by_grant:
            # Approved, but the backend minted nothing for it. Say so rather
            # than reporting a capability that will not work.
            messages.append(
                f"! the server did not return a '{grant}' credential — "
                "that capability is NOT active"
            )
    if "capture" in grants:
        missing_sources = [source for source in requested_sources if source not in captures]
        if missing_sources:
            by_grant.pop("capture", None)
            messages.append(
                "! the server did not return capture credentials for "
                f"{', '.join(missing_sources)} — those agents are NOT active"
            )
        for paired_source, credential in captures.items():
            label = "Codex" if paired_source == "codex" else "Claude Code"
            messages.append(
                f"{label} Session capture paired (device {credential.get('device_id', '?')})."
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
            # Capture's runtime switch intentionally describes credential +
            # killswitch state, independently of plugin installation. A direct
            # `codex plugin remove` therefore leaves capture_on=True while the
            # hook that starts the uploader is absent. Keep that independence,
            # but make the manager plan the missing install explicitly.
            if capability is Capability.CAPTURE and want and not caps.capture_plugin_installed:
                steps.append(f"enable {PLAN_LABELS[capability]}")
                continue
            # A STALE block is installed-and-wrong, so want == have and this
            # loop skipped it -- plan() came back empty, the caller returned
            # "Nothing to change", and the refresh branch below it never ran.
            # `probe doctor` meanwhile said "outdated wording -- re-run
            # 'probe wizard'", so the two commands sent the user in a circle
            # and a POINTER_VERSION bump could never reach a machine at all.
            if capability is Capability.AGENT_RULES and want and caps.agent_rules_stale:
                steps.append(f"refresh {PLAN_LABELS[capability]}")
            continue
        label = PLAN_LABELS[capability]
        if not want:
            steps.append(f"disable {label}")
            continue
        # "enable X" when the plugin is ALREADY there and only the credential
        # is missing describes work this run will not do -- and it is the
        # common first-run case, because `capture_on` is credential-only and
        # `tracking_on` is plugin AND login. Name the piece that is actually
        # absent instead.
        plugin_here = {
            Capability.TRACKING: caps.tracking_plugin_installed,
            Capability.CAPTURE: caps.capture_plugin_installed,
        }.get(capability)
        if plugin_here is True:
            steps.append(SIGN_IN_LABELS.get(capability, f"enable {label}"))
        else:
            steps.append(f"enable {label}")
    return steps


def apply_capture(caps: Capabilities, want: bool, *, mode: OffMode, on_retry=None) -> list[str]:
    """Bring capture to `want` and report honestly.

    Turning it OFF goes through the verified postcondition in capture.py rather
    than deleting a file and hoping.
    """
    messages: list[str] = []
    if want:
        # Two INDEPENDENT jobs, and conflating them is a bug in both directions.
        # Clearing the killswitch is what actually turns capture back on for a
        # machine that already has the plugin; installing is only needed when
        # the plugin is absent. Gating the whole step on "plugin absent" (which
        # this function's caller briefly did) left `.disabled` in place while
        # the wizard reported success -- capture silently off after we said on.
        # Gating it on "capture off" instead reinstalls a plugin that is already
        # there. So: always clear, install only when missing.
        clear_killswitch()
        if caps.agent_source == "codex" and caps.legacy_capture_plugin_installed:
            retired = plugin_cli.uninstall("codex", LEGACY_CODEX_TAP_PLUGIN_ID)
            if not retired.ok:
                messages.append(
                    "could not remove the legacy Codex capture plugin "
                    f"{LEGACY_CODEX_TAP_PLUGIN_ID}: {retired.detail}"
                )
                return messages
            messages.append(
                f"Removed legacy {LEGACY_CODEX_TAP_PLUGIN_ID}; the unified tap owns capture now."
            )
        if caps.capture_plugin_installed:
            return messages
        tap_name = CODEX_TAP_PLUGIN_NAME if caps.agent_source == "codex" else TAP_PLUGIN_NAME
        result = install_plugin(tap_name, source=caps.agent_source, on_retry=on_retry)
        if not result.ok:
            messages.append(f"could not install {tap_name}: {result.detail}")
        return messages
    if not caps.capture_on and not caps.capture_token_sources:
        return messages
    result = turn_off(mode)
    messages.append(result.summary())
    messages.extend(f"! {warning}" for warning in result.warnings)
    return messages


def apply_tracking(want: bool, *, on_retry=None) -> list[str]:
    """Bring tracking to `want`.

    Turning it off removes the plugin but deliberately does NOT revoke the PAT
    or log the CLI out: the wizard is not a logout command, and silently
    destroying a credential the user may be scripting against would be a nasty
    surprise. `probe logout` remains the explicit way to do that.
    """
    messages: list[str] = []
    if want:
        result = install_plugin(TRACKING_PLUGIN_NAME, on_retry=on_retry)
        if not result.ok:
            messages.append(f"could not install {TRACKING_PLUGIN_NAME}: {result.detail}")
        return messages
    result = uninstall_plugin(TRACKING_PLUGIN_NAME)
    if not result.ok:
        messages.append(f"could not uninstall {TRACKING_PLUGIN_NAME}: {result.detail}")
    else:
        messages.append(
            "Removed the tracking plugin. Your login is untouched — "
            "run `probe logout` if you also want to revoke this device's token."
        )
    return messages


def _reuse_approval_for_codex_mcp(notes: list[str]) -> bool:
    """Serve Codex the read token the browser approval already minted.

    The approval this run just performed asks for `api` and `mcp` together and
    stores both, so by the time we get here the credential Codex needs is
    already on disk. Sending the user to a second page to mint another one is
    the whole complaint: it is redundant, and it is the step that times out.

    Declines quietly whenever anything is not exactly right -- no token yet, no
    plugin manifest to read the URL from, an unparseable config -- because the
    OAuth flow below still works and a slower install beats a wrong one.
    """
    from probe.sdk.config import load_context

    try:
        token = (load_context() or {}).get("mcp_token")
    except Exception:  # noqa: BLE001 - a config we cannot read is just "no shortcut"
        token = None
    if not token:
        return False

    url = codex_config.plugin_mcp_url(CODEX_MCP_NAME, marketplace=MARKETPLACE)
    if not url:
        return False

    try:
        written = codex_config.write_mcp_bearer(CODEX_MCP_NAME, url=url, token=token)
    except codex_config.ConfigError as exc:
        notes.append(f"! could not reuse your sign-in for the Codex MCP: {exc}")
        return False

    # Ask Codex, rather than trusting that valid TOML is acceptable TOML --
    # `bearer_token` is the standing proof those are different things. A status
    # we cannot read is the same shape as a config Codex cannot load, so the
    # write goes back rather than being left for the fallback to sit on top of.
    if plugin_cli.codex_mcp_auth_status(CODEX_MCP_NAME) != codex_config.BEARER_STATUS:
        codex_config.restore(written)
        notes.append(
            "! Codex did not accept the credential from your sign-in; its config is back as it was."
        )
        return False
    return True


def apply_codex_mcp_auth() -> list[str]:
    """Authorize the Codex-hosted MCP, reusing this run's approval if it can."""
    status = plugin_cli.codex_mcp_auth_status(CODEX_MCP_NAME)
    if status in {"o_auth", "bearer_token"}:
        return []

    notes: list[str] = []
    if _reuse_approval_for_codex_mcp(notes):
        return [*notes, "Codex MCP authorized from your Probe sign-in."]

    result = plugin_cli.login_codex_mcp(CODEX_MCP_NAME)
    if not result.ok:
        return [
            *notes,
            f"could not log in to the {CODEX_MCP_NAME} MCP: {result.detail}. "
            f"Run `codex mcp login {CODEX_MCP_NAME}` and then re-run `probe doctor`.",
        ]
    verified = plugin_cli.codex_mcp_auth_status(CODEX_MCP_NAME)
    if verified not in {"o_auth", "bearer_token"}:
        return [
            *notes,
            f"! Codex completed the login command but {CODEX_MCP_NAME} still reports "
            f"{verified or 'unknown'}; run `codex mcp login {CODEX_MCP_NAME}` again.",
        ]
    return [*notes, f"Codex MCP logged in ({CODEX_MCP_NAME})."]


def apply_auto_update(want: bool) -> list[str]:
    autoupdate.save(enabled=want)
    if not want:
        return ["Auto-update off. You'll still get a nudge when a release lands."]
    return ["Auto-update on."]


def apply_agent_rules(want: bool, *, stale: bool = False) -> list[str]:
    """Write or drop the selected agent's global instruction pointer.

    `stale` re-writes an already-installed block whose version moved, which is
    the only way a wording fix reaches a machine that ticked this once and never
    re-ran the wizard.
    """
    path = agent_rules.memory_path()
    try:
        if want:
            changed = agent_rules.install(path)
        else:
            changed = agent_rules.remove(path)
    except agent_rules.DamagedBlock as exc:
        return [
            f"! Left {path} alone: {exc}.",
            "  Delete the stray probe-research marker by hand, then re-run this.",
        ]
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError is a ValueError, so the OSError guard never caught
        # it: one latin-1 character in a researcher's own CLAUDE.md took the
        # whole wizard down with a traceback, mid-install. "Nothing else was
        # affected" is only true because the write is atomic.
        return [f"Could not update {path}: {exc}. Nothing else was affected."]

    if not want:
        return [f"Removed the Probe block from your global {path.name}."] if changed else []
    if stale and changed:
        return [f"Refreshed the Probe block in {path}."]
    if changed:
        return [
            f"Added a Probe tracking rule to {path}. It points at the skills and "
            "leaves the rest of the file alone."
        ]
    return []


#: The value of the row that ENDS the picker. Not a capability, never returned
#: in a Selection, and deliberately last -- the cursor starts on the first
#: capability, so the choices are in front of you before the way out is.
NEXT = "__next__"

#: What that row says. A verb, not a noun: every other row is a thing you turn
#: on, and this one is the only thing you DO.
NEXT_TITLE = "Next  ›  continue with these settings"


def _menu_row(title: str, detail: tuple[str, ...], *, checked: bool, indent: str) -> str:
    """One capability row, box included.

    WE draw the box (see tui.draw_own_boxes). questionary's own is
    all-rows-or-nothing, and the "Next" row must not have one -- `○ Next` reads
    as an option someone forgot to tick rather than the way forward.
    """
    from probe.cli import tui

    box = tui.TICK if checked else tui.UNTICK
    # The box sits on the title line only; wrapped detail lines clear it, so
    # they do not read as further options.
    return f"\n{indent}  ".join((f"{box} {title}", *detail))


def run_menu(
    defaults: dict[Capability, bool],
    agent_sources: tuple[str, ...] | list[str] | str = ("claude_code",),
):
    """The capability checkbox. Returns None (quit), tui.BACK, or a Selection.

    ENTER ACTIVATES THE ROW UNDER THE CURSOR. On a capability that means toggle;
    on the "Next" row it means done. questionary's default is space-toggles /
    enter-submits, which is the checkbox convention but leaves the way out
    invisible -- people read three rows with no apparent way forward and start
    ticking things to find one. One rule ("enter does the thing you are looking
    at") plus a visible Next row removes the guesswork. Space still toggles, for
    anyone who already has the muscle memory.

    questionary is imported HERE, inside the call, and never at module scope:
    cli/__init__ eagerly imports cli/main, and `probe log` runs inside training
    loops.
    """
    import questionary

    from probe.cli import tui

    tui.use_checkmarks()  # the fallback path, if we cannot take the box over

    indent = tui.body_indent()
    copy = menu_copy(agent_sources)
    rows: dict[Capability, questionary.Choice] = {}
    choices: list = [questionary.Separator(" ")]
    for index, (capability, (title, detail)) in enumerate(copy.items()):
        if index:
            choices.append(questionary.Separator(" "))
        row = questionary.Choice(
            title=_menu_row(title, detail, checked=defaults[capability], indent=indent),
            value=capability,
            checked=defaults[capability],
        )
        rows[capability] = row
        choices.append(row)
    choices.append(questionary.Separator(" "))
    choices.append(questionary.Choice(title=NEXT_TITLE, value=NEXT))

    message = tui.framed(
        "Choose what Probe does on this device.", [], "What should Probe Research do here?"
    )
    question = questionary.checkbox(
        message,
        choices=choices,
        instruction="(enter toggles a row, or picks Next · esc goes back)",
        style=tui.style(),
        qmark=tui.qmark(),
        pointer=tui.pointer(),
    )
    _bind_menu_keys(question, rows, copy=copy, indent=indent)

    picked = tui.ask(question, height=tui.content_height(message, choices))
    if picked is None or picked is tui.BACK:
        return picked
    chosen = set(picked)
    return Selection(
        tracking=Capability.TRACKING in chosen,
        capture=Capability.CAPTURE in chosen,
        agent_rules=Capability.AGENT_RULES in chosen,
        # Carried through untouched; ask_auto_update owns this one.
        auto_update=defaults[Capability.AUTO_UPDATE],
    )


def _bind_menu_keys(
    question,
    rows: dict[Capability, object],
    *,
    copy: dict[Capability, tuple[str, tuple[str, ...]]],
    indent: str,
) -> None:
    """Make enter activate the pointed row, and keep our boxes in sync.

    Every reach into questionary is guarded. If any of it stops working the
    prompt still runs with the library's own behaviour (space toggles, enter
    submits) rather than failing to render -- a picker that looks slightly wrong
    beats a wizard that cannot ask the question at all.
    """
    from probe.cli import tui

    own_boxes = tui.draw_own_boxes(question)
    control = tui.checkbox_control(question)
    if control is None:
        return  # library defaults; enter still submits, space still toggles

    def restyle(capability: Capability) -> None:
        """Redraw one row's box. Only ours to do when we took the box over."""
        if not own_boxes:
            return
        row = rows.get(capability)
        if row is None:
            return
        title, detail = copy[capability]
        row.title = _menu_row(
            title, detail, checked=capability in control.selected_options, indent=indent
        )

    def toggle(capability: Capability) -> None:
        if capability in control.selected_options:
            control.selected_options.remove(capability)
        else:
            control.selected_options.append(capability)
        restyle(capability)

    for capability in rows:
        restyle(capability)

    try:
        bindings = question.application.key_bindings

        @bindings.add("c-m", eager=True)  # Enter
        def _(event) -> None:  # pragma: no cover - requires a live terminal
            pointed = control.get_pointed_at()
            if pointed is None or pointed.value == NEXT:
                control.is_answered = True
                event.app.exit(result=[c for c in control.selected_options if c != NEXT])
                return
            toggle(pointed.value)

        @bindings.add(" ", eager=True)
        def _(event) -> None:  # pragma: no cover - requires a live terminal
            pointed = control.get_pointed_at()
            if pointed is None:
                return
            if pointed.value == NEXT:
                control.is_answered = True
                event.app.exit(result=[c for c in control.selected_options if c != NEXT])
                return
            toggle(pointed.value)

    except Exception:  # noqa: BLE001 - never let a binding break the prompt
        pass


def ask_auto_update(
    default: bool,
    agent_sources: tuple[str, ...] | list[str] | str = ("claude_code",),
):
    """The follow-up step. Returns None, tui.BACK, or a bool.

    Built exactly like the capability picker, and for the same reason. The
    detail used to be `print`ed and the confirm rendered underneath it, so
    prompt_toolkit took a screen that already had two lines on it and the whole
    step sat welded to the top while every other step was centred.
    """
    import questionary

    from probe.cli import tui

    title, detail = auto_update_copy(agent_sources)
    message = tui.framed("Now, how Probe keeps itself current.", tui.wrap(detail), title)
    return tui.ask(
        questionary.confirm(
            message,
            default=default,
            style=tui.style(),
            qmark=tui.qmark(),
        ),
        height=tui.content_height(message),
    )


def confirm_removal(agent_sources: tuple[str, ...] | list[str] | str = ("claude_code",)):
    """The uninstall gate. Returns None, tui.BACK, or a bool.

    A bare `typer.confirm` here was the one prompt in the wizard that printed
    at column 0 -- and it guarded the single destructive action, which is the
    worst place to look like a different program.
    """
    import questionary

    from probe.cli import tui

    message = tui.framed(
        "Remove Probe Research from this device.",
        tui.wrap(
            f"Uninstalls the {agent_label(agent_sources)} plugins, stops session capture and "
            "clears the credentials stored on this machine. Nothing already "
            "sent to your team's knowledgebase is touched."
        ),
        "Remove it?",
    )
    return tui.ask(
        questionary.confirm(
            message,
            default=False,
            style=tui.style(),
            qmark=tui.qmark(),
        ),
        height=tui.content_height(message),
    )


def run_action_menu(caps: Capabilities | dict[str, Capabilities]):
    """The top-level menu. Returns None (quit), tui.BACK, or an Action."""
    import questionary

    from probe.cli import tui
    from probe.cli.actions import ACTION_COPY

    # A leading blank row so the first option is not welded to the question.
    choices: list = [questionary.Separator(" ")]
    for index, (action, (title, detail)) in enumerate(ACTION_COPY.items()):
        if index:
            choices.append(questionary.Separator(" "))
        body = tui.body_indent()
        choices.append(questionary.Choice(title=f"{title}\n{body}  {detail}", value=action))

    from probe.cli import doctor as doctor_impl  # noqa: F401

    message = tui.framed("On this device:", describe_state(caps), "What do you want to do?")
    return tui.ask(
        questionary.select(
            message,
            choices=choices,
            instruction="(arrow keys, enter to choose)",
            style=tui.style(),
            qmark=tui.qmark(),
            pointer=tui.pointer(),
        ),
        height=tui.content_height(message, choices),
    )


def describe_state(caps: Capabilities | dict[str, Capabilities]) -> list[str]:
    """A one-glance summary printed above the menu on a re-run.

    The wizard already knows all of this, so showing it means the user picks an
    action against real state rather than guessing which one they need.
    """
    if isinstance(caps, dict):
        lines: list[str] = []
        for source, snapshot in caps.items():
            label = agent_label(source)
            capture = "on" if snapshot.capture_on else "off"
            if snapshot.capture_killswitched:
                capture = "off (killswitch set)"
            lines.append(
                f"  {label:<28} MCP {'on' if snapshot.tracking_on else 'off'} · capture {capture}"
            )
        first = next(iter(caps.values()), None)
        if first is not None:
            lines.append(
                f"  {'Automatic updates':<28} {'on' if first.auto_update_enabled else 'off'}"
            )
            if first.logged_in_as:
                lines.append(f"  {'Account':<28} {first.logged_in_as}")
            if first.last_update_attempt:
                lines.append(f"  Last update attempt         {first.last_update_attempt}")
        return lines

    lines = []
    lines.append(
        f"  CLI + MCP                   {'on' if caps.tracking_on else 'off'}"
        + (f"  ({caps.logged_in_as})" if caps.logged_in_as else "")
    )
    capture = "on" if caps.capture_on else "off"
    if caps.capture_killswitched:
        capture = "off (killswitch set)"
    lines.append(f"  Session capture             {capture}")
    auto = "on" if caps.auto_update_enabled else "off"
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

    # `uninstall_plugin` returns a Result, not a 2-tuple. Unpacking it raised
    # TypeError on the FIRST line of removal that touches a plugin, so
    # `probe wizard --action uninstall` crashed for everyone -- after the
    # plugins were gone, before the instruction block, the auto-update flag and
    # (below) the Codex MCP entry were dealt with. A half-removed machine that
    # ends in a traceback, and no test caught it because none called this.
    removal = uninstall_plugin(TRACKING_PLUGIN_NAME)
    if not removal.ok and "not found" not in removal.detail.lower():
        messages.append(f"! could not remove {TRACKING_PLUGIN_NAME}: {removal.detail}")

    # The MCP entry we may have written into the user's own config.toml is not
    # ours to leave behind: after this call its token is orphaned, so Codex
    # would keep a server that lists as configured and answers 401.
    if caps.agent_source == "codex":
        try:
            removed = codex_config.remove_mcp_server(CODEX_MCP_NAME)
        except codex_config.ConfigError as exc:
            messages.append(
                f"! left the Codex MCP entry in place: {exc}. "
                f"Delete [mcp_servers.{CODEX_MCP_NAME}] by hand."
            )
        else:
            if removed.changed:
                messages.append(f"Removed the {CODEX_MCP_NAME} MCP entry from {removed.path}.")

    # The block lives OUTSIDE the repo, in the researcher's global CLAUDE.md,
    # and removal used to skip it entirely -- so "Removed." left every agent in
    # every repository still being told to use skills this very call had just
    # uninstalled. It also keeps `Capabilities.configured` True forever, so a
    # fully removed device can never look fresh again.
    messages.extend(apply_agent_rules(False))

    autoupdate.save(enabled=False)
    messages.append(
        "Removed. Your account and any data already sent are untouched — "
        "revoke this device's tokens in Settings if you also want those gone."
    )
    return messages


def restart_notice(caps: Capabilities, selection: Selection) -> str | None:
    """Tell the user to restart their coding agent, when it actually matters.

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
        or caps.legacy_capture_plugin_installed
    )
    if not plugin_changed:
        return None
    agent = "Codex" if caps.agent_source == "codex" else "Claude Code"
    notice = (
        f"Restart {agent} to finish. Plugins and the MCP are read at session "
        "start, so this session will not see them until you do."
    )
    if caps.agent_source == "codex" and selection.capture:
        notice += (
            " In the new Codex session, open `/hooks`, review Probe Session "
            "Capture, and approve it once. Installation succeeds without this "
            "approval, but Codex will not run an untrusted hook."
        )
    return notice
