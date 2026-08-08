"""`probe setup` / `probe doctor`: the flag contract and the off switch.

The two things most likely to hurt someone are covered first: an omitted flag
silently revoking capture in CI, and "off" that does not actually turn capture
off.
"""

from __future__ import annotations

import dataclasses
import json
import os

import pytest

from probe.cli import autoupdate, capture
from probe.cli import capabilities as capabilities_mod
from probe.cli import doctor, setup
from probe.cli.capabilities import (
    ENV_INGEST_TOKEN,
    Capabilities,
    Capability,
    TokenSource,
    capture_token_sources,
)


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Point every state path at a tmpdir so tests never touch a real install."""
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "tap"))
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(tmp_path / "probe" / "config.json"))
    # BOTH conventions: the tap honours PROBE_CONFIG_PATH, the SDK honours
    # XDG_CONFIG_HOME. They agree in production (~/.config/probe/config.json);
    # a test that sets only one writes to the developer's REAL config.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("PROBE_AGENT", raising=False)
    monkeypatch.delenv("PROBE_TAP_SOURCE", raising=False)
    monkeypatch.delenv(ENV_INGEST_TOKEN, raising=False)
    (tmp_path / "tap").mkdir(parents=True, exist_ok=True)
    (tmp_path / "probe").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _caps(**overrides) -> Capabilities:
    return Capabilities(**overrides)


# --- the flag truth table --------------------------------------------------


def test_fresh_machine_defaults_everything_on():
    """A fresh machine gets every capability, capture included.

    This INVERTS the original default and the inversion is deliberate, so the
    assertion is kept rather than deleted: capture used to default off, and a
    silent flip back would be a privacy regression nobody would notice. What
    now carries the consent is the menu -- a ticked, labelled row saying what
    capture sends and where, under the cursor before Next is reachable.
    """
    selection = setup.resolve_selection(
        _caps(), tracking=None, capture=None, auto_update=None, configured=False
    )
    assert selection.tracking is True
    assert selection.capture is True
    assert selection.auto_update is True
    assert selection.agent_rules is True


def test_the_capture_row_still_says_what_it_sends_and_where():
    """Load-bearing now that capture ships ticked. The row IS the disclosure --
    if it stops naming what leaves the machine, a pre-ticked box becomes a
    grant made in silence, which is the thing the default-off used to prevent."""
    _, detail = setup.menu_copy("codex")[Capability.CAPTURE]
    blob = " ".join(detail)
    assert "this device's" in blob
    assert "Codex sessions" in blob
    assert "Claude" not in blob


def test_wizard_copy_names_exactly_the_selected_agents():
    claude = " ".join(setup.menu_copy("claude_code")[Capability.CAPTURE][1])
    codex = " ".join(setup.menu_copy("codex")[Capability.CAPTURE][1])
    both = " ".join(setup.menu_copy(("claude_code", "codex"))[Capability.CAPTURE][1])

    assert "Claude Code sessions" in claude and "Codex" not in claude
    assert "Codex sessions" in codex and "Claude" not in codex
    assert "Claude Code and Codex sessions" in both
    assert "CLAUDE.md" in setup.menu_copy("claude_code")[Capability.AGENT_RULES][0]
    assert "AGENTS.md" in setup.menu_copy("codex")[Capability.AGENT_RULES][0]
    assert "search and track research" in " ".join(
        setup.menu_copy("codex")[Capability.AGENT_RULES][1]
    )


def test_a_scripted_yes_can_still_decline_capture():
    """`--yes` has no screen, so it is the path the new default changes most.
    The flag has to remain a real off switch for anyone automating installs."""
    selection = setup.resolve_selection(
        _caps(), tracking=None, capture=False, auto_update=None, configured=False
    )
    assert selection.capture is False, "--no-capture must beat the default"


def test_rerun_preserves_capture_when_the_flag_is_omitted():
    """The load-bearing case: `probe setup --yes` in CI, or a re-run naming only
    one flag, must never silently revoke a developer's pairing."""
    configured = _caps(
        capture_token_sources=(TokenSource.PAIRED_FILE,),
        tracking_plugin_installed=True,
        logged_in_as="richard@prbe.ai",
    )
    assert configured.capture_on is True

    selection = setup.resolve_selection(configured, tracking=None, capture=None, auto_update=None)
    assert selection.capture is True, "an omitted flag must preserve, never disable"


def test_rerun_preserves_auto_update_when_the_flag_is_omitted():
    configured = _caps(auto_update_enabled=True, logged_in_as="richard@prbe.ai")
    selection = setup.resolve_selection(configured, tracking=None, capture=None, auto_update=None)
    assert selection.auto_update is True


def test_explicit_flags_always_win():
    configured = _caps(capture_token_sources=(TokenSource.PAIRED_FILE,))
    selection = setup.resolve_selection(
        configured, tracking=False, capture=False, auto_update=False
    )
    assert (selection.tracking, selection.capture, selection.auto_update) == (
        False,
        False,
        False,
    )


def test_capture_only_asks_for_capture_alone():
    """Someone who ticked only session capture must not be handed a
    read/write/delete PAT they never asked for."""
    grants = setup.grants_for(
        setup.Selection(tracking=False, capture=True, auto_update=False, agent_rules=False)
    )
    assert grants == ["capture"]


def test_tracking_asks_for_a_separate_read_only_mcp_credential():
    grants = setup.grants_for(
        setup.Selection(tracking=True, capture=True, auto_update=False, agent_rules=False)
    )
    assert grants == ["api", "mcp", "capture"]


# --- capture off is a verified postcondition -------------------------------


def test_off_clears_the_probe_config_token_not_just_the_paired_file(isolate):
    """The bug: the uploader also accepts `ingest_token` from the CLI config,
    which `probe login --ingest-token` writes. Clearing only `.token` lets
    capture resume at the next session start while the menu says it is off."""
    (isolate / "tap" / ".token").write_text("ros_ing_paired")
    (isolate / "probe" / "config.json").write_text(
        json.dumps({"base_url": "https://api.research.prbe.ai", "ingest_token": "ros_ing_cfg"})
    )
    assert set(capture_token_sources()) == {
        TokenSource.PAIRED_FILE,
        TokenSource.PROBE_CONFIG,
    }

    result = capture.turn_off(capture.OffMode.DISABLE)

    assert result.verified is True
    assert capture_token_sources() == ()
    # Unrelated config survives: this is an off switch, not a reset.
    surviving = json.loads((isolate / "probe" / "config.json").read_text())
    assert surviving["base_url"] == "https://api.research.prbe.ai"
    assert "ingest_token" not in surviving


def test_off_sets_the_killswitch_so_the_next_session_does_not_respawn(isolate):
    (isolate / "tap" / ".token").write_text("ros_ing_paired")
    capture.turn_off(capture.OffMode.DISABLE)
    assert (isolate / "tap" / ".disabled").exists()


def test_off_refuses_to_claim_success_while_the_env_var_is_set(isolate, monkeypatch):
    """The one source the wizard cannot fix -- it cannot unset a variable in the
    parent shell. Reporting "off" here would be exactly the lie this guards."""
    (isolate / "tap" / ".token").write_text("ros_ing_paired")
    monkeypatch.setenv(ENV_INGEST_TOKEN, "ros_ing_from_env")

    result = capture.turn_off(capture.OffMode.DISABLE)

    assert result.verified is False
    assert TokenSource.ENVIRONMENT in result.remaining
    assert any(ENV_INGEST_TOKEN in warning for warning in result.warnings)
    assert "NOT fully off" in result.summary()


def test_turning_capture_back_on_clears_the_killswitch(isolate):
    (isolate / "tap" / ".token").write_text("ros_ing_paired")
    capture.turn_off(capture.OffMode.DISABLE)
    assert (isolate / "tap" / ".disabled").exists()
    capture.clear_killswitch()
    assert not (isolate / "tap" / ".disabled").exists()


def test_killswitch_alone_means_capture_is_not_on(isolate):
    """A paired device with the killswitch set ships nothing, so the menu must
    not show capture as on."""
    caps = _caps(capture_token_sources=(TokenSource.PAIRED_FILE,), capture_killswitched=True)
    assert caps.capture_on is False


def test_rejected_capture_credential_is_not_treated_as_live():
    caps = _caps(
        capture_token_sources=(TokenSource.PAIRED_FILE,),
        capture_credential_valid=False,
    )
    assert caps.capture_on is False
    selection = setup.Selection(tracking=False, capture=True, auto_update=False, agent_rules=False)
    assert setup.needs_authorization(caps, selection) == ["capture"]


def test_doctor_explains_a_rejected_capture_credential():
    report = doctor.render(
        _caps(
            capture_token_sources=(TokenSource.PAIRED_FILE,),
            capture_credential_valid=False,
        )
    )
    assert "rejected" in report
    assert "probe wizard" in report


