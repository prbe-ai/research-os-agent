"""Telling a Codex researcher that Probe's own hooks are switched off.

Codex trusts each hooks.json entry by a hash of the whole entry and silently
skips one it has not approved. When Probe's tracking plugins moved to one
dispatcher entry per event, every Probe hook stayed off after the upgrade until
the researcher approved it in `/hooks`, and nothing said so. The tap's entries
did not change, so its hooks are where the researcher hears about it
(hooks/probe-hooks-trust.sh):

  * SessionStart records once whether the session should expect Probe's
    heartbeat (an enabled Probe plugin whose newest version has dispatch.sh);
  * every prompt prints one systemMessage while expected and no heartbeat.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
TRUST = HOOKS / "probe-hooks-trust.sh"
ENSURE = HOOKS / "ensure-daemon.sh"
SID = "01a06383-6f4e-751a-b94c-ee5ea70d00f7"
MARKETPLACE = "research-os-agent"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX shell")


@pytest.fixture
def codex(tmp_path):
    """A Codex home with the tap installed beside Probe's plugins."""
    home = tmp_path / "codex"
    cache = home / "plugins" / "cache" / MARKETPLACE
    tap = cache / "probe-research-tap" / "0.9.7"
    (tap / "hooks").mkdir(parents=True)
    for name in ("probe-hooks-trust.sh", "ensure-daemon.sh", "session-start.sh"):
        (tap / "hooks" / name).write_bytes((HOOKS / name).read_bytes())
        (tap / "hooks" / name).chmod(0o755)
    state = tmp_path / "xdg"
    return {"home": home, "cache": cache, "tap": tap, "state": state / "probe" / "hooks", "xdg": state}


