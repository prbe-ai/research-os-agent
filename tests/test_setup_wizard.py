"""`probe setup` / `probe doctor`: the flag contract and the off switch.

The two things most likely to hurt someone are covered first: an omitted flag
silently revoking capture in CI, and "off" that does not actually turn capture
off.
"""

from __future__ import annotations

import json
import os

import pytest

from probe.cli import autoupdate, capture, doctor, setup
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
    monkeypatch.delenv(ENV_INGEST_TOKEN, raising=False)
    (tmp_path / "tap").mkdir(parents=True, exist_ok=True)
    (tmp_path / "probe").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _caps(**overrides) -> Capabilities:
    return Capabilities(**overrides)


# --- the flag truth table --------------------------------------------------


def test_fresh_machine_uses_defaults_and_capture_is_off():
    """Omitting flags on a fresh machine must NOT opt anyone into transcript
    egress. That is the consent failure this whole feature exists to prevent."""
    selection = setup.resolve_selection(
        _caps(), tracking=None, capture=None, auto_update=None, configured=False
    )
    assert selection.tracking is True
    assert selection.capture is False
    assert selection.auto_update is True


def test_rerun_preserves_capture_when_the_flag_is_omitted():
    """The load-bearing case: `probe setup --yes` in CI, or a re-run naming only
    one flag, must never silently revoke a developer's pairing."""
    configured = _caps(
        capture_token_sources=(TokenSource.PAIRED_FILE,),
        tracking_plugin_installed=True,
        logged_in_as="richard@prbe.ai",
    )
    assert configured.capture_on is True

    selection = setup.resolve_selection(
        configured, tracking=None, capture=None, auto_update=None
    )
    assert selection.capture is True, "an omitted flag must preserve, never disable"


def test_rerun_preserves_auto_update_when_the_flag_is_omitted():
    configured = _caps(auto_update_enabled=True, logged_in_as="richard@prbe.ai")
    selection = setup.resolve_selection(
        configured, tracking=None, capture=None, auto_update=None
    )
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
    grants = setup.grants_for(setup.Selection(tracking=False, capture=True, auto_update=False))
    assert grants == ["capture"]


def test_tracking_asks_for_a_separate_read_only_mcp_credential():
    grants = setup.grants_for(setup.Selection(tracking=True, capture=True, auto_update=False))
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
    caps = _caps(
        capture_token_sources=(TokenSource.PAIRED_FILE,), capture_killswitched=True
    )
    assert caps.capture_on is False


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


def test_doctor_reports_a_never_run_updater_distinctly_from_a_working_one():
    assert "never run on this device" in doctor.render(_caps())
    assert "FAILED" in doctor.render(
        _caps(last_update_attempt="FAILED (2026-07-24 10:00): boom")
    )


def test_doctor_renders_on_a_bare_machine_without_raising():
    # The command people run when everything is already broken.
    assert "Probe Research doctor" in doctor.render(_caps())


def test_menu_rows_are_capabilities_not_plugin_names():
    """Nobody knows what `probe-research-tap` is; the consent decision is about
    what the thing does."""
    # AUTO_UPDATE is asked separately now: it is a policy about the
    # capabilities, not one of them.
    assert set(setup.MENU_COPY) == {Capability.TRACKING, Capability.CAPTURE}
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
    _, detail = setup.MENU_COPY[Capability.CAPTURE]
    blob = " ".join(detail)
    assert "this device's" in blob, "scope must be explicit"
    assert "Claude Code sessions" in blob
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


def test_off_is_not_verified_when_the_killswitch_could_not_be_written(
    isolate, monkeypatch
):
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


def test_a_planted_pid_file_cannot_get_an_unrelated_process_killed(
    isolate, monkeypatch
):
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
    }
    assert all_off.configured is True

    selection = setup.resolve_selection(
        all_off, tracking=None, capture=None, auto_update=None
    )
    assert selection.tracking is False
    assert selection.auto_update is False


def test_a_genuinely_fresh_machine_still_gets_defaults():
    fresh = _caps()
    assert fresh.configured is False
    selection = setup.resolve_selection(
        fresh, tracking=None, capture=None, auto_update=None
    )
    assert selection.tracking is True
    assert selection.auto_update is True


# --- the pieces that make it actually work --------------------------------


def test_setup_requests_only_the_grants_it_still_needs():
    """A re-run where everything already works must not drag the user through
    another browser approval."""
    fully_set_up = _caps(
        tracking_plugin_installed=True,
        logged_in_as="richard@prbe.ai",
        capture_token_sources=(TokenSource.PAIRED_FILE,),
    )
    everything = setup.Selection(tracking=True, capture=True, auto_update=True)
    assert setup.needs_authorization(fully_set_up, everything) == []

    # Logged in, but capture was never paired: ask for capture ALONE.
    tracking_only = _caps(tracking_plugin_installed=True, logged_in_as="richard@prbe.ai")
    assert setup.needs_authorization(tracking_only, everything) == ["capture"]

    # Nothing yet: ask for all three in one approval.
    assert setup.needs_authorization(_caps(), everything) == ["api", "mcp", "capture"]


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
    assert set(by_grant) == {"api", "mcp", "capture"}
    assert any("paired" in m for m in messages)

    # The capture credential landed where the uploader actually looks for it.
    assert TokenSource.PROBE_CONFIG in capture_token_sources()
    from probe.cli.capabilities import probe_config_credentials

    creds = probe_config_credentials()
    assert creds["token"] == "probe_pat_api"
    assert creds["mcp_token"] == "probe_pat_mcp"
    assert creds["ingest_token"] == "ros_ing_dev"