def test_capture_credential_probe_distinguishes_rejection_from_offline(isolate, monkeypatch):
    import urllib.error

    (isolate / "tap" / ".token").write_text("ros_ing_test")
    (isolate / "tap" / ".config").write_text(json.dumps({"api_base_url": "https://api.test"}))

    def rejected(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://api.test", 401, "no", {}, None)

    monkeypatch.setattr(capabilities_mod.urllib.request, "urlopen", rejected)
    assert capabilities_mod.verify_capture_credential() is False

    monkeypatch.setattr(
        capabilities_mod.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )
    assert capabilities_mod.verify_capture_credential() is None


# --- auto-update -----------------------------------------------------------


def test_auto_update_defaults_off_and_round_trips(isolate):
    assert autoupdate.load().enabled is False
    autoupdate.save(enabled=True)
    assert autoupdate.load().enabled is True


def test_a_channel_written_by_an_older_cli_is_ignored_not_rejected(isolate):
    """There is one channel. `stable` was stored, passed and validated, and read
    by nothing — but the state file is also read by the plugin hook, which can
    be older than the CLI, so an existing key must not break loading."""
    autoupdate.save(enabled=True)
    raw = json.loads(autoupdate.state_path().read_text())
    raw["channel"] = "stable"
    autoupdate.state_path().write_text(json.dumps(raw))

    assert autoupdate.load().enabled is True
    assert not hasattr(autoupdate.load(), "channel")
    assert not hasattr(autoupdate, "Channel")


def test_corrupt_state_reads_as_off_rather_than_guessing_on(isolate):
    autoupdate.state_dir().mkdir(parents=True, exist_ok=True)
    autoupdate.state_path().write_text("{not json")
    assert autoupdate.load().enabled is False


def test_last_attempt_is_recorded_so_a_silent_failure_is_visible(isolate):
    autoupdate.record_attempt(
        autoupdate.Attempt(at=1_700_000_000, ok=False, detail="network unreachable")
    )
    described = autoupdate.load().last_attempt.describe()
    assert "FAILED" in described
    assert "network unreachable" in described


def test_only_one_session_may_upgrade_at_a_time(isolate):
    """Several Claude Code sessions starting at once would otherwise each spawn
    `uv tool upgrade` against the same install."""
    assert autoupdate.acquire_lock() is True
    assert autoupdate.acquire_lock() is False
    autoupdate.release_lock()
    assert autoupdate.acquire_lock() is True


def test_a_stale_lock_does_not_wedge_auto_update_forever(isolate):
    assert autoupdate.acquire_lock() is True
    stale = autoupdate.lock_path()
    os.utime(stale, (0, 0))
    assert autoupdate.acquire_lock() is True


# --- the plan is printed before anything is touched ------------------------


def test_the_plan_survives_the_answer_every_fresh_machine_gives():
    """The exact crash: a fresh install answers "yes" to auto-update, the plan
    has to name a capability that is not a checkbox row, and the wizard died
    with a KeyError before it had installed anything. Fresh is the WORST case,
    not an edge one -- auto-update defaults on and starts off, so it changes
    state on every machine that has never been set up."""
    selection = setup.resolve_selection(
        _caps(), tracking=None, capture=None, auto_update=None, configured=False
    )
    steps = setup.plan(_caps(), selection)

    assert any("automatic updates" in step for step in steps)
    assert all(step.startswith(("enable ", "disable ")) for step in steps)


def test_every_capability_can_be_named_in_a_plan():
    """PLAN_LABELS must stay TOTAL over Capability. A partial map is what broke
    the wizard, and the failure landed at a user's terminal rather than in CI
    because nothing here asserted the mapping covered the enum."""
    assert set(setup.PLAN_LABELS) == set(Capability)


def test_the_plan_lists_only_what_actually_changes():
    """It is an audit trail, so a run that changes one thing must not claim to
    change three."""
    caps = _caps(
        tracking_plugin_installed=True,
        logged_in_as="richard@prbe.ai",
        auto_update_enabled=True,
    )
    steps = setup.plan(
        caps, setup.Selection(tracking=True, capture=True, auto_update=True, agent_rules=False)
    )

    assert len(steps) == 1
    assert steps[0].startswith("enable ")
    assert "capture" in steps[0].lower()


def test_no_change_means_no_plan():
    caps = _caps(
        tracking_plugin_installed=True,
        capture_plugin_installed=True,
        logged_in_as="richard@prbe.ai",
        capture_token_sources=(TokenSource.PAIRED_FILE,),
        auto_update_enabled=True,
    )
    selection = setup.Selection(tracking=True, capture=True, auto_update=True, agent_rules=False)
    assert setup.plan(caps, selection) == []


# --- doctor ----------------------------------------------------------------


def test_doctor_names_every_credential_source_not_just_the_winning_one():
    """A user who believes capture is off deserves to see what is keeping it
    alive."""
    report = doctor.render(
        _caps(
            capture_token_sources=(TokenSource.PAIRED_FILE, TokenSource.ENVIRONMENT),
            capture_plugin_installed=True,
        )
    )
    assert "paired device token" in report
    assert ENV_INGEST_TOKEN in report


def test_doctor_names_the_selected_agents_global_instructions():
    assert "Global CLAUDE.md" in doctor.render(_caps(agent_source="claude_code"))
    assert "Global AGENTS.md" in doctor.render(_caps(agent_source="codex"))


def test_doctor_reports_a_never_run_updater_distinctly_from_a_working_one():
    assert "never run on this device" in doctor.render(_caps())
    assert "FAILED" in doctor.render(_caps(last_update_attempt="FAILED (2026-07-24 10:00): boom"))


def test_doctor_renders_on_a_bare_machine_without_raising():
    # The command people run when everything is already broken.
    assert "Probe Research doctor" in doctor.render(_caps())


def test_menu_rows_are_capabilities_not_plugin_names():
    """Nobody knows what `probe-research-tap` is; the consent decision is about
    what the thing does."""
    # AUTO_UPDATE is asked separately now: it is a policy about the
    # capabilities, not one of them.
    assert set(setup.MENU_COPY) == {
        Capability.TRACKING,
        Capability.CAPTURE,
        Capability.AGENT_RULES,
    }
    for title, detail in setup.MENU_COPY.values():
        assert "probe-research" not in title
        assert not any("probe-research" in line for line in detail)


def test_capture_menu_copy_is_scoped_to_this_device():
    """The picker says WHAT and WHERE, in one line.

    The full disclosure -- prompts, file contents, tool output, server-side
    secret stripping -- lives on the BROWSER APPROVAL screen, which is where
    the grant is actually made and where research-os asserts it verbatim.
    Repeating three lines of it here made the shortest menu in the product the
    densest thing to read.
    """
    _, detail = setup.menu_copy(("claude_code", "codex"))[Capability.CAPTURE]
    blob = " ".join(detail)
    assert "this device's" in blob, "scope must be explicit"
    assert "Claude Code and Codex sessions" in blob
    # One line, not a wall.
    assert len(detail) == 1


# --- fixes from the adversarial review ------------------------------------


def test_config_credentials_read_the_active_context_like_the_uploader(isolate):
    """The uploader reads contexts[current_context] and does NOT fall back to
    the top level. Reading only the top level would miss the credential on every
    modern config, so teardown would clear nothing and still report success."""
    from probe.cli.capabilities import probe_config_credentials

    (isolate / "probe" / "config.json").write_text(
        json.dumps(
            {
                "ingest_token": "ros_ing_v1_should_be_ignored",
                "current_context": "work",
                "contexts": {
                    "work": {"ingest_token": "ros_ing_active"},
                    "other": {"ingest_token": "ros_ing_inactive"},
                },
            }
        )
    )
    assert probe_config_credentials()["ingest_token"] == "ros_ing_active"
    assert TokenSource.PROBE_CONFIG in capture_token_sources()


def test_off_clears_a_context_scoped_token_and_verifies(isolate):
    (isolate / "probe" / "config.json").write_text(
        json.dumps(
            {
                "current_context": "work",
                "contexts": {"work": {"ingest_token": "ros_ing_active"}},
            }
        )
    )
    assert TokenSource.PROBE_CONFIG in capture_token_sources()
    result = capture.turn_off(capture.OffMode.DISABLE)
    assert result.verified is True
    assert capture_token_sources() == ()


def test_off_is_not_verified_when_the_killswitch_could_not_be_written(isolate, monkeypatch):
    """Credentials gone is not enough: without the killswitch the next session
    respawns the uploader."""
    monkeypatch.setattr(capture, "_set_killswitch", lambda: False)
    result = capture.turn_off(capture.OffMode.DISABLE)
    assert result.verified is False
    assert "killswitch not set" in result.summary()


def test_off_is_not_verified_while_an_uploader_survives(isolate, monkeypatch):
    """A daemon that ignored SIGTERM still holds its bearer and its queue."""
    monkeypatch.setattr(capture, "_stop_daemon", lambda: (False, ["still running"]))
    result = capture.turn_off(capture.OffMode.DISABLE)
    assert result.verified is False
    assert "uploader still running" in result.summary()


def test_a_planted_pid_file_cannot_get_an_unrelated_process_killed(isolate, monkeypatch):
    """/tmp is world-writable, so the PID files there are untrusted input."""
    killed: list[int] = []
    monkeypatch.setattr(capture.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(capture, "_looks_like_the_uploader", lambda pid: False)
    capture._stop_daemon()
    assert killed == [], "signalled a process that is not the uploader"


def test_an_all_off_install_is_not_mistaken_for_a_fresh_machine():
    """Someone who ran setup and turned everything off has still configured this
    machine. Treating it as fresh lets `--yes` switch things back on."""
    all_off = _caps(capture_plugin_installed=True)
    assert all_off.enabled() == {
        Capability.TRACKING: False,
        Capability.CAPTURE: False,
        Capability.AUTO_UPDATE: False,
        Capability.AGENT_RULES: False,
    }
    assert all_off.configured is True

    selection = setup.resolve_selection(all_off, tracking=None, capture=None, auto_update=None)
    assert selection.tracking is False
    assert selection.auto_update is False


def test_a_genuinely_fresh_machine_still_gets_defaults():
    fresh = _caps()
    assert fresh.configured is False
    selection = setup.resolve_selection(fresh, tracking=None, capture=None, auto_update=None)
    assert selection.tracking is True
    assert selection.auto_update is True


def test_legacy_codex_tap_counts_as_an_existing_configuration():
    assert _caps(legacy_capture_plugin_installed=True).configured is True


# --- the pieces that make it actually work --------------------------------


def test_setup_requests_only_the_grants_it_still_needs():
    """A re-run where everything already works must not drag the user through
    another browser approval."""
    fully_set_up = _caps(
        tracking_plugin_installed=True,
        logged_in_as="richard@prbe.ai",
        capture_token_sources=(TokenSource.PAIRED_FILE,),
    )
    everything = setup.Selection(tracking=True, capture=True, auto_update=True, agent_rules=False)
    assert setup.needs_authorization(fully_set_up, everything) == []

    # Logged in, but capture was never paired: ask for capture ALONE.
    tracking_only = _caps(tracking_plugin_installed=True, logged_in_as="richard@prbe.ai")
    assert setup.needs_authorization(tracking_only, everything) == ["capture"]

    # Nothing yet: ask for all three in one approval.
    assert setup.needs_authorization(_caps(), everything) == ["api", "mcp", "capture"]


def test_codex_mcp_login_is_verified_after_the_supported_oauth_flow(monkeypatch):
    from probe.cli import claude_cli, plugin_cli

    statuses = iter(["not_logged_in", "o_auth"])
    monkeypatch.setattr(plugin_cli, "codex_mcp_auth_status", lambda _name: next(statuses))
    monkeypatch.setattr(
        plugin_cli,
        "login_codex_mcp",
        lambda _name: claude_cli.Result(ok=True, detail="Login successful"),
    )
    assert setup.apply_codex_mcp_auth() == ["Codex MCP logged in (probe-research)."]


def test_codex_mcp_login_failure_is_actionable(monkeypatch):
    from probe.cli import claude_cli, plugin_cli

    monkeypatch.setattr(plugin_cli, "codex_mcp_auth_status", lambda _name: "not_logged_in")
    monkeypatch.setattr(
        plugin_cli,
        "login_codex_mcp",
        lambda _name: claude_cli.Result(ok=False, detail="browser cancelled"),
    )
    messages = setup.apply_codex_mcp_auth()
    assert "codex mcp login probe-research" in messages[0]


def test_codex_capture_retires_the_legacy_plugin_before_using_unified_tap(monkeypatch):
    from probe.cli import claude_cli, plugin_cli

    removed: list[str] = []
    monkeypatch.setattr(
        plugin_cli,
        "uninstall",
        lambda source, plugin_id: (
            removed.append(f"{source}:{plugin_id}") or claude_cli.Result(ok=True, detail="removed")
        ),
    )
    messages = setup.apply_capture(
        _caps(
            agent_source="codex",
            capture_plugin_installed=True,
            legacy_capture_plugin_installed=True,
        ),
        True,
        mode=capture.OffMode.DISABLE,
    )
    assert removed == ["codex:prbe-codex-tap-plugin@prbe-ai"]
    assert any("Removed legacy" in message for message in messages)


def test_authorize_persists_every_minted_credential(isolate, monkeypatch):
    """The gap this closes: computing a grant set and never sending it left
    capture off after a setup that said it turned it on."""
    sent = {}

    def fake_device_authorize(base_url, **kwargs):
        sent.update(kwargs)
        return {
            "token": "probe_pat_api",
            "id": "api-id",
            "grants": [
                {"grant": "api", "token": "probe_pat_api", "token_id": "api-id"},
                {"grant": "mcp", "token": "probe_pat_mcp", "token_id": "mcp-id"},
                {"grant": "capture", "token": "ros_ing_dev", "device_id": "dev-1"},
            ],
        }

    monkeypatch.setattr("probe.sdk.device.device_authorize", fake_device_authorize)

    by_grant, messages = setup.authorize(
        ["api", "mcp", "capture"],
        base_url="https://api.research.prbe.ai",
        open_browser=False,
    )

    assert sent["grants"] == ["api", "mcp", "capture"]
    assert sent["capture_source"] == "claude_code"
    assert set(by_grant) == {"api", "mcp", "capture"}
    assert any("paired" in m for m in messages)

    # The capture credential landed where the uploader actually looks for it.
    assert TokenSource.PROBE_CONFIG in capture_token_sources()
    from probe.cli.capabilities import probe_config_credentials

    creds = probe_config_credentials()
    assert creds["token"] == "probe_pat_api"
    assert creds["mcp_token"] == "probe_pat_mcp"
    assert creds["ingest_token"] == "ros_ing_dev"


def test_codex_authorization_is_source_bound_and_writes_the_codex_token(
    isolate, monkeypatch, tmp_path
):
    sent = {}

    def fake_device_authorize(base_url, **kwargs):
        sent.update(kwargs)
        return {"grants": [{"grant": "capture", "token": "ros_ing_codex", "device_id": "cx-1"}]}

    monkeypatch.setenv("PROBE_AGENT", "codex")
    monkeypatch.setenv("PRBE_CODEX_TAP_PLUGIN_DIR", str(tmp_path))
    monkeypatch.setattr("probe.sdk.device.device_authorize", fake_device_authorize)

    by_grant, _messages = setup.authorize(
        ["capture"], base_url="https://api.research.prbe.ai", open_browser=False
    )

    assert sent["capture_source"] == "codex"
    assert by_grant["capture"]["device_id"] == "cx-1"
    assert (tmp_path / ".token").read_text() == "ros_ing_codex"
    assert (tmp_path / ".token").stat().st_mode & 0o777 == 0o600


def test_one_authorization_pairs_claude_and_codex_with_distinct_tokens(
    isolate, monkeypatch, tmp_path
):
    sent = {}

    def fake_device_authorize(base_url, **kwargs):
        sent.update(kwargs)
        return {
            "grants": [
                {
                    "grant": "capture",
                    "capture_source": "claude_code",
                    "token": "ros_ing_claude",
                    "device_id": "cc-1",
                },
                {
                    "grant": "capture",
                    "capture_source": "codex",
                    "token": "ros_ing_codex",
                    "device_id": "cx-1",
                },
            ]
        }

    codex_state = tmp_path / "codex-tap"
    monkeypatch.setenv("PRBE_CODEX_TAP_PLUGIN_DIR", str(codex_state))
    monkeypatch.setattr("probe.sdk.device.device_authorize", fake_device_authorize)

    granted, messages = setup.authorize(
        ["capture"],
        capture_sources=["claude_code", "codex"],
        base_url="https://api.research.prbe.ai",
        open_browser=False,
    )

    assert sent["capture_sources"] == ["claude_code", "codex"]
    assert "capture_source" not in sent
    assert granted["capture"]["capture_source"] == "claude_code"
    assert capture_token_sources("claude_code") == (TokenSource.PROBE_CONFIG,)
    assert (codex_state / ".token").read_text() == "ros_ing_codex"
    assert any("Claude Code Session capture paired" in message for message in messages)
    assert any("Codex Session capture paired" in message for message in messages)


def test_capture_credentials_are_indexed_by_source_without_collapsing():
    from probe.sdk.device import capture_credentials_by_source

    minted = {
        "grants": [
            {"grant": "capture", "capture_source": "claude_code", "token": "one"},
            {"grant": "capture", "capture_source": "codex", "token": "two"},
        ]
    }
    assert {
        source: credential["token"]
        for source, credential in capture_credentials_by_source(minted).items()
    } == {"claude_code": "one", "codex": "two"}


def test_codex_plugin_install_uses_codex_marketplace_commands(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setenv("PROBE_AGENT", "codex")
    monkeypatch.setattr(
        setup.plugin_cli,
        "install",
        lambda source, plugin_id: (
            calls.append([source, plugin_id]) or setup.claude_cli.Result(ok=True, detail="ok")
        ),
    )

    assert setup.install_plugin("probe-research-tap").ok is True
    assert calls == [
        ["codex", "probe-research-tap@research-os-agent"],
    ]


def test_authorize_says_so_when_the_server_returns_nothing_for_a_grant(isolate, monkeypatch):
    """Approved but not minted must not read as success."""
    monkeypatch.setattr(
        "probe.sdk.device.device_authorize",
        lambda base_url, **kw: {"grants": [{"grant": "api", "token": "probe_pat_x"}]},
    )
    _, messages = setup.authorize(["api", "capture"], base_url="https://x", open_browser=False)
    assert any("capture" in m and "NOT active" in m for m in messages)


def test_authorize_reports_a_failed_approval_instead_of_claiming_success(isolate, monkeypatch):
    from probe.sdk.device import DeviceLoginError

    def boom(base_url, **kw):
        raise DeviceLoginError("the user denied this request")

    monkeypatch.setattr("probe.sdk.device.device_authorize", boom)
    by_grant, messages = setup.authorize(["api"], base_url="https://x", open_browser=False)
    assert by_grant == {}
    assert any("denied" in m for m in messages)


def test_older_backend_without_grants_still_yields_the_api_credential():
    """A backend that predates grants returns only the top-level PAT."""
    from probe.sdk.device import credentials_by_grant

    assert credentials_by_grant({"token": "probe_pat_old", "id": "t1"}) == {
        "api": {"grant": "api", "token": "probe_pat_old", "token_id": "t1"}
    }


def test_device_authorize_omits_grants_entirely_when_not_asked(monkeypatch):
    """Sending `grants: null` would fail validation on an older backend."""
    import httpx

    captured = {}

    class FakeClient:
        def post(self, path, json=None):
            captured.update(json or {})
            raise httpx.HTTPError("stop here")

        def close(self):
            pass

    from probe.sdk import device

    with pytest.raises(device.DeviceLoginError):
        device.device_authorize("https://x", client=FakeClient(), open_browser=False)
    assert "grants" not in captured


# --- the collapsed dashboard sections, now wizard actions ------------------


def test_manual_steps_are_generated_from_the_same_constants_the_wizard_uses():
    """The page's copy had already drifted from the commands printed beside it.
    A script generated from the real constants cannot drift."""
    from probe.cli.actions import manual_steps
    from probe.cli.capabilities import MARKETPLACE_REPO, PLUGIN_ID, TAP_PLUGIN_ID

    steps = manual_steps(base_url="https://api.research.prbe.ai")
    assert f"claude plugin marketplace add {MARKETPLACE_REPO}" in steps
    assert f"claude plugin install {PLUGIN_ID}" in steps
    assert f"claude plugin install {TAP_PLUGIN_ID}" in steps
    # `add` does not refresh an already-added marketplace; the update must be there.
    assert "marketplace update" in steps
    assert steps.index("marketplace add") < steps.index("marketplace update")
    assert "probe login --base-url https://api.research.prbe.ai" in steps
    # Never a credential, and never a git URL (a moving branch nobody can name).
    assert "git+https://" not in steps
    assert "--token" not in steps


def test_manual_steps_use_codex_verbs_when_codex_is_selected():
    from probe.cli.actions import manual_steps

    steps = manual_steps(base_url="https://api.research.prbe.ai", agent_source="codex")
    assert "codex plugin marketplace upgrade research-os-agent" in steps
    assert "codex plugin add probe-research@research-os-agent" in steps
    assert "codex mcp login probe-research" in steps
    assert "claude plugin" not in steps


def test_manual_steps_include_each_selected_agent():
    from probe.cli.actions import manual_steps

    steps = manual_steps(
        base_url="https://api.research.prbe.ai",
        agent_source=("claude_code", "codex"),
    )
    assert "claude plugin marketplace update research-os-agent" in steps
    assert "codex plugin marketplace upgrade research-os-agent" in steps
    assert "claude plugin install probe-research@research-os-agent" in steps
    assert "codex plugin add probe-research@research-os-agent" in steps
    assert "codex mcp login probe-research" in steps


def test_troubleshooting_is_state_aware_not_a_static_list():
    """A static list makes the reader work out which item applies. The wizard
    already knows, so it should only say the relevant things."""
    from probe.cli.actions import troubleshooting

    no_claude = troubleshooting(_caps(cli_version="0.9.2"))
    assert any("not on PATH" in note for note in no_claude)

    healthy = troubleshooting(
        _caps(cli_version="0.9.2", claude_available=True, logged_in_as="a@b.c")
    )
    assert not any("not on PATH" in note for note in healthy)

    # The footgun that cannot heal itself is ALWAYS surfaced: nothing in the
    # product can unset a variable in the user's shell.
    for notes in (no_claude, healthy):
        assert any("PROBE_MCP_TOKEN" in note and "SHADOWS" in note for note in notes)


def test_codex_troubleshooting_uses_native_oauth_language():
    from probe.cli.actions import troubleshooting

    notes = troubleshooting(_caps(agent_source="codex", codex_available=True))
    blob = " ".join(notes)
    assert "codex mcp login probe-research" in blob
    assert "PROBE_MCP_TOKEN" not in blob


def test_installed_but_not_logged_in_is_called_out():
    from probe.cli.actions import troubleshooting

    notes = troubleshooting(
        _caps(cli_version="0.9.2", claude_available=True, tracking_plugin_installed=True)
    )
    assert any("not logged in" in note for note in notes)


def test_every_menu_action_has_copy():
    from probe.cli.actions import ACTION_COPY, Action

    # MANUAL is deliberately absent: reachable via `--action manual` for
    # air-gapped users, but it is the rarest path and it made the menu longer
    # for everyone else.
    assert set(ACTION_COPY) == set(Action) - {Action.MANUAL}
    for title, detail in ACTION_COPY.values():
        assert title and detail


def test_the_menu_reads_install_then_uninstall_then_the_rest():
    """Install and Uninstall are the same decision in two directions; splitting
    them across the list makes both harder to find."""
    from probe.cli.actions import ACTION_COPY

    titles = [t for t, _ in ACTION_COPY.values()]
    assert titles[0].endswith("Install Probe") and titles[0].startswith("★")
    assert titles[1] == "Uninstall Probe"
    assert titles[-1] == "Exit"
    assert not any("manual" in t.lower() for t in titles)


def test_state_summary_shows_the_killswitch_rather_than_a_bare_off(isolate):
    """ "off" and "off because you disabled it" are different situations."""
    killswitched = _caps(
        capture_token_sources=(TokenSource.PAIRED_FILE,), capture_killswitched=True
    )
    assert any("killswitch" in line for line in setup.describe_state(killswitched))
    plain = _caps()
    assert not any("killswitch" in line for line in setup.describe_state(plain))


def test_self_host_notes_keep_the_hosted_endpoint_and_air_gap_path():
    from probe.cli.actions import self_host_notes

    notes = self_host_notes(
        base_url="https://api.research.prbe.ai",
        mcp_endpoint="https://mcp.research.prbe.ai/mcp",
    )
    assert "mcp.research.prbe.ai/mcp" in notes
    assert "PROBE_MCP_TOKEN=YOUR_READ_TOKEN" in notes
    assert "probe-research-mcp" in notes


# --- the wizard must leave a real binary behind ---------------------------


def test_ephemeral_launch_installs_the_cli_persistently(monkeypatch):
    """`npx probe-research` runs us through an EPHEMERAL `uv tool run`, which
    leaves nothing installed. Everything after the wizard assumes a real binary:
    `probe doctor`, the plugin's version-check hook, the MCP headers helper."""
    from probe.cli import bootstrap

    calls = []
    monkeypatch.setattr(bootstrap, "_resolves_on_path", lambda: False)
    monkeypatch.setattr(
        bootstrap.shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(bootstrap.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Done())

    result = bootstrap.ensure_persistent_install()

    assert result.installed is True
    install = [c for c in calls if "install" in c][0]
    assert install[:4] == ["uv", "tool", "install", "--force"]
    # The legacy distribution owns the same `probe` binary, so it must be
    # removed FIRST or the old build keeps answering.
    assert calls[0][:3] == ["uv", "tool", "uninstall"]


def test_an_existing_install_is_left_alone(monkeypatch):
    """A re-run must not reinstall on every invocation."""
    from probe.cli import bootstrap

    monkeypatch.setattr(bootstrap, "_resolves_on_path", lambda: True)
    result = bootstrap.ensure_persistent_install()
    assert result.already_persistent is True
    assert result.installed is False
    assert result.message == ""


def test_binary_outside_PATH_still_counts_as_installed(monkeypatch, tmp_path):
    """Claude Code launched from the dock sources no profile, so ~/.local/bin
    can be missing from PATH while the binary is right there. The plugin hook
    checks the same fallbacks, so we must agree or we reinstall forever."""
    from probe.cli import bootstrap

    fake = tmp_path / ".local" / "bin" / "probe"
    fake.parent.mkdir(parents=True)
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: None)
    monkeypatch.setattr(bootstrap.os.path, "expanduser", lambda _: str(tmp_path))

    # Found outside PATH — the plugin hook checks these same fallbacks.
    assert bootstrap._installed_binary() == str(fake)
    # And a new-enough one counts as installed.
    monkeypatch.setattr(bootstrap, "_version_of", lambda b: "99.0.0")
    assert bootstrap._resolves_on_path() is True


def test_no_uv_or_pipx_warns_instead_of_silently_continuing(monkeypatch):
    from probe.cli import bootstrap

    monkeypatch.setattr(bootstrap, "_resolves_on_path", lambda: False)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: None)
    result = bootstrap.ensure_persistent_install()
    assert result.installed is False
    assert "neither uv nor pipx" in result.message
    assert "probe doctor" in result.message


def test_never_falls_back_to_bare_pip(monkeypatch):
    """On a researcher's machine bare pip usually means conda or system Python,
    and mutating that to run an installer breaks training runs days later."""
    from probe.cli import bootstrap

    monkeypatch.setattr(bootstrap, "_resolves_on_path", lambda: False)
    monkeypatch.setattr(bootstrap.shutil, "which", lambda _: None)
    result = bootstrap.ensure_persistent_install()
    assert "pip install" not in result.message


def test_setup_is_still_a_working_alias_for_wizard():
    """`probe setup` is on the live connect page and in shipped plugin copy.
    It is the SAME callable, so the alias can never lose a flag."""
    from probe.cli.main import app

    names = {c.name for c in app.registered_commands}
    assert {"wizard", "setup"} <= names
    by_name = {c.name: c for c in app.registered_commands}
    assert by_name["setup"].callback is by_name["wizard"].callback


def test_installing_a_plugin_tells_you_to_restart_claude_code():
    """Plugins and the MCP are read at session start, and `probe` cannot restart
    Claude Code. Without this the wizard says "done" and nothing works in the
    session the user is sitting in — the last mile of the exact problem this
    feature exists to solve."""
    fresh = _caps()
    turning_on = setup.Selection(tracking=True, capture=False, auto_update=False, agent_rules=False)
    notice = setup.restart_notice(fresh, turning_on)
    assert notice and "Restart Claude Code" in notice


def test_capture_alone_also_needs_a_restart():
    notice = setup.restart_notice(
        _caps(), setup.Selection(tracking=False, capture=True, auto_update=False, agent_rules=False)
    )
    assert notice is not None


def test_codex_capture_restart_notice_explains_hook_trust_boundary():
    notice = setup.restart_notice(
        _caps(agent_source="codex"),
        setup.Selection(tracking=False, capture=True, auto_update=False, agent_rules=False),
    )
    assert notice is not None
    assert "/hooks" in notice
    assert "Installation succeeds" in notice
    assert "untrusted hook" in notice


def test_retiring_legacy_codex_tap_requires_a_restart_even_when_capture_stays_on():
    caps = _caps(
        agent_source="codex",
        capture_plugin_installed=True,
        legacy_capture_plugin_installed=True,
        capture_token_sources=(TokenSource.PAIRED_FILE,),
        capture_credential_valid=True,
    )
    selection = setup.Selection(tracking=False, capture=True, auto_update=False, agent_rules=False)
    assert setup.restart_notice(caps, selection) is not None


def test_an_auto_update_only_change_does_not_send_you_off_to_restart():
    """No plugin moved, so there is nothing for a restart to pick up."""
    already = _caps(
        tracking_plugin_installed=True,
        logged_in_as="richard@prbe.ai",
        capture_token_sources=(TokenSource.PAIRED_FILE,),
    )
    same_plugins = setup.Selection(tracking=True, capture=True, auto_update=True, agent_rules=False)
    assert setup.restart_notice(already, same_plugins) is None


def test_turning_a_plugin_OFF_also_needs_a_restart():
    already = _caps(tracking_plugin_installed=True, logged_in_as="richard@prbe.ai")
    turning_off = setup.Selection(
        tracking=False, capture=False, auto_update=False, agent_rules=False
    )
    assert setup.restart_notice(already, turning_off) is not None


# --- menu readability ------------------------------------------------------


def test_menu_entries_are_separated():
    """Without a blank row between them, one option's title sits directly under
    the previous option's description at a similar indent, and the menu reads as
    a paragraph rather than a list — exactly the thing you scan to choose."""
    import inspect

    for fn in (setup.run_action_menu, setup.run_menu):
        source = inspect.getsource(fn)
        assert "Separator" in source, f"{fn.__name__} must separate entries"
        # Separator("") is FALSY, so questionary falls back to its dashed
        # default line and you get `---------------` between every option.
        assert 'Separator("")' not in source


def test_every_menu_line_fits_a_narrow_terminal():
    """80 columns, minus the 5-space description indent. A wrapped description
    breaks mid-word and undoes the separation entirely."""
    from probe.cli.actions import ACTION_COPY

    for title, detail in ACTION_COPY.values():
        assert len(title) + 5 <= 80, title
        assert len(detail) + 5 <= 80, detail

    for title, detail in setup.MENU_COPY.values():
        assert len(title) + 5 <= 80, title
        for line in detail:
            assert len(line) + 5 <= 80, line
    assert len(setup.AUTO_UPDATE_COPY[0]) + 5 <= 80


def test_title_and_description_stay_together():
    """The description belongs to its title, so they must be ONE choice — a
    separator between them would orphan the description."""
    import inspect

    source = inspect.getsource(setup.run_action_menu)
    # The pointer carries the indent now, so only continuation lines are padded.
    # Title and detail must still be ONE choice: a separator between them would
    # orphan the description from its title.
    assert 'title=f"{title}\\n{body}' in source


# --- the wizard DOES things, it does not print commands --------------------


def test_every_action_acts_rather_than_printing_a_command():
    """The Update action used to print "Run: probe update" and exit. Bouncing
    the user back to a shell to type a command themselves is exactly the
    failure a wizard exists to remove."""
    import inspect
    import sys

    # NOT `from probe.cli import main` -- probe/cli/__init__.py defines a main()
    # FUNCTION that shadows the submodule of the same name.
    import probe.cli.main  # noqa: F401

    cli_main = sys.modules["probe.cli.main"]

    source = inspect.getsource(cli_main.wizard) + inspect.getsource(cli_main._run_wizard_action)
    # The specific regression: a literal instruction to go run something.
    assert 'print("Run:' not in source
    assert "perform_update" in source, "Update must perform the update in-process"
    assert "remove_everything" in source


def test_update_command_is_hidden_but_still_works():
    """Deleting it outright would silently break auto-update on every machine
    whose plugin has not been refreshed — plugins update on the USER's
    schedule, not ours."""
    import sys

    import probe.cli.main  # noqa: F401

    app = sys.modules["probe.cli.main"].app

    by_name = {c.name: c for c in app.registered_commands}
    assert "update" in by_name, "the hook still spawns `probe update`"
    assert by_name["update"].hidden is True, "but it must not be discoverable"


def test_the_wizard_is_the_only_discoverable_entry_point():
    import sys

    import probe.cli.main  # noqa: F401

    app = sys.modules["probe.cli.main"].app

    visible = {c.name for c in app.registered_commands if not c.hidden}
    assert "wizard" in visible
    assert "update" not in visible
    assert "setup" not in visible


def test_perform_update_records_the_attempt(isolate, monkeypatch):
    """A detached auto-update has no terminal, so a recorded attempt is the only
    way a month of silent failures becomes visible."""
    from probe.cli import autoupdate, updater, upgrading

    monkeypatch.setattr(upgrading.updater, "fetch_latest", lambda base: {})
    monkeypatch.setattr(
        upgrading.updater, "detect_install", lambda: updater.Install(updater.Method.EDITABLE)
    )
    monkeypatch.setattr(
        upgrading.updater,
        "upgrade_cli",
        lambda i, c, t: updater.CliResult(
            ran=False, ok=True, changed=False, before=c, after=c, message="skipped"
        ),
    )
    outcome = upgrading.perform_update(base_url="https://x", include_plugin=False)
    assert outcome.ok is True
    assert autoupdate.load().last_attempt is not None


def test_the_auto_update_hook_targets_the_wizard():
    """New plugin versions must not depend on the deprecated command."""
    import pathlib

    hook = pathlib.Path("plugins/probe-research/hooks/version_check.py").read_text()
    assert '"wizard", "--action", "update"' in hook


# --- the wizard is a session, not a one-shot -------------------------------


def test_there_is_an_exit_action():
    from probe.cli.actions import ACTION_COPY, Action

    assert Action.EXIT in ACTION_COPY
    assert ACTION_COPY[Action.EXIT][0] == "Exit"


def test_the_menu_comes_back_after_an_action():
    """Dropping to a shell after one task is the same "go do it yourself"
    failure as printing a command."""
    import inspect
    import sys

    import probe.cli.main  # noqa: F401

    source = inspect.getsource(sys.modules["probe.cli.main"].wizard)
    assert "while True" in source, "the menu must loop"
    assert "run_action_menu" in source, "and re-prompt after each action"


def test_a_flagged_action_does_not_loop_forever():
    """`--action manual` in a script must run once and exit."""
    import inspect
    import sys

    import probe.cli.main  # noqa: F401

    source = inspect.getsource(sys.modules["probe.cli.main"].wizard)
    assert "if not looping:" in source


def test_an_ephemeral_uvx_env_is_not_mistaken_for_a_pip_install(monkeypatch):
    """`npx probe-research` runs us from uv's CACHE, which has no pip. Falling
    through to Method.PIP made the upgrade die with "No module named pip" —
    while there was nothing to upgrade anyway, since the env is discarded."""
    from pathlib import Path

    from probe.cli import updater

    monkeypatch.setattr(
        updater,
        "_probe_pkg_dir",
        lambda: Path("/Users/x/.cache/uv/archive-v0/abc123/lib/python3.13/site-packages/probe"),
    )
    assert updater.detect_install().method is updater.Method.EPHEMERAL


def test_an_ephemeral_env_is_never_package_managed(monkeypatch):
    from probe.cli import updater, upgrading

    monkeypatch.setattr(
        upgrading.updater,
        "detect_install",
        lambda: updater.Install(updater.Method.EPHEMERAL),
    )
    monkeypatch.setattr(upgrading.updater, "fetch_latest", lambda base: {})
    monkeypatch.setattr(
        "probe.cli.bootstrap.ensure_persistent_install",
        lambda: __import__("probe.cli.bootstrap", fromlist=["x"]).BootstrapResult(
            installed=True, already_persistent=False, message="Installed `probe` (uv tool)."
        ),
    )
    outcome = upgrading.perform_update(base_url="https://x", include_plugin=False)
    blob = " ".join(outcome.lines)
    assert "temporary environment" in blob
    assert "pip" not in blob


def test_bootstrap_upgrades_an_OLD_install_not_just_a_missing_one(monkeypatch):
    """Existence is not enough: every existing user has an old `probe`, and
    checking only existence left them on it forever while the wizard ran a new
    version ephemerally."""
    from probe.cli import bootstrap

    monkeypatch.setattr(bootstrap, "_installed_binary", lambda: "/usr/local/bin/probe")
    monkeypatch.setattr(bootstrap, "_version_of", lambda b: "0.8.2")
    assert bootstrap._resolves_on_path() is False

    monkeypatch.setattr(bootstrap, "_version_of", lambda b: "99.0.0")
    assert bootstrap._resolves_on_path() is True


def test_a_failed_approval_does_not_claim_the_install_finished():
    """ "Restart Claude Code to finish" after a FAILED approval reads as success.
    The user restarts, finds the capability off, and has no idea why."""
    import inspect
    import sys

    import probe.cli.main  # noqa: F401

    source = inspect.getsource(sys.modules["probe.cli.main"]._run_wizard_action)
    # The restart notice must be reachable only when nothing is missing.
    assert "missing = [grant for grant in needs if grant not in granted]" in source
    assert "if missing:" in source
    assert "Not finished" in source
    # And it must sit in the else branch, not unconditionally after authorize().
    after = source.split("if missing:")[1]
    assert "restart_notice" in after, "the notice must be gated on success"


def test_authorize_result_is_used_not_discarded():
    """The bug was that authorize()'s return value was thrown away with `_`,
    so nothing downstream could tell success from failure."""
    import inspect
    import sys

    import probe.cli.main  # noqa: F401

    source = inspect.getsource(sys.modules["probe.cli.main"]._run_wizard_action)
    assert "granted, auth_messages = wizard.authorize(" in source
    assert "_, auth_messages = wizard.authorize(" not in source


def test_noop_wizard_rerun_still_refreshes_server_snapshot(monkeypatch):
    import sys

    import probe.cli.main  # noqa: F401
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    caps = _caps(
        tracking_plugin_installed=True,
        logged_in_as="richard@prbe.ai",
        auto_update_enabled=True,
    )
    seen = []
    monkeypatch.setattr(
        cli_main,
        "_register_local_capabilities",
        lambda current, **kwargs: seen.append((current, kwargs["settings"])) or [],
    )

    lines = cli_main._run_wizard_action(
        Action.CONFIGURE,
        caps=caps,
        base_now="https://api.test",
        yes=True,
        tracking=True,
        capture=False,
        auto_update=True,
        agent_rules=False,
        uninstall=False,
        configured=True,
    )

    assert lines == ["Already set up the way you asked. Nothing to change."]
    assert seen[0][0] is caps
    assert seen[0][1].base_url == "https://api.test"


def test_update_publishes_to_the_explicit_wizard_backend(monkeypatch):
    import sys
    from types import SimpleNamespace

    import probe.cli.main  # noqa: F401
    from probe.cli import doctor
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    updated_caps = _caps(tracking_plugin_installed=True)
    seen = []
    monkeypatch.setattr(
        "probe.cli.upgrading.perform_update",
        lambda **kwargs: SimpleNamespace(lines=["updated"], restart_needed=False),
    )
    monkeypatch.setattr(doctor, "collect", lambda: updated_caps)
    monkeypatch.setattr(
        cli_main,
        "_register_local_capabilities",
        lambda current, **kwargs: seen.append((current, kwargs["settings"])) or [],
    )

    lines = cli_main._run_wizard_action(
        Action.UPDATE,
        caps=_caps(),
        base_now="https://self-hosted.test",
        yes=True,
        tracking=None,
        capture=None,
        auto_update=None,
        agent_rules=None,
        uninstall=False,
        configured=False,
    )

    assert lines == ["updated"]
    assert seen[0][0] is updated_caps
    assert seen[0][1].base_url == "https://self-hosted.test"


def _update_action(monkeypatch, caps):
    """Run the UPDATE action against a stubbed updater, returning (lines, calls).

    `calls` records every apply_agent_rules invocation, so a test can assert the
    block was left ALONE as easily as it can assert it was rewritten.
    """
    import sys
    from types import SimpleNamespace

    import probe.cli.main  # noqa: F401
    from probe.cli import doctor, setup
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    calls = []
    monkeypatch.setattr(
        "probe.cli.upgrading.perform_update",
        lambda **kwargs: SimpleNamespace(lines=["updated"], restart_needed=False),
    )
    monkeypatch.setattr(doctor, "collect", lambda: _caps())
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])
    monkeypatch.setattr(
        setup,
        "apply_agent_rules",
        lambda want, **kwargs: calls.append((want, kwargs)) or ["refreshed"],
    )

    lines = cli_main._run_wizard_action(
        Action.UPDATE,
        caps=caps,
        base_now="https://api.test",
        yes=True,
        tracking=None,
        capture=None,
        auto_update=None,
        agent_rules=None,
        uninstall=False,
        configured=False,
    )
    return lines, calls