def _install(codex: dict, name: str, version: str, *, dispatcher: bool, age_s: float = 0) -> Path:
    root = codex["cache"] / name / version
    (root / ".codex-plugin").mkdir(parents=True)
    (root / "hooks").mkdir()
    manifest = root / ".codex-plugin" / "plugin.json"
    manifest.write_text(json.dumps({"name": name, "version": version}), encoding="utf-8")
    if dispatcher:
        (root / "hooks" / "dispatch.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    then = time.time() - age_s
    os.utime(manifest, (then, then))
    return root


def _config(codex: dict, enabled: dict[str, bool]) -> None:
    codex["home"].mkdir(parents=True, exist_ok=True)
    lines = ['model = "x"', ""]
    for name, on in enabled.items():
        lines += [f'[plugins."{name}@{MARKETPLACE}"]', f"enabled = {'true' if on else 'false'}", ""]
    lines += ["[hooks.state]", ""]
    (codex["home"] / "config.toml").write_text("\n".join(lines), encoding="utf-8")


def _bash(codex: dict, script: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "CODEX_HOME": str(codex["home"]), "XDG_STATE_HOME": str(codex["xdg"])}
    return subprocess.run(
        ["/bin/bash", "-c", f'set -uo pipefail; . "{TRUST}"; {script}'],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
    )


def _record(codex: dict, sid: str = SID, event: str = "SessionStart") -> str:
    result = _bash(codex, f'probe_hooks_record "{sid}" "{codex["tap"]}/" "{event}"; echo rc=$?')
    assert result.stdout.strip() == "rc=0", result.stderr
    path = codex["state"] / "expect" / sid
    return path.read_text(encoding="utf-8").strip() if path.exists() else ""


# --- what SessionStart records -------------------------------------------------


def test_an_enabled_plugin_with_the_dispatcher_is_expected(codex):
    _install(codex, "probe-research-daemon", "0.117.0", dispatcher=True)
    _config(codex, {"probe-research-daemon": True})
    assert _record(codex) == "1"


def test_a_plugin_from_before_the_dispatcher_is_not(codex):
    """A session started before the upgrade runs the old entries, which write no
    heartbeat: telling it to approve anything would be false."""
    _install(codex, "probe-research-daemon", "0.116.0", dispatcher=False)
    _config(codex, {"probe-research-daemon": True})
    assert _record(codex) == "0"


@pytest.mark.parametrize("enabled", [{"probe-research-daemon": False}, {}], ids=["disabled", "no-section"])
def test_a_plugin_codex_does_not_run_is_not(codex, enabled):
    _install(codex, "probe-research-daemon", "0.117.0", dispatcher=True)
    _config(codex, enabled)
    assert _record(codex) == "0"


def test_the_newest_installed_version_decides(codex):
    """By the manifest's age, the rule the hooks' stale-root recovery uses."""
    _install(codex, "probe-research", "0.117.0", dispatcher=True, age_s=3600)
    _install(codex, "probe-research", "0.116.0", dispatcher=False, age_s=0)
    _config(codex, {"probe-research": True})
    assert _record(codex) == "0"


def test_either_tracking_plugin_counts(codex):
    _install(codex, "probe-research", "0.116.0", dispatcher=False)
    _install(codex, "probe-research-daemon", "0.117.0", dispatcher=True)
    _config(codex, {"probe-research": True, "probe-research-daemon": True})
    assert _record(codex) == "1"


def test_a_respawn_never_re_records(codex):
    """ensure-daemon.sh re-runs session-start.sh mid-session. A session that
    started on the old plugin must keep its 0 after the upgrade lands."""
    root = _install(codex, "probe-research-daemon", "0.116.0", dispatcher=False)
    _config(codex, {"probe-research-daemon": True})
    assert _record(codex) == "0"
    (root / "hooks" / "dispatch.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    assert _record(codex) == "0"


def test_a_respawn_in_a_session_that_never_recorded_records_0(codex):
    """A session started under a tap with no record (<= 0.9.6) whose daemon is
    respawned after the upgrade lands: it runs the OLD Probe entries, so the
    heartbeat never comes. Only a real SessionStart may expect it."""
    _install(codex, "probe-research-daemon", "0.117.0", dispatcher=True)
    _config(codex, {"probe-research-daemon": True})
    assert _record(codex, event="UserPromptSubmit") == "0"
    assert _record(codex, event="SessionStart") == "0", "and never over an existing record"


def test_no_config_no_cache_records_nothing_expected(codex):
    assert _record(codex) == "0"


# --- what each prompt says -----------------------------------------------------


def _nudge(codex: dict) -> str:
    return _bash(codex, f'probe_hooks_nudge "{SID}" "{codex["tap"]}"; echo rc=$?').stdout


def _set(codex: dict, expect: str | None, alive: bool) -> None:
    for sub in ("expect", "alive"):
        (codex["state"] / sub).mkdir(parents=True, exist_ok=True)
    if expect is not None:
        (codex["state"] / "expect" / SID).write_text(expect + "\n", encoding="utf-8")
    if alive:
        (codex["state"] / "alive" / SID).touch()


def test_expected_and_silent_prints_one_message(codex):
    _set(codex, "1", alive=False)
    out = _nudge(codex).splitlines()
    assert out[-1] == "rc=0"
    [line] = out[:-1]
    message = json.loads(line)["systemMessage"]
    assert "/hooks" in message and "Probe" in message


@pytest.mark.parametrize(
    ("expect", "alive"), [("1", True), ("0", False), (None, False)], ids=["heartbeat", "not-expected", "unrecorded"]
)
def test_otherwise_silent(codex, expect, alive):
    _set(codex, expect, alive)
    assert _nudge(codex) == "rc=0\n"


def test_it_is_said_on_three_prompts_then_never_again(codex):
    """Seen, never a nag the researcher cannot stop (if an approval only
    reaches the next session, this one must not repeat it forever)."""
    _set(codex, "1", alive=False)
    said = ["systemMessage" in _nudge(codex) for _ in range(5)]
    assert said == [True, True, True, False, False]


def test_switching_probe_off_in_codex_stops_it(codex):
    _set(codex, "1", alive=False)
    _config(codex, {"probe-research-daemon": False})
    later = time.time() + 5
    os.utime(codex["home"] / "config.toml", (later, later))
    assert _nudge(codex) == "rc=0\n"
    assert (codex["state"] / "expect" / SID).read_text(encoding="utf-8").strip() == "0"


def test_an_approval_that_rewrites_the_config_keeps_it_while_probe_is_on(codex):
    _set(codex, "1", alive=False)
    _config(codex, {"probe-research-daemon": True})
    later = time.time() + 5
    os.utime(codex["home"] / "config.toml", (later, later))
    assert "systemMessage" in _nudge(codex)


def test_no_home_no_error(codex):
    """ensure-daemon.sh runs under `set -u`: an unset HOME must not end it."""
    env = {k: v for k, v in os.environ.items() if k not in ("HOME", "XDG_STATE_HOME", "CODEX_HOME")}
    result = subprocess.run(["/bin/bash", "-c", f'set -u; . "{TRUST}"; probe_hooks_nudge "{SID}" "{codex["tap"]}"; echo rc=$?'],
                            capture_output=True, text=True, env=env, timeout=10)
    assert result.stdout == "rc=0\n" and result.stderr == ""


# --- the prompt hook end to end ------------------------------------------------


def _ensure(codex: dict, source: str) -> subprocess.CompletedProcess:
    prefix = "prbe-codex-tap" if source == "codex" else "probe-research-tap"
    pid = Path("/tmp") / f"{prefix}-watcher-{SID}.pid"
    pid.write_text(str(os.getpid()), encoding="utf-8")  # a live daemon: the hot path
    try:
        return subprocess.run(
            ["/bin/bash", str(codex["tap"] / "hooks" / "ensure-daemon.sh")],
            input=json.dumps({"session_id": SID}),
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PLUGIN_ROOT": str(codex["tap"]),
                "PROBE_TAP_SOURCE": source,
                "CODEX_HOME": str(codex["home"]),
                "XDG_STATE_HOME": str(codex["xdg"]),
                "PRBE_CODEX_TAP_PLUGIN_DIR": str(codex["home"] / "state"),
            },
            timeout=10,
        )
    finally:
        pid.unlink(missing_ok=True)


def test_the_prompt_hook_says_it_on_codex(codex):
    _set(codex, "1", alive=False)
    result = _ensure(codex, "codex")
    assert result.returncode == 0
    assert "/hooks" in json.loads(result.stdout)["systemMessage"]


def test_the_prompt_hook_is_silent_once_probe_runs(codex):
    _set(codex, "1", alive=True)
    result = _ensure(codex, "codex")
    assert (result.returncode, result.stdout) == (0, "")


def test_claude_code_never_hears_it(codex):
    """Claude Code has no per-hook approval; the files mean nothing there."""
    _set(codex, "1", alive=False)
    result = _ensure(codex, "claude_code")
    assert (result.returncode, result.stdout) == (0, "")


def test_session_start_records_before_its_token_exits(codex, tmp_path):
    """The record must not depend on capture being set up: an unpaired tap
    still runs SessionStart, and still has to tell the researcher."""
    _install(codex, "probe-research-daemon", "0.117.0", dispatcher=True)
    _config(codex, {"probe-research-daemon": True})
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PROBE_INGEST", "PRBE_CODEX_TAP", "PROBE_RESEARCH_TAP"))
    }
    result = subprocess.run(
        ["/bin/bash", str(codex["tap"] / "hooks" / "session-start.sh")],
        input=json.dumps({"session_id": SID, "cwd": str(tmp_path), "hook_event_name": "SessionStart"}),
        capture_output=True,
        text=True,
        env={
            **env,
            "HOME": str(tmp_path / "home"),
            "PLUGIN_ROOT": str(codex["tap"]),
            "PROBE_TAP_SOURCE": "codex",
            "CODEX_HOME": str(codex["home"]),
            "XDG_STATE_HOME": str(codex["xdg"]),
            "PRBE_CODEX_TAP_PLUGIN_DIR": str(tmp_path / "tapstate"),
        },
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert (codex["state"] / "expect" / SID).read_text(encoding="utf-8").strip() == "1"
