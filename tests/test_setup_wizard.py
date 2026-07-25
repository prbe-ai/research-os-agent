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