def test_update_refreshes_a_stale_pointer_block(monkeypatch):
    """The one copy no release can reach.

    POINTER_BODY ships inside the CLI, but the block it wrote lives in the
    researcher's home directory. Before this, UPDATE upgraded the CLI and left
    the block on whatever version it was first written at -- so a wording fix
    reached only machines that happened to re-run the CONFIGURE path.
    """
    lines, calls = _update_action(monkeypatch, _caps(agent_rules_stale=True))

    assert calls == [(True, {"stale": True})]
    assert "refreshed" in lines


def test_update_does_not_install_a_pointer_block_that_was_never_wanted(monkeypatch):
    """Declining the block must survive an upgrade.

    `agent_rules_stale` is `is_installed() and not is_current()`, so a machine
    that never took the block reads as not-stale. Refreshing on anything looser
    would write into a researcher's CLAUDE.md they deliberately kept clean.
    """
    _, calls = _update_action(monkeypatch, _caps(agent_rules_installed=False))

    assert calls == []


def test_uninstall_preserves_token_and_explicit_backend_for_final_snapshot(
    monkeypatch,
):
    import sys

    import probe.cli.main  # noqa: F401
    from probe.cli import doctor, setup
    from probe.cli.actions import Action
    from probe.sdk.config import Settings

    cli_main = sys.modules["probe.cli.main"]
    before = _caps(tracking_plugin_installed=True)
    after = _caps()
    seen = []
    monkeypatch.setattr(
        cli_main,
        "resolve",
        lambda **kwargs: Settings(
            base_url=kwargs["base_url"],
            token="preserved-api-secret",
        ),
    )
    monkeypatch.setattr(setup, "remove_everything", lambda caps: ["removed"])
    monkeypatch.setattr(doctor, "collect", lambda: after)
    monkeypatch.setattr(
        cli_main,
        "_register_local_capabilities",
        lambda current, **kwargs: seen.append((current, kwargs["settings"])) or [],
    )

    lines = cli_main._run_wizard_action(
        Action.UNINSTALL,
        caps=before,
        base_now="https://self-hosted.test",
        yes=True,
        tracking=None,
        capture=None,
        auto_update=None,
        agent_rules=None,
        uninstall=True,
        configured=True,
    )

    assert lines == ["removed"]
    assert seen[0][0] is after
    assert seen[0][1].base_url == "https://self-hosted.test"
    assert seen[0][1].token == "preserved-api-secret"