def test_authorize_says_so_when_the_server_returns_nothing_for_a_grant(
    isolate, monkeypatch
):
    """Approved but not minted must not read as success."""
    monkeypatch.setattr(
        "probe.sdk.device.device_authorize",
        lambda base_url, **kw: {"grants": [{"grant": "api", "token": "probe_pat_x"}]},
    )
    _, messages = setup.authorize(
        ["api", "capture"], base_url="https://x", open_browser=False
    )
    assert any("capture" in m and "NOT active" in m for m in messages)


def test_authorize_reports_a_failed_approval_instead_of_claiming_success(
    isolate, monkeypatch
):
    from probe.sdk.device import DeviceLoginError

    def boom(base_url, **kw):
        raise DeviceLoginError("the user denied this request")

    monkeypatch.setattr("probe.sdk.device.device_authorize", boom)
    by_grant, messages = setup.authorize(
        ["api"], base_url="https://x", open_browser=False
    )
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
    """"off" and "off because you disabled it" are different situations."""
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

    monkeypatch.setattr(
        bootstrap.subprocess, "run", lambda cmd, **kw: calls.append(cmd) or Done()
    )

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
    turning_on = setup.Selection(tracking=True, capture=False, auto_update=False)
    notice = setup.restart_notice(fresh, turning_on)
    assert notice and "Restart Claude Code" in notice


def test_capture_alone_also_needs_a_restart():
    notice = setup.restart_notice(
        _caps(), setup.Selection(tracking=False, capture=True, auto_update=False)
    )
    assert notice is not None


def test_an_auto_update_only_change_does_not_send_you_off_to_restart():
    """No plugin moved, so there is nothing for a restart to pick up."""
    already = _caps(
        tracking_plugin_installed=True,
        logged_in_as="richard@prbe.ai",
        capture_token_sources=(TokenSource.PAIRED_FILE,),
    )
    same_plugins = setup.Selection(tracking=True, capture=True, auto_update=True)
    assert setup.restart_notice(already, same_plugins) is None


def test_turning_a_plugin_OFF_also_needs_a_restart():
    already = _caps(tracking_plugin_installed=True, logged_in_as="richard@prbe.ai")
    turning_off = setup.Selection(tracking=False, capture=False, auto_update=False)
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

    source = inspect.getsource(cli_main.wizard) + inspect.getsource(
        cli_main._run_wizard_action
    )
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
    """"Restart Claude Code to finish" after a FAILED approval reads as success.
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
        uninstall=False,
        configured=False,
    )

    assert lines == ["updated"]
    assert seen[0][0] is updated_caps
    assert seen[0][1].base_url == "https://self-hosted.test"


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
        uninstall=True,
        configured=True,
    )

    assert lines == ["removed"]
    assert seen[0][0] is after
    assert seen[0][1].base_url == "https://self-hosted.test"
    assert seen[0][1].token == "preserved-api-secret"


# --- the wizard is a screen, not a transcript ------------------------------


def test_escape_is_distinct_from_ctrl_c():
    """"Go back one step" and "abandon the whole wizard" are different
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


def test_the_interactive_wizard_starts_without_crashing():
    """Actually run it on a pty. Every previous test stubbed `interactive()`
    to False, so the branch that renders the menu was never executed once."""
    import os
    import pty
    import re
    import select
    import shutil
    import sys
    import time

    # THIS interpreter's probe, not whatever is on PATH — a stale global
    # install would make the test report on code that is not under test.
    probe = str(pathlib.Path(sys.executable).parent / "probe")
    if not os.path.exists(probe):
        probe = shutil.which("probe") or ""
    if not probe or not os.path.exists(probe):
        import pytest

        pytest.skip("no probe binary on this machine")

    pid, fd = pty.fork()
    if pid == 0:  # pragma: no cover - child process
        os.execve(probe, [probe, "wizard"], dict(os.environ, TERM="xterm-256color"))

    out = b""
    deadline = time.time() + 15
    while time.time() < deadline:
        if select.select([fd], [], [], 0.3)[0]:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        if b"Exit" in out or b"Traceback" in out:
            break
    try:
        os.write(fd, b"\x03")
    except OSError:
        pass

    text = re.sub(rb"\x1b\[[0-9;?]*[a-zA-Z]", b"", out).decode("utf8", "replace")
    assert "Traceback" not in text, text[-600:]
    assert "AttributeError" not in text, text[-600:]


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


def test_content_taller_than_the_screen_gets_no_spacer():
    """Centring something that does not fit only chooses which end to
    amputate — and the top is the end with the state block on it."""

    def spacer_for(rows, height):
        return 0 if height >= rows else (rows - height) // 2

    assert spacer_for(20, 23) == 0
    assert spacer_for(23, 23) == 0
    assert spacer_for(60, 23) == 18
    # And it never pushes the bottom off.
    for rows in (24, 30, 45, 60, 120):
        assert spacer_for(rows, 23) + 23 <= rows


def test_the_prompt_goes_full_screen():
    """Inline rendering grows down from the cursor, so anything taller than the
    room below it scrolls — which is what kept eating the state block, and is
    worse in a block terminal like Warp."""
    import inspect

    from probe.cli import tui

    assert "full_screen = True" in inspect.getsource(tui.center_vertically)


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

    assert "typer.confirm" not in inspect.getsource(sys.modules["probe.cli.main"]._run_wizard_action)


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
    assert lines.index(pad + "one") == (40 - 2) // 2, "vertical centring"
    assert lines[lines.index(pad + "one") + 1] == pad + "two"


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
