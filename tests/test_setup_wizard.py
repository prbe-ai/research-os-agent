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
    autoupdate.save(enabled=True, channel=autoupdate.Channel.STABLE)
    loaded = autoupdate.load()
    assert loaded.enabled is True
    assert loaded.channel is autoupdate.Channel.STABLE


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
    assert set(setup.MENU_COPY) == set(Capability)
    for title, detail in setup.MENU_COPY.values():
        assert "probe-research" not in title
        assert not any("probe-research" in line for line in detail)


def test_capture_menu_copy_says_what_leaves_the_machine():
    _, detail = setup.MENU_COPY[Capability.CAPTURE]
    blob = " ".join(detail)
    assert "prompts" in blob
    assert "file contents" in blob
    assert "tool output" in blob
    assert "stripped on the server" in blob


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


def test_every_action_has_copy():
    from probe.cli.actions import ACTION_COPY, Action

    assert set(ACTION_COPY) == set(Action)
    for title, detail in ACTION_COPY.values():
        assert title and detail


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