# --- the wizard is a screen, not a transcript ------------------------------


def test_escape_is_distinct_from_ctrl_c():
    """ "Go back one step" and "abandon the whole wizard" are different
    intentions. Collapsing both to None would make Escape quit."""
    from probe.cli import tui

    assert tui.BACK is not None
    import inspect

    assert "escape" in inspect.getsource(tui.bind_escape)


def test_clearing_never_fires_on_a_pipe(monkeypatch, capsys):
    """Escape codes in a CI log or a captured pipe are noise, and there is no
    screen to clear anyway."""
    from probe.cli import tui

    monkeypatch.setattr(tui, "interactive", lambda: False)
    tui.clear()
    assert capsys.readouterr().out == ""


def test_centring_collapses_on_a_narrow_terminal(monkeypatch):
    """Padding a block that already does not fit only makes it wrap."""
    from probe.cli import tui

    monkeypatch.setattr(tui, "columns", lambda: 60)
    assert tui.left_pad() == 0
    monkeypatch.setattr(tui, "columns", lambda: 120)
    assert tui.left_pad() == (120 - tui.CONTENT_WIDTH) // 2


def test_the_markers_carry_the_indent_not_the_text(monkeypatch):
    """0.13.0 padded the MESSAGE instead, which left `?` stranded at column 0
    with its text 78 columns away — it read as a rendering fault.

    questionary emits (qmark)(space)(message) and, per row,
    `" {pointer} "` or `" " * (2 + len(pointer))`. Both must land the text on
    the same column as the framed body."""
    from probe.cli import tui

    monkeypatch.setattr(tui, "columns", lambda: 120)
    pad = tui.left_pad()
    assert len(tui.qmark()) + 1 == pad + 2, "qmark must place the message at pad+2"
    assert 1 + len(tui.pointer()) + 1 == pad + 2, "pointer must match it"
    assert len(tui.body_indent()) == pad + 2, "and so must wrapped description lines"


def test_state_and_question_render_as_one_prompt(monkeypatch):
    """Printing the state separately let prompt_toolkit scroll it off the top.
    Handing it the whole block means nothing can drift out of view."""
    from probe.cli import tui

    monkeypatch.setattr(tui, "columns", lambda: 120)
    framed = tui.framed("On this device:", ["  Tracking   on"], "What now?")
    assert framed.splitlines()[0] == "On this device:", "first line rides the qmark"
    assert "What now?" in framed
    # No leading blank lines: they would strand the qmark alone at the top.
    assert not framed.startswith("\n")


def test_selected_is_green_and_the_highlight_is_not_inverted():
    """The default inverts a whole entry into a block of background colour,
    which on a three-line choice paints three solid lines."""
    from probe.cli import tui

    rules = {name: style for name, style in tui.style().style_rules}
    assert "noreverse" in rules["highlighted"]
    assert "#00af5f" in rules["selected"]


def test_checkmarks_replace_the_dots():
    from probe.cli import tui
    from questionary.prompts import common

    tui.use_checkmarks()
    assert common.INDICATOR_SELECTED == "✔"


def test_auto_update_is_asked_after_the_capabilities_not_inside_them():
    import inspect
    import sys

    import probe.cli.main  # noqa: F401

    source = inspect.getsource(sys.modules["probe.cli.main"]._run_wizard_action)
    assert "ask_auto_update" in source
    assert source.index("run_menu") < source.index("ask_auto_update")


def test_answering_the_auto_update_question_keeps_every_other_choice(monkeypatch):
    """The 0.48.0 crash. `_run_wizard_action` rebuilt the Selection by hand
    after the auto-update confirm, so adding `agent_rules` to the dataclass
    left that call one argument short:

        TypeError: Selection.__init__() missing 1 required positional
        argument: 'agent_rules'

    It shipped because the whole block sits behind `interactive()`, which is
    False under pytest, and the pty test quits at the first menu -- nothing
    executed the line AFTER the confirm was answered. This test answers it.
    """
    import sys

    import probe.cli.main  # noqa: F401
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    picked = wizard.Selection(tracking=True, capture=False, auto_update=False, agent_rules=True)
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "clear", lambda: None)
    monkeypatch.setattr(tui, "say", lambda *a, **k: None)
    monkeypatch.setattr(wizard, "interactive", lambda: True)
    monkeypatch.setattr(wizard, "run_menu", lambda defaults, *args: picked)
    monkeypatch.setattr(wizard, "ask_auto_update", lambda default, *args: True)

    # Record EVERY apply, not just agent_rules: the bug was a field-by-field
    # copy, so a future one that scrambles two fields must fail here too.
    applied: dict[str, bool] = {}
    monkeypatch.setattr(
        wizard,
        "apply_agent_rules",
        lambda want, **kw: applied.update(agent_rules=want, stale=kw.get("stale", False)) or [],
    )
    monkeypatch.setattr(
        wizard, "apply_tracking", lambda want, **kw: applied.update(tracking=want) or []
    )
    monkeypatch.setattr(
        wizard, "apply_auto_update", lambda want: applied.update(auto_update=want) or []
    )
    monkeypatch.setattr(wizard, "needs_authorization", lambda caps, selection: [])
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])
    # The post-apply verdict re-collects to verify the plugins actually landed.
    # apply_tracking is stubbed here, so nothing really installs -- hand it a
    # snapshot where the plugin IS present, or the verdict correctly reports a
    # failed install and this test stops being about Selection at all.
    from probe.cli import doctor as doctor_impl

    monkeypatch.setattr(
        doctor_impl,
        "collect",
        lambda *a, **k: _caps(tracking_plugin_installed=True, logged_in_as="x@y.z"),
    )

    cli_main._run_wizard_action(
        Action.CONFIGURE,
        caps=_caps(),
        base_now="https://api.test",
        yes=False,
        tracking=None,
        capture=None,
        auto_update=None,
        agent_rules=None,
        uninstall=False,
        configured=False,
    )

    # Every ticked box survived the auto-update answer instead of being dropped,
    # and the answer itself landed on the field it was asked about.
    assert applied == {
        "tracking": True,
        "agent_rules": True,
        "stale": False,
        "auto_update": True,
    }


def test_every_capability_is_reachable_as_a_flag():
    """The wizard's own contract: "Every capability is also a flag". Only the
    flags work headlessly, and agent_rules writes to a file OUTSIDE the repo --
    `--yes` on a fresh machine must have a way to decline it."""
    import inspect
    import sys

    import probe.cli.main  # noqa: F401
    from probe.cli.capabilities import Capability

    cli_main = sys.modules["probe.cli.main"]
    params = inspect.signature(cli_main.wizard).parameters
    for capability in Capability:
        assert capability.value in params, f"no --{capability.value.replace('_', '-')} flag"


def test_the_agent_rules_flag_actually_parses(monkeypatch):
    """A signature check proves the parameter exists, not that typer spells the
    option the way a user types it -- `--agent-rulez` would pass that one.

    Asserted on the Selection that reaches `plan()`, not on an apply: whether an
    apply fires depends on what this MACHINE already has installed, and a test
    that reads the developer's own ~/.claude is not a test.
    """
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from probe.cli import bootstrap
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli.main import app

    seen: list = []
    monkeypatch.setattr(bootstrap, "ensure_persistent_install", lambda: SimpleNamespace(message=""))
    monkeypatch.setattr(doctor_impl, "collect", lambda: _caps())
    monkeypatch.setattr(wizard, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "plan", lambda caps, selection: seen.append(selection) or [])

    for flag, expected in (("--agent-rules", True), ("--no-agent-rules", False)):
        seen.clear()
        result = CliRunner().invoke(app, ["wizard", "--action", "configure", "--yes", flag])
        assert result.exit_code == 0, result.output
        assert seen and seen[0].agent_rules is expected, f"{flag} did not parse"


def test_agent_both_runs_one_shared_authorization_then_configures_each_agent(monkeypatch):
    import sys
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from probe.cli import bootstrap
    from probe.cli import doctor as doctor_impl
    import probe.cli.main  # noqa: F401

    cli_main = sys.modules["probe.cli.main"]

    monkeypatch.setattr(bootstrap, "ensure_persistent_install", lambda: SimpleNamespace(message=""))
    monkeypatch.setattr(
        doctor_impl,
        "collect",
        lambda: _caps(agent_source=os.environ.get("PROBE_AGENT", "claude_code")),
    )
    calls: list[dict] = []

    def record(action, **kwargs):
        calls.append({"source": os.environ["PROBE_AGENT"], **kwargs})
        return []

    monkeypatch.setattr(cli_main, "_run_wizard_action", record)
    result = CliRunner().invoke(
        cli_main.app,
        ["wizard", "--agent", "both", "--action", "configure", "--yes"],
    )

    assert result.exit_code == 0, result.output
    assert [call["source"] for call in calls] == ["claude_code", "codex"]
    assert calls[0]["authorization_needs"] == ["api", "mcp", "capture"]
    assert calls[0]["capture_sources"] == ["claude_code", "codex"]
    assert calls[1]["authorization_needs"] is None


def test_agent_both_uses_one_dual_agent_uninstall_confirmation(monkeypatch):
    import sys
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from probe.cli import bootstrap
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    import probe.cli.main  # noqa: F401

    cli_main = sys.modules["probe.cli.main"]
    monkeypatch.setattr(bootstrap, "ensure_persistent_install", lambda: SimpleNamespace(message=""))
    monkeypatch.setattr(
        doctor_impl,
        "collect",
        lambda: _caps(
            agent_source=os.environ.get("PROBE_AGENT", "claude_code"),
            tracking_plugin_installed=True,
        ),
    )
    monkeypatch.setattr(wizard, "interactive", lambda: True)
    confirmations: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        wizard,
        "confirm_removal",
        lambda sources: confirmations.append(tuple(sources)) or True,
    )
    calls: list[dict] = []
    monkeypatch.setattr(
        cli_main,
        "_run_wizard_action",
        lambda action, **kwargs: calls.append(kwargs) or [],
    )

    result = CliRunner().invoke(
        cli_main.app,
        ["wizard", "--agent", "both", "--action", "uninstall"],
    )

    assert result.exit_code == 0, result.output
    assert confirmations == [("claude_code", "codex")]
    assert len(calls) == 2
    assert all(call["yes"] is True for call in calls)


def test_interactive_wizard_asks_action_then_agent_then_runs_features(monkeypatch):
    import sys
    from types import SimpleNamespace

    from typer.testing import CliRunner

    from probe.cli import bootstrap
    from probe.cli import doctor as doctor_impl
    from probe.cli import plugin_cli
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action
    import probe.cli.main  # noqa: F401

    cli_main = sys.modules["probe.cli.main"]
    monkeypatch.setattr(bootstrap, "ensure_persistent_install", lambda: SimpleNamespace(message=""))
    monkeypatch.setattr(plugin_cli, "available", lambda _source: True)
    monkeypatch.setattr(
        doctor_impl,
        "collect",
        lambda: _caps(agent_source=os.environ.get("PROBE_AGENT", "claude_code")),
    )
    monkeypatch.setattr(wizard, "interactive", lambda: True)
    monkeypatch.setattr(tui, "clear", lambda: None)
    monkeypatch.setattr(tui, "page", lambda *args, **kwargs: None)

    events: list[str] = []
    actions = iter((Action.CONFIGURE, Action.EXIT))

    def choose_action(_caps):
        events.append("action")
        return next(actions)

    def choose_agent(_defaults):
        events.append("agent")
        return ("codex",)

    def run_features(_action, **_kwargs):
        events.append("features")
        return ["done"]

    monkeypatch.setattr(wizard, "run_action_menu", choose_action)
    monkeypatch.setattr(wizard, "run_agent_menu", choose_agent)
    monkeypatch.setattr(cli_main, "_run_wizard_action", run_features)

    result = CliRunner().invoke(cli_main.app, ["wizard"])

    assert result.exit_code == 0, result.output
    assert events[:3] == ["action", "agent", "features"]


def test_action_menu_state_names_each_detected_agent():
    lines = setup.describe_state(
        {
            "claude_code": _caps(agent_source="claude_code"),
            "codex": _caps(agent_source="codex"),
        }
    )
    assert any("Claude Code" in line for line in lines)
    assert any("Codex" in line for line in lines)


def test_removing_probe_also_takes_the_block_out_of_claude_md(monkeypatch, tmp_path):
    """ "Removed." used to be false outside the repo: the plugin went, the
    credential went, and the global CLAUDE.md kept telling every agent in every
    repository to use the two skills this call had just uninstalled.

    It also pinned `Capabilities.configured` True forever, so a device that had
    removed everything could never look fresh again."""
    from types import SimpleNamespace

    from probe.cli import agent_rules

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    memory = tmp_path / "CLAUDE.md"
    memory.write_text("# mine\n", encoding="utf-8")
    agent_rules.install(memory)
    assert agent_rules.is_installed(memory)

    monkeypatch.setattr(
        setup, "turn_off", lambda mode: SimpleNamespace(summary=lambda: "capture off", warnings=())
    )
    monkeypatch.setattr(setup, "uninstall_plugin", lambda name: (True, "removed"))
    monkeypatch.setattr(setup.autoupdate, "save", lambda **kw: None)

    messages = setup.remove_everything(_caps(agent_rules_installed=True))

    assert not agent_rules.is_installed(memory), "the block outlived 'Removed.'"
    assert memory.read_text() == "# mine\n", "and the user's own text survived"
    assert any("CLAUDE.md" in m for m in messages), "removal must be reported"


def test_a_stale_block_is_something_to_do_not_nothing_to_change():
    """`plan()` skipped any capability where want == have, and a STALE block is
    installed-and-wrong, so the refresh never made it into the plan: the wizard
    said "Nothing to change" while `probe doctor` said "re-run probe wizard".
    A POINTER_VERSION bump could not reach a machine at all."""
    stale = _caps(agent_rules_installed=True, agent_rules_stale=True)
    keep = setup.Selection(tracking=False, capture=False, auto_update=False, agent_rules=True)

    steps = setup.plan(stale, keep)

    assert steps and "refresh" in steps[0], f"stale block produced no plan: {steps}"
    # And a current block is still a no-op, or every run would rewrite the file.
    current = _caps(agent_rules_installed=True, agent_rules_stale=False)
    assert setup.plan(current, keep) == []


def test_both_recommended_options_say_so():
    from probe.cli.capabilities import Capability

    assert "(recommended)" in setup.MENU_COPY[Capability.TRACKING][0]
    assert "(recommended)" in setup.AUTO_UPDATE_COPY[0]
    # Capture is NOT recommended by default: it is the data-egress one.
    assert "(recommended)" not in setup.MENU_COPY[Capability.CAPTURE][0]


# --- the interactive path, which unit tests never execute ------------------


def test_every_tui_reference_resolves():
    """The crash that shipped in 0.13.1: `tui.header` was deleted and one of
    its TWO call sites updated. Nothing caught it, because every call sits
    behind `interactive()`, which is False under pytest — so the only path real
    users take is the one with no coverage.

    This is a cheap static stand-in: every `tui.X(` in the package must name
    something `tui` actually defines.
    """
    import pathlib
    import re

    from probe.cli import tui

    src_root = pathlib.Path(tui.__file__).parent
    referenced = set()
    for path in src_root.glob("*.py"):
        referenced |= set(re.findall(r"\btui\.([a-z_]+)\s*\(", path.read_text()))

    missing = sorted(name for name in referenced if not hasattr(tui, name))
    assert not missing, f"tui has no: {missing}"


# --- reading a REAL screen back off a REAL pty ------------------------------
#
# The layout tests below used to be `inspect.getsource` greps -- "is the string
# `full_screen = True` in this function". A grep like that passes with the
# margin set to zero, set negative, or applied to the wrong edge: it certifies
# that a line of code EXISTS, not that a screen looks right. Everything from
# here down renders onto an actual pty and reads the rows back, so "the top two
# rows are blank" is a claim about what a user sees.


class _Screen:
    """The smallest VT100 that can read prompt_toolkit back.

    Not a terminal emulator so much as a transcript of one. prompt_toolkit's
    Vt100 output uses a narrow vocabulary -- SGR, erase-down, erase-line,
    relative cursor moves, CR/LF/BS and text -- so a grid plus those handlers
    reproduces exactly what would be on screen. Sequences outside the
    vocabulary are SKIPPED rather than guessed at: a wrong guess would move the
    cursor silently and every row after it would be fiction.

    Autowrap is off (prompt_toolkit emits `\\x1b[?7l`), so a long line clips at
    the right edge instead of consuming the row below it.
    """

    _CSI = __import__("re").compile(r"\x1b\[([0-9;?<>=!]*)([@-~])")

    def __init__(self, rows: int, cols: int) -> None:
        self.h, self.w = rows, cols
        self.grid = [[" "] * cols for _ in range(rows)]
        self.row = self.col = 0

    def _linefeed(self) -> None:
        if self.row + 1 < self.h:
            self.row += 1
        else:  # the bottom row scrolls, exactly as a terminal would
            self.grid.pop(0)
            self.grid.append([" "] * self.w)

    def _csi(self, params: str, final: str) -> None:
        if params[:1] in ("?", "<", ">", "=", "!"):
            return  # private modes: cursor visibility, bracketed paste, autowrap
        nums = [int(p) if p else 0 for p in params.split(";")] if params else []
        first = nums[0] if nums else 0
        step = first or 1
        blank = [" "] * self.w
        if final == "A":
            self.row = max(0, self.row - step)
        elif final == "B":
            self.row = min(self.h - 1, self.row + step)
        elif final == "C":
            self.col = min(self.w - 1, self.col + step)
        elif final == "D":
            self.col = max(0, self.col - step)
        elif final == "G":
            self.col = max(0, min(self.w - 1, step - 1))
        elif final in "Hf":
            self.row = max(0, min(self.h - 1, (nums[0] if nums else 1) - 1))
            self.col = max(0, min(self.w - 1, (nums[1] if len(nums) > 1 else 1) - 1))
        elif final == "J":
            if first == 0:
                self.grid[self.row][self.col :] = [" "] * (self.w - self.col)
                for r in range(self.row + 1, self.h):
                    self.grid[r] = list(blank)
            elif first == 1:
                self.grid[self.row][: self.col + 1] = [" "] * (self.col + 1)
                for r in range(self.row):
                    self.grid[r] = list(blank)
            else:
                self.grid = [list(blank) for _ in range(self.h)]
        elif final == "K":
            if first == 0:
                self.grid[self.row][self.col :] = [" "] * (self.w - self.col)
            elif first == 1:
                self.grid[self.row][: self.col + 1] = [" "] * (self.col + 1)
            else:
                self.grid[self.row] = list(blank)
        elif final == "X":
            end = min(self.w, self.col + step)
            self.grid[self.row][self.col : end] = [" "] * (end - self.col)

    def feed(self, data: bytes) -> _Screen:
        text = data.decode("utf8", "replace")
        i, size = 0, len(text)
        while i < size:
            ch = text[i]
            if ch == "\x1b":
                match = self._CSI.match(text, i)
                if match:
                    self._csi(match.group(1), match.group(2))
                    i = match.end()
                    continue
                nxt = text[i + 1] if i + 1 < size else ""
                if nxt == "]":  # OSC, terminated by BEL
                    end = text.find("\x07", i)
                    i = size if end < 0 else end + 1
                    continue
                i += 3 if nxt in "()#" else 2
                continue
            i += 1
            if ch == "\r":
                self.col = 0
            elif ch == "\n":
                self._linefeed()
            elif ch == "\x08":
                self.col = max(0, self.col - 1)
            elif ch == "\t":
                self.col = min(self.w - 1, (self.col // 8 + 1) * 8)
            elif ch >= " " and self.col < self.w:
                self.grid[self.row][self.col] = ch
                self.col += 1
        return self

    def lines(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.grid]

    def dump(self) -> str:
        return "\n".join(f"{i:2d}|{line}|" for i, line in enumerate(self.lines()))


def _leading_blanks(lines: list[str]) -> int:
    return next((i for i, line in enumerate(lines) if line), len(lines))


def _trailing_blanks(lines: list[str]) -> int:
    return _leading_blanks(list(reversed(lines)))


def _assert_framed(screen) -> list[str]:
    """The margin claim, in the form a margin of ZERO cannot satisfy.

    The literal edge assertions carry the weight. `>= tui.MARGIN` alone is
    vacuous at MARGIN == 0 -- "at least zero blank rows" is true of content
    welded to row 0 -- so the depth check is written second, after the two
    claims that hold for any margin worth having. The bottom one also catches
    the other half of the bug: a margin applied to the top edge only.
    """
    from probe.cli import tui

    lines = screen.lines()
    assert any(lines), f"nothing rendered at all\n{screen.dump()}"
    assert lines[0] == "", f"content is welded to the top row\n{screen.dump()}"
    assert lines[-1] == "", f"content is welded to the bottom row\n{screen.dump()}"
    assert _leading_blanks(lines) >= tui.MARGIN, screen.dump()
    assert _trailing_blanks(lines) >= tui.MARGIN, screen.dump()
    return lines


def _pty_screen(argv, *, rows, cols, keys=b"", boot=20.0, settle=6.0):
    """Run `argv` on a pty of exactly rows x cols; return the final `_Screen`.

    Sizing happens in the PARENT right after the fork. The pty starts at 0x0,
    so the ioctl is a real change and the child gets a SIGWINCH -- it redraws at
    the size we asked for even if it had already rendered once.
    """
    import fcntl
    import os
    import pty
    import select
    import struct
    import termios
    import time

    raw = bytearray()

    def drain(fd, budget, quiet=0.5):
        end, last = time.time() + budget, time.time()
        while time.time() < end:
            if select.select([fd], [], [], 0.1)[0]:
                try:
                    chunk = os.read(fd, 65536)
                except OSError:
                    return False
                if not chunk:
                    return False
                raw.extend(chunk)
                last = time.time()
                if b"\x1b[6n" in chunk:
                    # Answer the cursor-position request. Left unanswered,
                    # prompt_toolkit prints a CPR warning INTO the screen we are
                    # about to measure and every row below it is off by one.
                    try:
                        os.write(fd, b"\x1b[1;1R")
                    except OSError:
                        return False
            elif raw and time.time() - last > quiet:
                return True
        return True

    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child process
        os.execve(argv[0], argv, dict(os.environ, TERM="xterm-256color"))
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        if drain(fd, boot) and keys:
            os.write(fd, keys)
            drain(fd, settle)
    finally:
        for closing in (lambda: os.write(fd, b"\x03"), lambda: os.close(fd)):
            try:
                closing()
            except OSError:
                pass
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
    return _Screen(rows, cols).feed(bytes(raw))


#: Renders ONE prompt through the real `tui.ask` and waits. Run as `python -c`,
#: so it exercises the shipped module rather than a copy of its logic.
_RENDER_ONE = """
import sys
import questionary
from probe.cli import tui

count = int(sys.argv[1])
header = sys.argv[2] or None
message = tui.framed("Import existing work.", ["  scanned  0 folders"], "Which folder?")
choices = []
for i in range(count):
    choices.append(questionary.Separator(" "))
    choices.append(questionary.Choice(title="Option %02d" % i, value=i))
tui.ask(
    questionary.select(
        message, choices=choices, instruction="(arrows)",
        style=tui.style(), qmark=tui.qmark(), pointer=tui.pointer(),
    ),
    height=tui.content_height(message, choices),
    header=header,
)
"""

_HEADER = "Folder: /Users/example/research"


def _prompt_screen(*, rows, cols, choices, header="", downs=0):
    import sys

    return _pty_screen(
        [sys.executable, "-c", _RENDER_ONE, str(choices), header],
        rows=rows,
        cols=cols,
        keys=b"\x1b[B" * downs,
    )


def test_the_margin_is_not_zero():
    """Every margin assertion in this file is written as ">= tui.MARGIN", which
    a margin of zero would satisfy vacuously -- blank rows would be asserted
    zero rows deep and every one of them would pass against content welded to
    row 0. This is the one place that says the margin must EXIST. Delete it and
    the whole section quietly stops being a test."""
    from probe.cli import tui

    assert tui.MARGIN >= 1, "a zero margin is the bug this whole section exists for"


def _probe_binary():
    """THIS interpreter's probe, not whatever is on PATH — a stale global
    install would make the test report on code that is not under test."""
    import os
    import shutil
    import sys

    probe = str(pathlib.Path(sys.executable).parent / "probe")
    if not os.path.exists(probe):
        probe = shutil.which("probe") or ""
    if not probe or not os.path.exists(probe):
        pytest.skip("no probe binary on this machine")
    return probe


def test_the_interactive_wizard_starts_without_crashing():
    """Actually run it on a pty, and walk a step INTO it.

    Every unit test stubs `interactive()` to False, so the branch that renders
    a menu is otherwise never executed. This used to stop at the first frame
    and only grep for "Traceback"; it now drives the pointer down and back up
    (harmless on both the action menu and the capability picker -- neither
    Enter nor Space is ever sent, so nothing installs) and measures the screen
    that comes back.

    The action menu is now always the first interactive screen, on both fresh
    and configured devices. It goes through `tui.ask`, so the margin claim
    holds against the real entry point rather than a rehearsal.
    """
    screen = _pty_screen([_probe_binary(), "wizard"], rows=24, cols=80, keys=b"\x1b[B\x1b[A")
    text = "\n".join(screen.lines())

    assert "Traceback" not in text, screen.dump()
    assert "AttributeError" not in text, screen.dump()
    # The margin, on the real binary rather than a rehearsal of it.
    _assert_framed(screen)


def test_a_prompt_keeps_its_margins_on_a_narrow_short_terminal():
    """80x24 is the floor every terminal clears, and the size at which a
    content block and the screen are closest to the same height -- which is
    exactly where a missing margin shows up first."""
    from probe.cli import tui

    lines = _assert_framed(_prompt_screen(rows=24, cols=80, choices=4))

    # Still CENTRED, not merely lifted off the top edge: a block this short
    # leaves far more than the margin at both ends, and the two ends match.
    assert _leading_blanks(lines) > tui.MARGIN
    assert abs(_leading_blanks(lines) - _trailing_blanks(lines)) <= 1, "\n".join(lines)


def test_content_taller_than_the_screen_scrolls_inside_the_frame():
    """The case the old layout could not survive, and the one that gives every
    margin assertion here its teeth.

    30 choices is more than twice a 24-row screen. Before the frame existed the
    block was drawn from row 0 and simply ran out of screen: the top row
    carried content that looked like it had scrolled in from somewhere, the
    bottom options were unreachable, and no amount of centring arithmetic
    helped because the spacer was deliberately zero at this size.

    Now it is a bounded band -- so the edges stay clear even here, and the
    pointed row stays visible because the choice window scrolls to its own
    cursor.
    """
    screen = _prompt_screen(rows=24, cols=80, choices=30, downs=12)
    lines = _assert_framed(screen)

    # Twelve rows down a 30-long list: the pointer is far below the fold and the
    # list has scrolled to bring it back, rather than the fold winning.
    assert any("» Option 12" in line for line in lines), screen.dump()
    assert not any("Option 00" in line for line in lines), (
        "the list never scrolled -- it was clipped instead\n" + screen.dump()
    )


def test_a_pinned_header_survives_scrolling_to_the_bottom_of_a_long_list():
    """What the folder picker needs: the path you are browsing has to stay
    readable when you are 12 rows down a list that does not fit. A header
    printed as part of the message scrolls away with everything else; this one
    is its own band above the body, so it cannot."""
    from probe.cli import tui

    screen = _prompt_screen(rows=24, cols=80, choices=30, header=_HEADER, downs=12)
    lines = _assert_framed(screen)

    assert any(_HEADER in line for line in lines), screen.dump()
    header_row = next(i for i, line in enumerate(lines) if _HEADER in line)
    question_row = next(i for i, line in enumerate(lines) if "Which folder?" in line)
    # Pinned ABOVE the question, and sitting on the first row the margin allows
    # rather than on row 0 -- which is where a header with no margin would land.
    assert header_row < question_row, screen.dump()
    assert header_row == tui.MARGIN, screen.dump()
    # It survived the scroll: the list moved under it, the header did not.
    assert any("» Option 12" in line for line in lines), screen.dump()


def test_the_margin_holds_on_a_terminal_too_short_to_centre_anything():
    """Twelve rows with a header and 30 choices: the content cannot fit however
    it is arranged, so there is nothing to centre and the margin is the only
    thing keeping the frame off the edges. It still holds -- twelve rows can
    afford four of them -- and the pointer is still reachable."""
    screen = _prompt_screen(rows=12, cols=80, choices=30, header=_HEADER, downs=8)
    lines = _assert_framed(screen)

    assert len(lines) == 12
    assert any(_HEADER in line for line in lines), screen.dump()
    assert any("» Option 08" in line for line in lines), screen.dump()


import pathlib  # noqa: E402  (used by the pty test above)


def test_content_height_is_counted_not_asked():
    """`Container.preferred_height()` needs a running event loop and returns a
    placeholder without one — which silently produced a 22-row spacer for 44
    rows of content and pushed the BOTTOM off instead."""
    import questionary

    from probe.cli import tui

    choices = [
        questionary.Separator(" "),
        questionary.Choice(title="one\n  detail", value="a"),
        questionary.Separator(" "),
        questionary.Choice(title="two\n  detail", value="b"),
    ]
    # 3 message lines + 2 separators + 2 two-line choices + 1 instruction
    assert tui.content_height("a\nb\nc", choices) == 3 + 1 + 2 + 1 + 2 + 1


def test_a_page_taller_than_the_screen_still_gets_no_spacer(monkeypatch):
    """Centring something that does not fit only chooses which end to
    amputate — and the top is the end with the verdict on it. The margin does
    NOT override that: whitespace gives way before content does.

    This used to define its own `spacer_for()` and assert against that, which
    tested the test. It calls the shipped function now.
    """
    from probe.cli import tui

    def at(screen):
        monkeypatch.setattr(tui, "rows", lambda: screen)

    for screen, height, expected in (
        (20, 23, 0),  # taller than the screen: no spacer at all
        (23, 23, 0),  # exactly the screen: likewise
        (24, 23, 1),  # one row of slack, and the margin cannot have all of it
    ):
        at(screen)
        assert tui.top_spacer(height) == expected, (screen, height)

    # A block that fits keeps the margin AND the centring.
    at(60)
    assert tui.top_spacer(23) >= tui.MARGIN

    # It never pushes the bottom off, at any size.
    for screen in (10, 24, 30, 45, 60, 120):
        at(screen)
        for height in (1, 5, 23, 40, 200):
            assert tui.top_spacer(height) + height <= max(screen, height)


def test_a_page_that_fits_is_centred_inside_the_margin(monkeypatch):
    """The margin is on BOTH edges, so the centring works against a frame two
    margins shorter than the screen — not against the raw screen."""
    from probe.cli import tui

    monkeypatch.setattr(tui, "rows", lambda: 40)
    top = tui.top_spacer(10)

    assert top >= tui.MARGIN
    assert 40 - top - 10 >= tui.MARGIN, "no room left at the bottom"
    # Centred: the two ends differ by at most the odd row.
    assert abs(top - (40 - top - 10)) <= 1


def test_the_prompt_goes_full_screen():
    """Inline rendering grows down from the cursor, so anything taller than the
    room below it scrolls — which is what kept eating the state block, and is
    worse in a block terminal like Warp.

    Asserted on the OBJECT, not on the source text. The grep this replaces
    ("full_screen = True" appears in the function) passed whether or not the
    line ever ran — it sits inside a bare `except Exception: pass`.
    """
    import questionary

    from probe.cli import tui

    question = questionary.select("pick", choices=["a", "b"], style=tui.style())
    assert question.application.full_screen is False, "questionary starts inline"

    tui.center_vertically(question, height=4)

    assert question.application.full_screen is True


def test_the_layout_is_four_bands_and_a_header_only_when_asked():
    """The frame is what makes a margin possible at all. Reached through the
    live layout, so a questionary reshuffle that our guard swallows shows up
    here as a missing band rather than as a silently uncentred prompt."""
    import questionary

    from probe.cli import tui

    plain = questionary.select("pick", choices=["a", "b"], style=tui.style())
    tui.center_vertically(plain, height=4)
    assert len(plain.application.layout.container.children) == 3, "top, body, bottom"

    pinned = questionary.select("pick", choices=["a", "b"], style=tui.style())
    tui.center_vertically(pinned, height=4, header="Folder: /tmp")
    assert len(pinned.application.layout.container.children) == 4, "+ the header band"


def test_the_frame_never_returns_a_negative_or_overflowing_band():
    """A negative Dimension is not a cosmetic bug — prompt_toolkit raises on
    it, and this module's whole posture is that layout must never take the
    prompt down with it. Swept rather than sampled: the interesting sizes are
    the degenerate ones nobody renders at until a user has a 3-row pane open.
    """
    from probe.cli import tui

    for available in range(1, 60):
        for height in (None, 1, 2, 13, 40, 500):
            for header in (0, 1, 3, 90):
                frame = tui.frame_rows(available, height, header)
                assert min(frame) >= 0, (available, height, header, frame)
                assert sum(frame) == max(1, available), (available, height, header, frame)


def test_the_frame_spends_its_last_rows_on_content_not_whitespace():
    """Two claims that pull against each other, and the order between them:
    the margin exists, but it is what shrinks when the terminal cannot afford
    it. A screen too short for both keeps the content."""
    from probe.cli import tui

    roomy = tui.frame_rows(40, 12, 0)
    assert roomy.top >= tui.MARGIN and roomy.bottom >= tui.MARGIN
    assert roomy.body == 12, "content that fits is never squeezed"

    # Taller than the frame: the body is capped so it SCROLLS rather than
    # overflowing, and both margins survive.
    tall = tui.frame_rows(24, 500, 0)
    assert tall.body == 24 - 2 * tui.MARGIN
    assert tall.top == tui.MARGIN and tall.bottom == tui.MARGIN

    # A header costs body rows, never margin rows.
    headed = tui.frame_rows(24, 500, 3)
    assert headed.header == 3
    assert headed.body == 24 - 2 * tui.MARGIN - 3
    assert headed.top == tui.MARGIN and headed.bottom == tui.MARGIN

    # Too short to afford the margin: it gives way, the content does not.
    cramped = tui.frame_rows(3, 500, 0)
    assert cramped.body >= 1
    assert sum(cramped) == 3


def test_every_prompt_in_the_wizard_is_a_centred_page():
    """One step used to print its detail and render a bare confirm underneath,
    so prompt_toolkit took a screen that already had two lines on it and the
    whole step sat welded to the top while every other step was centred.

    A prompt cannot be centred without a counted height, and it cannot own its
    own layout unless the whole block is handed to it as the message.
    """
    import inspect

    from probe.cli import setup as wizard

    for prompt in (
        wizard.run_menu,
        wizard.run_action_menu,
        wizard.ask_auto_update,
        wizard.confirm_removal,
    ):
        source = inspect.getsource(prompt)
        assert "tui.framed(" in source, f"{prompt.__name__} must hand the block to the prompt"
        assert "height=tui.content_height(" in source, f"{prompt.__name__} renders uncentred"


def test_the_wizard_never_drops_to_a_bare_prompt():
    """`typer.confirm` prints at column 0 with no page around it — and the one
    that guarded uninstall was the single destructive action in the product."""
    import inspect
    import sys

    import probe.cli.main  # noqa: F401

    assert "typer.confirm" not in inspect.getsource(
        sys.modules["probe.cli.main"]._run_wizard_action
    )


def test_a_confirm_does_not_count_an_instruction_row():
    """Only a list prompt draws one. Counting it for a confirm sits the page
    half a row high."""
    from probe.cli import tui

    assert tui.content_height("a\nb\nc") == 3


def test_output_pages_are_centred_like_the_prompts(monkeypatch, capsys):
    """Results used to print from column 0 at the top of the screen while every
    prompt sat centred, so finishing an action visibly threw you out of the
    wizard and back into a bare terminal."""
    from probe.cli import tui

    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "clear", lambda: None)
    monkeypatch.setattr(tui, "rows", lambda: 40)

    tui.page(["one", "two"])
    lines = capsys.readouterr().out.split("\n")
    pad = " " * ((120 - tui.CONTENT_WIDTH) // 2)
    top = lines.index(pad + "one")
    assert top == (40 - 2) // 2, "vertical centring"
    assert lines[top + 1] == pad + "two"
    # `page()` has no bottom band to draw -- `clear()` already blanked the
    # screen, so the margin is the rows it declines to fill. It still has to
    # LEAVE them: a block that ends on the last row reads as a truncated one.
    assert top >= tui.MARGIN, "no top margin"
    assert 40 - (top + 2) >= tui.MARGIN, "nothing left at the bottom"


def test_a_page_wraps_inside_its_block(monkeypatch, capsys):
    """A line that overflows the block wraps back to column 0 in the terminal,
    which breaks the centred column the whole page is built on."""
    from probe.cli import tui

    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "clear", lambda: None)
    monkeypatch.setattr(tui, "rows", lambda: 40)

    tui.page(["  - " + "word " * 40])
    body = [ln.strip() for ln in capsys.readouterr().out.split("\n") if ln.strip()]
    assert len(body) > 1, "a long line must be broken up"
    assert all(len(ln) <= tui.CONTENT_WIDTH for ln in body)
    # The bullet hangs so the wrapped remainder does not read as a new item.
    assert body[0].startswith("- ")
    assert not body[1].startswith("- ")


def test_piped_output_stays_flush_left(monkeypatch, capsys):
    """An indent in a log is noise every grep has to strip, and CI is the one
    consumer that never sees the layout."""
    from probe.cli import tui

    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr(tui, "interactive", lambda: False)

    tui.say("hello")
    tui.page(["one", "two"])
    assert capsys.readouterr().out == "hello\none\ntwo\n"


# --- the plugin half of an auto-update -------------------------------------


def _plugin_result(**kw):
    from probe.cli import updater

    base = dict(
        attempted=True, confirmed=True, changed=False, before=None, after="0.8.0", message="ok"
    )
    return updater.PluginResult(**{**base, **kw})


def _stub_cli_upgrade(monkeypatch, upgrading, updater):
    monkeypatch.setattr(upgrading.updater, "fetch_latest", lambda base: {})
    monkeypatch.setattr(
        upgrading.updater, "detect_install", lambda: updater.Install(updater.Method.UV_TOOL)
    )
    monkeypatch.setattr(
        upgrading.updater,
        "upgrade_cli",
        lambda i, c, t: updater.CliResult(
            ran=True, ok=True, changed=False, before=c, after=c, message="already at the latest"
        ),
    )


def test_a_failed_plugin_update_is_recorded(isolate, monkeypatch):
    """It used to live only in the printed lines, which a DETACHED run sends to
    /dev/null — so a plugin that had silently stopped updating looked exactly
    like one that worked. That is the failure this record exists to prevent."""
    from probe.cli import autoupdate, updater, upgrading

    _stub_cli_upgrade(monkeypatch, upgrading, updater)
    monkeypatch.setattr(
        upgrading.updater,
        "update_plugin",
        lambda target: _plugin_result(
            confirmed=False, after=None, message="`claude plugin update` did not complete"
        ),
    )

    outcome = upgrading.perform_update(base_url="https://x", include_plugin=True)
    attempt = autoupdate.load().last_attempt

    assert attempt.plugin_ok is False
    assert "did not complete" in attempt.plugin_detail
    assert attempt.succeeded is False
    assert "FAILED" in attempt.describe()
    assert "did not complete" in attempt.describe()
    # And the exit code means "the update worked", not "the CLI half worked".
    assert outcome.ok is False


def test_no_claude_on_path_is_not_a_plugin_failure(isolate, monkeypatch):
    """A CLI-only user has no `claude`. Recording that as a failure every
    session trains everyone to ignore the one line meant to mean something."""
    from probe.cli import autoupdate, updater, upgrading

    _stub_cli_upgrade(monkeypatch, upgrading, updater)
    monkeypatch.setattr(
        upgrading.updater,
        "update_plugin",
        lambda target: _plugin_result(
            attempted=False,
            confirmed=False,
            after=None,
            message="`claude` not found on PATH (skipping plugin update)",
        ),
    )

    outcome = upgrading.perform_update(base_url="https://x", include_plugin=True)
    attempt = autoupdate.load().last_attempt

    assert attempt.plugin_ok is True
    assert attempt.succeeded is True
    assert outcome.ok is True
    # Still SAID, though — silence about a skipped half is its own lie.
    assert "not found on PATH" in attempt.describe()


def test_a_successful_plugin_update_names_the_version(isolate, monkeypatch):
    from probe.cli import autoupdate, updater, upgrading

    _stub_cli_upgrade(monkeypatch, upgrading, updater)
    monkeypatch.setattr(
        upgrading.updater, "update_plugin", lambda target: _plugin_result(changed=True)
    )

    upgrading.perform_update(base_url="https://x", include_plugin=True)
    described = autoupdate.load().last_attempt.describe()

    assert described.startswith("success")
    assert "plugin 0.8.0" in described


def test_an_old_record_without_plugin_fields_still_reads_as_success(isolate):
    """Records written before the plugin half was tracked must not start
    reporting a failure the moment the CLI is upgraded."""
    from probe.cli import autoupdate

    autoupdate.state_dir().mkdir(parents=True, exist_ok=True)
    autoupdate.state_path().write_text(
        json.dumps(
            {
                "enabled": True,
                "channel": "latest",
                "last_attempt": {"at": 1_700_000_000, "ok": True, "to_version": "0.14.1"},
            }
        )
    )
    attempt = autoupdate.load().last_attempt
    assert attempt.succeeded is True
    assert attempt.describe().startswith("success -> CLI 0.14.1")


def test_the_hook_no_longer_passes_a_channel(isolate):
    """One channel, so the flag is gone from the spawn — but newer CLIs must
    still ACCEPT it, because a plugin updates on the USER's schedule and older
    copies of the hook are still out there passing it."""
    import pathlib

    hook = pathlib.Path("plugins/probe-research/hooks/version_check.py").read_text()
    argv = hook.split("subprocess.Popen(")[1].split("stdin=")[0]
    assert '"--action", "update"' in argv, "wrong call site"
    assert "--channel" not in argv

    from typer.testing import CliRunner

    from probe.cli.main import app

    result = CliRunner().invoke(app, ["wizard", "--action", "diagnose", "--channel", "stable"])
    assert result.exit_code == 0, result.output + repr(result.exception)


# --- the 2026-08-04 install phase ------------------------------------------
#
# A `npx probe-research` run sat on "This run will:" for minutes, then reported
# two failed plugin installs and finished with "Restart Claude Code to finish".
# Four separate defects, each with its own test below:
#
#   1. the phase buffered every message until all four steps had returned
#   2. the marketplace refresh ran per-plugin and both results were discarded
#   3. a failed install never reached the verdict
#   4. the install was attempted at all -- both plugins were already present


def test_the_install_phase_streams_instead_of_buffering():
    """It used to collect every message into a list and print the lot after all
    four apply steps finished. Since a step shells out to `claude`, that showed
    a blank screen for as long as the subprocesses took -- which is the entire
    reported symptom."""
    import inspect
    import sys

    import probe.cli.main  # noqa: F401

    source = inspect.getsource(sys.modules["probe.cli.main"]._run_wizard_action)
    # The tell-tale of the old shape: one list, extended by every apply, drained
    # at the end.
    assert "messages: list[str] = []" not in source
    assert "for message in messages:" not in source
    assert "tui.Progress(" in source


def test_a_present_plugin_is_not_reinstalled(monkeypatch):
    """THE root cause. The install was gated on `caps.tracking_on`, which is
    "plugin installed AND logged in", and on `capture_on`, which does not look
    at the plugin at all. A machine with both plugins present but no credential
    yet therefore read as "both off" and reinstalled both -- and those installs
    are the ones that failed.

    Measured: `claude plugin install` on an already-installed plugin returns
    "already installed" and does NOT upgrade it, so the work had no upside.
    """
    import sys

    import probe.cli.main  # noqa: F401
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    installed: list[str] = []
    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "install_plugin", lambda name, **kw: installed.append(name))
    monkeypatch.setattr(wizard, "needs_authorization", lambda caps, selection: [])
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])

    # Exactly the reported machine: both plugins on disk, no credential.
    present = _caps(
        tracking_plugin_installed=True,
        capture_plugin_installed=True,
        logged_in_as=None,
    )
    monkeypatch.setattr(doctor_impl, "collect", lambda *a, **k: present)

    cli_main._run_wizard_action(
        Action.CONFIGURE,
        caps=present,
        base_now="https://api.test",
        yes=True,
        tracking=True,
        capture=True,
        auto_update=None,
        agent_rules=False,
        uninstall=False,
        configured=True,
    )

    assert installed == [], "already-present plugins must not be reinstalled"


def test_the_plan_says_sign_in_when_only_the_credential_is_missing():
    """ "enable CLI + MCP" on a machine whose plugin is already installed
    describes work the run will not do. It is also the COMMON first-run case,
    because capture_on is credential-only."""
    from probe.cli import setup as wizard
    from probe.cli.capabilities import Capability

    caps = _caps(tracking_plugin_installed=True, capture_plugin_installed=True)
    selection = wizard.Selection(tracking=True, capture=True, auto_update=False, agent_rules=False)
    steps = wizard.plan(caps, selection)

    assert any("sign in" in step for step in steps), steps
    assert not any(step.startswith("enable CLI + MCP") for step in steps), steps
    # And the sign-in wording exists for both capabilities that have a plugin.
    assert set(wizard.SIGN_IN_LABELS) == {Capability.TRACKING, Capability.CAPTURE}


def test_a_successful_install_does_no_marketplace_work(monkeypatch):
    """install_plugin used to run `marketplace add` + `update` itself, so a run
    installing both plugins refreshed the same marketplace twice -- and threw
    both results away, which is why a failed refresh surfaced as Claude's
    downstream "not found in marketplace".

    The refresh is now the caller's job, once per run. Only the RETRY path may
    refresh again, so a clean install must touch the marketplace zero times.
    """
    from probe.cli import claude_cli
    from probe.cli import setup as wizard

    calls: list[list[str]] = []
    monkeypatch.setattr(
        claude_cli,
        "run",
        lambda args, *, timeout: calls.append(args) or claude_cli.Result(ok=True),
    )

    assert wizard.install_plugin("probe-research").ok is True
    assert calls == [["plugin", "install", "probe-research@research-os-agent"]]
    assert not any("marketplace" in a for call in calls for a in call)


def test_refresh_marketplace_reports_the_update_result(monkeypatch):
    """Both results used to be discarded. `update` is the one whose success
    decides whether the catalog we install from is current, so it is the one
    that comes back."""
    from probe.cli import claude_cli
    from probe.cli import setup as wizard

    def fake_run(args, *, timeout):
        if "update" in args:
            return claude_cli.Result(ok=False, detail="network unreachable")
        return claude_cli.Result(ok=True)

    monkeypatch.setattr(claude_cli, "run", fake_run)

    result = wizard.refresh_marketplace()
    assert result.ok is False
    assert "network unreachable" in result.detail


def test_a_failed_install_retries_once_on_any_failure(monkeypatch):
    """Deliberately NOT gated on matching Claude's error prose: a retry that
    fires only on "not found in marketplace" stops firing the day Anthropic
    rewords it, silently, with every test still green."""
    from probe.cli import claude_cli
    from probe.cli import setup as wizard

    attempts: list[list[str]] = []
    refreshed: list[int] = []

    def fake_run(args, *, timeout):
        attempts.append(args)
        # Fail the first install, succeed the second.
        if args[:2] == ["plugin", "install"]:
            return claude_cli.Result(ok=len(refreshed) > 0, detail="whatever went wrong")
        return claude_cli.Result(ok=True)

    monkeypatch.setattr(claude_cli, "run", fake_run)
    monkeypatch.setattr(wizard, "refresh_marketplace", lambda **_kwargs: refreshed.append(1))

    budget = [1]

    def may_retry():
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        return True

    result = wizard.install_plugin("probe-research", on_retry=may_retry)

    assert result.ok is True
    assert len(refreshed) == 1, "the retry must refresh first"
    installs = [a for a in attempts if a[:2] == ["plugin", "install"]]
    assert len(installs) == 2, "exactly one retry"


def test_the_retry_budget_is_per_run_not_per_plugin(monkeypatch):
    """Two plugins failing must not cost two refreshes and two reinstalls: that
    is how a fix for an 18-minute worst case becomes a 30-minute one."""
    from probe.cli import claude_cli
    from probe.cli import setup as wizard

    refreshed: list[int] = []
    monkeypatch.setattr(
        claude_cli, "run", lambda args, *, timeout: claude_cli.Result(ok=False, detail="no")
    )
    monkeypatch.setattr(wizard, "refresh_marketplace", lambda **_kwargs: refreshed.append(1))

    budget = [1]

    def may_retry():
        if budget[0] <= 0:
            return False
        budget[0] -= 1
        return True

    wizard.install_plugin("probe-research", on_retry=may_retry)
    wizard.install_plugin("probe-research-tap", on_retry=may_retry)

    assert len(refreshed) == 1, "the second plugin must find the budget spent"


def test_a_verified_absent_plugin_fails_the_run(monkeypatch):
    """ "Restart Claude Code to finish" after a failed install reads as success:
    the user restarts, finds Probe absent, and has no idea why."""
    import sys

    import typer

    import probe.cli.main  # noqa: F401
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "apply_tracking", lambda want, **kw: [])
    monkeypatch.setattr(wizard, "needs_authorization", lambda caps, selection: [])
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])
    # Verified: we asked, and the plugin is not there.
    monkeypatch.setattr(
        doctor_impl,
        "collect",
        lambda *a, **k: _caps(tracking_plugin_installed=False, plugins_verified=True),
    )

    with pytest.raises(typer.Exit) as excinfo:
        cli_main._run_wizard_action(
            Action.CONFIGURE,
            caps=_caps(),
            base_now="https://api.test",
            yes=True,
            tracking=True,
            capture=False,
            auto_update=None,
            agent_rules=False,
            uninstall=False,
            configured=True,
        )
    assert excinfo.value.exit_code == 1


def test_an_unverifiable_plugin_does_not_fail_the_run(monkeypatch):
    """`claude` absent is normal on a GPU pod. Swapping a silent failure for a
    false alarm on machines that are fine would be the worse bug -- it trains
    people to ignore the message."""
    import sys

    import probe.cli.main  # noqa: F401
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "apply_tracking", lambda want, **kw: [])
    monkeypatch.setattr(wizard, "needs_authorization", lambda caps, selection: [])
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])
    # We could NOT ask. Absent is an unanswered question, not a finding.
    monkeypatch.setattr(
        doctor_impl,
        "collect",
        lambda *a, **k: _caps(tracking_plugin_installed=False, plugins_verified=False),
    )

    # Must NOT raise.
    cli_main._run_wizard_action(
        Action.CONFIGURE,
        caps=_caps(),
        base_now="https://api.test",
        yes=True,
        tracking=True,
        capture=False,
        auto_update=None,
        agent_rules=False,
        uninstall=False,
        configured=True,
    )


def test_plugin_state_will_not_report_absence_it_did_not_verify():
    from probe.cli.capabilities import PluginState

    verified = PluginState(names=frozenset({"probe-research"}), verified=True)
    assert verified.missing(["probe-research", "probe-research-tap"]) == ["probe-research-tap"]

    unknown = PluginState(verified=False)
    assert unknown.missing(["probe-research"]) == [], "an unanswered question is not a no"


def test_installed_plugins_is_unverified_without_claude(monkeypatch):
    from probe.cli import capabilities

    monkeypatch.setattr(
        capabilities.plugin_cli,
        "list_plugins",
        lambda _source: capabilities.plugin_cli.claude_cli.Result(ok=False, reachable=False),
    )
    state = capabilities.installed_plugins()
    assert state.verified is False
    assert len(state) == 0


# --- the progress screen ----------------------------------------------------


def test_progress_redraws_one_centred_screen_on_a_terminal(monkeypatch, capsys):
    """The install phase must look like the menu that led into it: same column,
    wrapped inside the block, vertically centred. It used to print through
    say(), which left-pads but does NOT wrap, so a 200-character error from
    `claude` ran off the block and wrapped at the terminal's right edge back to
    column 0."""
    from probe.cli import tui

    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "clear", lambda: None)
    monkeypatch.setattr(tui, "rows", lambda: 40)

    progress = tui.Progress("This run will:", ["enable A", "enable B"])
    progress.start(0)
    progress.finish(0, ok=True)
    progress.note("could not install probe-research: " + "x" * 200)

    body = [ln.strip() for ln in capsys.readouterr().out.split("\n") if ln.strip()]
    assert all(len(ln) <= tui.CONTENT_WIDTH for ln in body), "must wrap inside the block"


def test_progress_marks_a_failed_step_distinctly(monkeypatch, capsys):
    from probe.cli import tui

    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "clear", lambda: None)
    monkeypatch.setattr(tui, "rows", lambda: 40)

    progress = tui.Progress("This run will:", ["enable A", "enable B"])
    progress.finish(0, ok=True)
    progress.finish(1, ok=False)
    block = "\n".join(progress.block())

    assert "✔ enable A" in block
    assert "✗ enable B" in block, "a failure must not read as a tick"


def test_the_bar_tracks_completed_steps(monkeypatch):
    from probe.cli import tui

    monkeypatch.setattr(tui, "interactive", lambda: False)
    progress = tui.Progress("t", ["a", "b", "c", "d"])
    assert progress.bar().endswith("0/4")
    progress.finish(0)
    progress.finish(1)
    assert progress.bar().endswith("2/4")
    assert progress.bar().count("#") == len(progress.bar().split("]")[0]) // 2


def test_progress_appends_one_line_per_step_when_piped(monkeypatch, capsys):
    """A screen and a log want opposite things. page() skips the clear when it
    is not interactive, so redrawing into a pipe prints the whole growing block
    once per step -- log spam, not progress. And `probe wizard --yes` is the
    one path where a human is NOT watching, so its log is the only evidence of
    which step a wedged CI job died on."""
    from probe.cli import tui

    monkeypatch.setattr(tui, "interactive", lambda: False)

    progress = tui.Progress("This run will:", ["enable A", "enable B"])
    progress.start(0)
    progress.finish(0, ok=True)
    progress.start(1)
    progress.finish(1, ok=False)

    out = capsys.readouterr().out
    lines = [ln for ln in out.split("\n") if ln.strip()]
    assert lines == ["[1/2] enable A ... ok", "[2/2] enable B ... FAILED"]
    assert "This run will:" not in out, "the block must not be reprinted per step"


def test_results_persist_across_redraws(monkeypatch, capsys):
    """clear() emits \\033[3J, which drops the scrollback. Anything not
    re-drawn is gone for good, so a failure message from step 1 must still be
    on screen after step 4 redraws."""
    from probe.cli import tui

    monkeypatch.setenv("COLUMNS", "120")
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "clear", lambda: None)
    monkeypatch.setattr(tui, "rows", lambda: 40)

    progress = tui.Progress("t", ["a", "b"])
    progress.note("could not install probe-research")
    progress.finish(1, ok=True)

    assert "could not install probe-research" in "\n".join(progress.block())


def test_the_progress_bar_tracks_real_work_not_plan_lines(monkeypatch, capsys):
    """Caught by the pty smoke test, not by any unit test.

    The bar was built from `plan()`'s steps and indexed positionally by the
    apply loop. Those stopped being 1:1 the moment plan() learned to say "sign
    in" for a machine whose plugin is already installed: a sign-in-only run
    showed "0/2" and never moved, because the actual work happened in the
    authorization phase, which no step covered.

    The browser approval is the LONGEST wait in the run -- it blocks on a human
    -- so it has to be one of the tracked steps.
    """
    import sys

    import probe.cli.main  # noqa: F401
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    rendered: list[list[str]] = []

    class Recorder(tui.Progress):
        def render(self):
            rendered.append(list(self.block()))
            super().render()

    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(tui, "Progress", Recorder)
    monkeypatch.setattr(wizard, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "needs_authorization", lambda caps, selection: ["api", "mcp"])
    monkeypatch.setattr(wizard, "authorize", lambda *a, **k: ({"api": {}, "mcp": {}}, []))
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])

    # Plugin already present, credential missing: nothing to install, so the
    # ONLY work is the sign-in.
    present = _caps(tracking_plugin_installed=True, plugins_verified=True)
    monkeypatch.setattr(doctor_impl, "collect", lambda *a, **k: present)

    cli_main._run_wizard_action(
        Action.CONFIGURE,
        caps=present,
        base_now="https://api.test",
        yes=True,
        tracking=True,
        capture=False,
        auto_update=None,
        agent_rules=False,
        uninstall=False,
        configured=True,
    )

    final = rendered[-1]
    assert any("sign in" in line for line in final), final
    bar = next(line for line in final if line.startswith("["))
    assert bar.endswith("1/1"), f"the sign-in must count as work: {bar}"
    assert not bar.endswith("0/1"), "the bar must not sit at zero through the whole run"


def test_ticking_capture_clears_the_killswitch_even_when_the_plugin_is_present(monkeypatch):
    """Regression introduced while fixing the reinstall bug, caught in review.

    Gating the capture step on "plugin absent" skipped `apply_capture(True)`
    entirely on a machine whose tap plugin was installed but killswitched. The
    `.disabled` file survived, capture stayed off, and the wizard reported
    success -- the inverse of the failure capture.py calls the worst available
    bug ("we told you it was off and it wasn't").
    """
    import sys

    import probe.cli.main  # noqa: F401
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    cleared: list[int] = []
    installed: list[str] = []
    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "clear_killswitch", lambda: cleared.append(1))
    monkeypatch.setattr(wizard, "install_plugin", lambda name, **kw: installed.append(name))
    monkeypatch.setattr(wizard, "needs_authorization", lambda caps, selection: [])
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])

    # Plugin installed, credential present, but the killswitch is ON -- so
    # capture_on is False and the user ticking it must actually turn it on.
    killswitched = _caps(
        capture_plugin_installed=True,
        capture_killswitched=True,
        capture_token_sources=(TokenSource.PAIRED_FILE,),
        plugins_verified=True,
    )
    monkeypatch.setattr(doctor_impl, "collect", lambda *a, **k: killswitched)

    cli_main._run_wizard_action(
        Action.CONFIGURE,
        caps=killswitched,
        base_now="https://api.test",
        yes=True,
        tracking=False,
        capture=True,
        auto_update=None,
        agent_rules=False,
        uninstall=False,
        configured=True,
    )

    assert cleared, "the killswitch must be cleared when capture is ticked on"
    assert installed == [], "a present plugin must still not be reinstalled"


def test_a_valid_pairing_does_not_hide_a_manually_removed_capture_plugin(monkeypatch):
    """A direct plugin removal intentionally leaves durable pairing state.

    The manager must inspect both halves of capture: a valid token is not a
    substitute for the hook plugin that starts the uploader.
    """
    import sys

    import probe.cli.main  # noqa: F401
    from probe.cli import claude_cli
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    installed: list[str] = []
    before = _caps(
        agent_source="codex",
        capture_plugin_installed=False,
        capture_token_sources=(TokenSource.PAIRED_FILE,),
        capture_credential_valid=True,
        plugins_verified=True,
    )
    after = dataclasses.replace(before, capture_plugin_installed=True)

    monkeypatch.setenv("PROBE_AGENT", "codex")
    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "needs_authorization", lambda caps, selection: [])
    monkeypatch.setattr(
        wizard,
        "install_plugin",
        lambda name, **kw: installed.append(name) or claude_cli.Result(ok=True),
    )
    monkeypatch.setattr(doctor_impl, "collect", lambda *a, **k: after)
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])

    cli_main._run_wizard_action(
        Action.CONFIGURE,
        caps=before,
        base_now="https://api.test",
        yes=True,
        tracking=False,
        capture=True,
        auto_update=None,
        agent_rules=False,
        uninstall=False,
        configured=True,
    )

    assert installed == ["probe-research-tap"]


def test_every_failure_message_the_wizard_emits_is_classified_as_one():
    """The progress tick decides ✔ vs ✗ from these strings. If a message the
    apply_* helpers can emit is not recognised, the screen shows a tick over a
    line that says the step failed."""
    import inspect
    import re

    from probe.cli import setup as wizard

    source = inspect.getsource(wizard)
    # Every literal the module appends as a failure/warning message.
    emitted = re.findall(r'messages\.append\(\s*f?"([^"]+)"', source)
    emitted += re.findall(r'return \[\s*f?"(! [^"]+)"', source)
    assert emitted, "expected to find failure messages in setup.py"
    for template in emitted:
        rendered = template.replace("{", "").replace("}", "")
        if not rendered.startswith(("could not", "!")):
            continue  # a success message; nothing to assert
        assert wizard.reports_failure(rendered), f"unclassified failure: {template}"


def test_the_marketplace_is_refreshed_before_the_first_install(monkeypatch):
    """Caught by adversarial review of the hoist itself.

    Hoisting the refresh OUT of install_plugin is only half the change: the
    caller has to actually do it. Without that, the first attempt installs from
    whatever stale copy is on disk -- the exact failure the original code's
    comment warned about ("a fresh wizard run happily installs a stale plugin
    version ... how a newly published plugin appears to be missing") -- and on a
    machine that never added the marketplace at all, attempt one ALWAYS fails
    and only the retry repairs it.
    """
    import sys

    import probe.cli.main  # noqa: F401
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    order: list[str] = []
    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "refresh_marketplace", lambda: order.append("refresh"))
    monkeypatch.setattr(
        wizard, "install_plugin", lambda name, **kw: order.append(f"install:{name}")
    )
    monkeypatch.setattr(wizard, "needs_authorization", lambda caps, selection: [])
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])
    monkeypatch.setattr(
        doctor_impl,
        "collect",
        lambda *a, **k: _caps(tracking_plugin_installed=True, plugins_verified=True),
    )

    cli_main._run_wizard_action(
        Action.CONFIGURE,
        caps=_caps(),  # nothing installed -> a real install is needed
        base_now="https://api.test",
        yes=True,
        tracking=True,
        capture=False,
        auto_update=None,
        agent_rules=False,
        uninstall=False,
        configured=True,
    )

    assert "refresh" in order, "the marketplace must be refreshed before installing"
    assert order.index("refresh") < order.index("install:probe-research")


def test_no_install_means_no_marketplace_refresh(monkeypatch):
    """The refresh is two `claude` subprocesses. A run that installs nothing --
    the common re-run -- must not pay for them."""
    import sys

    import probe.cli.main  # noqa: F401
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    refreshed: list[int] = []
    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "refresh_marketplace", lambda: refreshed.append(1))
    monkeypatch.setattr(wizard, "needs_authorization", lambda caps, selection: [])
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])

    already = _caps(tracking_plugin_installed=True, logged_in_as="x@y.z", plugins_verified=True)
    monkeypatch.setattr(doctor_impl, "collect", lambda *a, **k: already)

    cli_main._run_wizard_action(
        Action.CONFIGURE,
        caps=already,
        base_now="https://api.test",
        yes=True,
        tracking=True,
        capture=False,
        auto_update=True,
        agent_rules=False,
        uninstall=False,
        configured=True,
    )

    assert refreshed == [], "a run with nothing to install must not refresh"


def test_the_credential_is_minted_before_the_plugin_is_installed(monkeypatch):
    """The tracking plugin publishes an MCP server it cannot yet authenticate.

    `plugins/probe-research/.mcp.json` points at the hosted MCP and resolves its
    bearer through a headers helper that reads the token this run mints. Install
    first and there is a window -- as long as a human takes to approve a browser
    prompt -- where the server is on disk with no credential behind it. A connect
    in that window is unauthenticated, the edge answers 401 with a
    `WWW-Authenticate` challenge, and Claude Code discovers an authorization
    server and pins the connection to OAuth. The user then has to open `/mcp` and
    authenticate a device the installer had already authorized, which is the
    symptom this ordering exists to prevent.

    Asserted on the observable call order, not on the work list, because the list
    is an implementation detail and the window is not.
    """
    import sys

    import probe.cli.main  # noqa: F401
    from probe.cli import claude_cli
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    order: list[str] = []

    def _install(name, **kw):
        order.append(f"install:{name}")
        # A real Result, not None. The step swallows an AttributeError and marks
        # itself failed, so a bare stub would leave this test passing on an
        # install that never ran the code path it is timing.
        return claude_cli.Result(ok=True)

    def _authorize(grants, **kw):
        order.append("authorize")
        return {grant: {"token": f"probe_pat_{grant}"} for grant in grants}, []

    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "refresh_marketplace", lambda: order.append("refresh"))
    monkeypatch.setattr(wizard, "install_plugin", _install)
    monkeypatch.setattr(wizard, "authorize", _authorize)
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])
    monkeypatch.setattr(
        doctor_impl,
        "collect",
        lambda *a, **k: _caps(
            tracking_plugin_installed=True, logged_in_as="x@y.z", plugins_verified=True
        ),
    )

    cli_main._run_wizard_action(
        Action.CONFIGURE,
        caps=_caps(),  # a genuinely fresh machine: no plugin, no credential
        base_now="https://api.test",
        yes=True,
        tracking=True,
        capture=False,
        auto_update=None,
        agent_rules=False,
        uninstall=False,
        configured=False,
    )

    assert "authorize" in order, "a fresh machine must run the browser approval"
    assert order.index("authorize") < order.index("install:probe-research"), (
        f"the plugin was installed before it had a credential to serve: {order}"
    )


# --- the picker: enter activates, Next ends it ------------------------------


def _built_menu(defaults=None):
    """Build the picker exactly as run_menu does, and hand back its control."""
    import questionary

    from probe.cli import setup as wizard
    from probe.cli import tui

    defaults = defaults or dict(wizard.FRESH_DEFAULTS)
    tui.use_checkmarks()
    indent = tui.body_indent()
    rows, choices = {}, [questionary.Separator(" ")]
    copy = wizard.menu_copy("claude_code")
    for index, (capability, (title, detail)) in enumerate(copy.items()):
        if index:
            choices.append(questionary.Separator(" "))
        row = questionary.Choice(
            title=wizard._menu_row(title, detail, checked=defaults[capability], indent=indent),
            value=capability,
            checked=defaults[capability],
        )
        rows[capability] = row
        choices.append(row)
    choices.append(questionary.Separator(" "))
    choices.append(questionary.Choice(title=wizard.NEXT_TITLE, value=wizard.NEXT))
    question = questionary.checkbox("m", choices=choices, style=tui.style(), pointer="»")
    wizard._bind_menu_keys(question, rows, copy=copy, indent=indent)
    return question, tui.checkbox_control(question), rows


def _press_enter(question, control):
    """Fire the enter handler prompt_toolkit would ACTUALLY run.

    questionary binds enter to submit and we bind it to activate-the-row, so
    both match. prompt_toolkit resolves that with `matches[-1]`
    (key_processor.py) -- the LAST binding registered wins, which is ours. A
    test that fired the first match would exercise questionary's handler and
    prove nothing about this feature.
    """
    from prompt_toolkit.keys import Keys

    exited = {}

    class FakeApp:
        def exit(self, result=None):
            exited["result"] = result

    class FakeEvent:
        app = FakeApp()

    matches = question.application.key_bindings.get_bindings_for_keys((Keys.ControlM,))
    assert matches, "no enter binding found"
    matches[-1].handler(FakeEvent())
    return exited


def test_the_next_row_carries_no_checkbox():
    """A row you can put the cursor on ALWAYS gets questionary's box, so `○ Next`
    reads as an option someone forgot to tick rather than the way forward. We
    take the box over so the action row can go without one."""
    from probe.cli import tui

    _, control, _ = _built_menu()
    assert control.use_indicator is False, "questionary must not be drawing boxes"
    rendered = "".join(t[1] for t in control._get_choice_tokens())
    next_line = next(ln for ln in rendered.split("\n") if "Next" in ln)
    assert tui.TICK not in next_line and tui.UNTICK not in next_line, next_line
    # ...while the capability rows still show one.
    cli_line = next(ln for ln in rendered.split("\n") if "CLI + MCP" in ln)
    assert tui.TICK in cli_line


def test_enter_toggles_the_row_under_the_cursor():
    """The whole point of the change: enter activates what you are looking at."""
    question, control, _ = _built_menu()
    assert Capability.TRACKING in control.selected_options
    control.pointed_at = control.choices.index(
        next(c for c in control.choices if getattr(c, "value", None) is Capability.TRACKING)
    )

    exited = _press_enter(question, control)
    assert exited == {}, "enter on a capability must NOT submit"
    assert Capability.TRACKING not in control.selected_options, "it must toggle off"

    _press_enter(question, control)
    assert Capability.TRACKING in control.selected_options, "and back on"


def test_enter_on_next_submits():
    question, control, _ = _built_menu()
    control.pointed_at = control.choices.index(
        next(c for c in control.choices if getattr(c, "value", None) == setup.NEXT)
    )
    exited = _press_enter(question, control)
    assert "result" in exited, "enter on Next must submit"
    assert setup.NEXT not in exited["result"], "the action row is not a capability"
    expected = {c for c, on in setup.FRESH_DEFAULTS.items() if on} - {Capability.AUTO_UPDATE}
    assert set(exited["result"]) == expected


def test_clicking_straight_to_next_grants_exactly_the_defaults():
    """A Next row means people accept defaults without touching a row, so the
    defaults ARE the grant. Everything ships on now, capture included, so what
    this pins is that clicking through grants the defaults and NOTHING MORE --
    no row silently on that the screen did not show ticked."""
    defaults = {
        Capability.TRACKING: True,
        Capability.CAPTURE: True,
        Capability.AGENT_RULES: False,  # deliberately off, to prove it stays off
        Capability.AUTO_UPDATE: True,
    }
    question, control, _ = _built_menu(defaults)
    control.pointed_at = control.choices.index(
        next(c for c in control.choices if getattr(c, "value", None) == setup.NEXT)
    )
    granted = set(_press_enter(question, control)["result"])
    assert granted == {Capability.TRACKING, Capability.CAPTURE}
    assert Capability.AGENT_RULES not in granted, "an unticked row must stay unticked"


def test_the_capture_row_is_visibly_ticked_before_next_is_reachable():
    """What replaced the default-off as the consent gate. The grant has to be ON
    SCREEN and refusable: capture ticked, labelled, and above the Next row so it
    is passed over on the way there."""
    from probe.cli import tui

    _, control, _ = _built_menu()
    values = [getattr(c, "value", None) for c in control.choices]
    assert values.index(Capability.CAPTURE) < values.index(setup.NEXT)

    rendered = "".join(t[1] for t in control._get_choice_tokens())
    capture_line = next(ln for ln in rendered.split("\n") if "Session capture" in ln)
    assert tui.TICK in capture_line, "capture must show as ticked, not merely be on"


def test_the_box_redraws_when_a_row_is_toggled():
    """We own the box now, so nothing else will keep it in sync. A stale glyph
    would say a capability is on while the selection says otherwise."""
    from probe.cli import tui

    question, control, rows = _built_menu()
    row = rows[Capability.CAPTURE]
    control.pointed_at = control.choices.index(row)
    # Direction-agnostic: what matters is the box FOLLOWS the selection, not
    # which way it starts. Pinning the start couples this to FRESH_DEFAULTS,
    # which is a product decision that moves.
    before = row.title.lstrip()[0]
    assert before in (tui.TICK, tui.UNTICK)

    _press_enter(question, control)
    after = row.title.lstrip()[0]
    assert after != before, "box must follow the toggle"
    assert after in (tui.TICK, tui.UNTICK)
    on_now = Capability.CAPTURE in control.selected_options
    assert after == (tui.TICK if on_now else tui.UNTICK), "box must match the selection"


def test_the_instruction_line_names_enter_not_space():
    """The instruction is the only place the new rule is written down."""
    import inspect

    source = inspect.getsource(setup.run_menu)
    instruction = source.split("instruction=")[1].split("\n")[0]
    assert "enter" in instruction.lower()
    assert "space toggles" not in instruction.lower(), "space is no longer the headline"


def test_next_is_the_last_row_and_the_cursor_does_not_start_on_it():
    """Consent gate: the choices must be in front of you before the way out is.
    A cursor that starts on Next turns the menu into a single keystroke."""
    _, control, _ = _built_menu()
    values = [getattr(c, "value", None) for c in control.choices]
    assert values[-1] == setup.NEXT, "Next must be last"
    assert control.get_pointed_at().value is not setup.NEXT
    assert control.get_pointed_at().value is Capability.TRACKING


# --- the credential gate --------------------------------------------------


def test_a_refused_approval_installs_nothing(monkeypatch):
    """A cancelled browser approval must not leave a plugin behind.

    The tracking plugin publishes an MCP server whose bearer comes from the
    credential this run failed to mint. Installing it anyway puts an `.mcp.json`
    on disk with nothing behind it, and the first unauthenticated connect is
    answered with a `WWW-Authenticate` challenge that pins Claude Code to OAuth --
    so the user is sent to `/mcp` to authenticate a device that was never
    authorized at all. Leaving the machine as it was is the honest outcome of a
    refused approval.
    """
    import sys

    import typer

    import probe.cli.main  # noqa: F401
    from probe.cli import claude_cli
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    order: list[str] = []

    def _install(name, **kw):
        order.append(f"install:{name}")
        return claude_cli.Result(ok=True)

    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "refresh_marketplace", lambda: order.append("refresh"))
    monkeypatch.setattr(wizard, "install_plugin", _install)
    # The user closed the browser tab: approved nothing, minted nothing.
    monkeypatch.setattr(
        wizard, "authorize", lambda grants, **kw: (order.append("authorize"), ({}, []))[1]
    )
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])
    monkeypatch.setattr(doctor_impl, "collect", lambda *a, **k: _caps())

    with pytest.raises(typer.Exit):
        cli_main._run_wizard_action(
            Action.CONFIGURE,
            caps=_caps(),
            base_now="https://api.test",
            yes=True,
            tracking=True,
            capture=False,
            auto_update=None,
            agent_rules=False,
            uninstall=False,
            configured=False,
        )

    assert "authorize" in order
    assert not [step for step in order if step.startswith("install:")], (
        f"a plugin was installed with no credential behind it: {order}"
    )


def test_a_partial_grant_installs_only_what_it_can_authenticate(monkeypatch):
    """One failed grant must not veto the capability that DID get its credential.

    Refusing both would punish someone whose tracking approval succeeded for a
    capture grant the server declined -- and the two are independent plugins with
    independent credentials.
    """
    import sys

    import typer

    import probe.cli.main  # noqa: F401
    from probe.cli import claude_cli
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    order: list[str] = []

    def _install(name, **kw):
        order.append(f"install:{name}")
        return claude_cli.Result(ok=True)

    def _authorize(grants, **kw):
        # api + mcp came back; capture did not.
        return {g: {"token": f"probe_pat_{g}"} for g in grants if g != "capture"}, []

    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "refresh_marketplace", lambda: None)
    monkeypatch.setattr(wizard, "install_plugin", _install)
    monkeypatch.setattr(wizard, "authorize", _authorize)
    monkeypatch.setattr(wizard, "clear_killswitch", lambda: None)
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])
    monkeypatch.setattr(
        doctor_impl,
        "collect",
        lambda *a, **k: _caps(tracking_plugin_installed=True, logged_in_as="x@y.z"),
    )

    with pytest.raises(typer.Exit):
        cli_main._run_wizard_action(
            Action.CONFIGURE,
            caps=_caps(),
            base_now="https://api.test",
            yes=True,
            tracking=True,
            capture=True,
            auto_update=None,
            agent_rules=False,
            uninstall=False,
            configured=False,
        )

    assert f"install:{capabilities_mod.TRACKING_PLUGIN_NAME}" in order, (
        f"tracking had its credential and must still install: {order}"
    )
    assert f"install:{capabilities_mod.TAP_PLUGIN_NAME}" not in order, (
        f"capture had no credential and must not install: {order}"
    )


def test_turning_a_capability_off_is_never_gated_on_a_credential(monkeypatch):
    """Removal must work when minting fails.

    Gating an uninstall on a grant would trap someone on the very plugin they
    just asked to remove -- and removal needs no credential to be correct.
    """
    import sys

    import probe.cli.main  # noqa: F401
    from probe.cli import claude_cli
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.actions import Action

    cli_main = sys.modules["probe.cli.main"]
    order: list[str] = []

    monkeypatch.setattr(tui, "interactive", lambda: False)
    monkeypatch.setattr(wizard, "interactive", lambda: False)
    monkeypatch.setattr(
        wizard,
        "uninstall_plugin",
        lambda name: (order.append(f"uninstall:{name}"), claude_cli.Result(ok=True))[1],
    )
    monkeypatch.setattr(wizard, "authorize", lambda grants, **kw: ({}, []))
    monkeypatch.setattr(cli_main, "_register_local_capabilities", lambda *a, **k: [])
    monkeypatch.setattr(doctor_impl, "collect", lambda *a, **k: _caps())

    cli_main._run_wizard_action(
        Action.CONFIGURE,
        caps=_caps(tracking_plugin_installed=True, logged_in_as="x@y.z"),
        base_now="https://api.test",
        yes=True,
        tracking=False,
        capture=False,
        auto_update=None,
        agent_rules=False,
        uninstall=False,
        configured=True,
    )

    assert f"uninstall:{capabilities_mod.TRACKING_PLUGIN_NAME}" in order


def test_a_rerun_that_already_holds_its_grants_is_not_gated():
    """`granted` is empty on a healthy re-run -- nothing was requested. Reading
    that as failure would refuse to install on every machine already signed in."""
    from probe.cli.capabilities import Capability

    assert setup.blocked_by_missing_grants(Capability.TRACKING, needed=[], granted={}) == []
    assert (
        setup.blocked_by_missing_grants(
            Capability.TRACKING, needed=["api", "mcp"], granted={"api": {}, "mcp": {}}
        )
        == []
    )
    assert setup.blocked_by_missing_grants(
        Capability.TRACKING, needed=["api", "mcp"], granted={"api": {}}
    ) == ["mcp"]


def test_the_grant_table_is_the_only_source_of_what_a_capability_needs():
    """grants_for and the gate must read ONE table. Two hardcoded lists is how a
    third grant gets added to the request and not to the check."""
    from probe.cli.capabilities import Capability

    everything = setup.Selection(tracking=True, capture=True, auto_update=False, agent_rules=False)
    requested = set(setup.grants_for(everything))
    tabled = {g for grants in setup.CAPABILITY_GRANTS.values() for g in grants}
    assert requested == tabled

    for capability in (Capability.TRACKING, Capability.CAPTURE):
        needed = list(setup.CAPABILITY_GRANTS[capability])
        # Every grant the table names must be able to block its own capability.
        assert setup.blocked_by_missing_grants(capability, needed=needed, granted={}) == needed
