"""The tap's shell hooks under Kimi Code, driven through the real scripts and
the exact commands `.kimi-plugin/plugin.json` gives Kimi.

Kimi's payloads differ from Claude Code's in the two ways capture depends on:
the session id is `session_<uuid>` (Probe stores the bare UUID), and there is
no `transcript_path` (it comes from Kimi's own session index). Claude Code's
and Codex's paths through the same scripts are pinned by test_hook_spawn.py,
test_ensure_daemon_hook.py and test_codex_research_os_contract.py, unchanged.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from tap import hook_env

TAP = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((TAP / ".kimi-plugin" / "plugin.json").read_text())
FIXTURE_SESSION = "c1c22c25-6382-4e79-8f00-a1a45e3c8e77"  # the one with recorded hook payloads

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX shell")

#: Stands in for the daemon's interpreter ($PLUGIN_ROOT/.venv/bin/python3):
#: records what session-start.sh spawns and starts nothing.
STUB_PYTHON = """#!/bin/bash
if [ "$1" = "-c" ]; then
    shift 6  # -c SHIM /bin/bash -c WRAPPER wrapper
    printf '%s\\n' "$@" >"$STUB_SPAWN"
    echo $$ >"$6"
fi
exit 0
"""


def _command(event: str, *, wait: bool = False) -> str:
    entries = [h for h in MANIFEST["hooks"] if h["event"] == event]
    if event == "SessionEnd":
        entries = [h for h in entries if ("--wait" in h["command"]) == wait]
    assert len(entries) == 1, (event, entries)
    return entries[0]["command"]


def _payload(name: str, kimi_fixtures: Path, sid: str) -> str:
    raw = json.loads((kimi_fixtures / "hook_payloads" / f"tui-ask-{name}.json").read_text())
    raw["session_id"] = f"session_{sid}"
    return json.dumps(raw)


@pytest.fixture
def kimi(kimi_fixtures, tmp_path):
    """A Kimi Code home holding one real session under a fresh id (so the
    real /tmp pid files this touches are this test's alone), Kimi's index of
    it, a paired capture state folder, and a copy of the plugin whose daemon
    interpreter is the stub."""
    sid = str(uuid.uuid4())
    home = tmp_path / "home"
    kimi_home = tmp_path / "kimi"
    src = kimi_fixtures / "sessions" / "wd_proj_846899122802" / f"session_{FIXTURE_SESSION}"
    session_dir = kimi_home / "sessions" / "wd_proj_846899122802" / f"session_{sid}"
    shutil.copytree(src, session_dir)
    state = json.loads((session_dir / "state.json").read_text())
    state["id"] = f"session_{sid}"
    (session_dir / "state.json").write_text(json.dumps(state))
    (kimi_home / "session_index.jsonl").write_text(json.dumps(
        {"sessionId": f"session_{sid}", "sessionDir": str(session_dir), "workDir": "/w"}) + "\n")

    plugin = tmp_path / "plugin"
    for part in ("hooks", "tap", ".kimi-plugin", ".claude-plugin"):
        shutil.copytree(TAP / part, plugin / part, ignore=shutil.ignore_patterns("__pycache__"))
    (plugin / ".venv" / "bin").mkdir(parents=True)
    (plugin / ".venv" / "bin" / "python3").write_text(STUB_PYTHON)
    (plugin / ".venv" / "bin" / "python3").chmod(0o755)

    capture_state = tmp_path / "state"
    capture_state.mkdir()
    (capture_state / ".token").write_text("ros_ing_kimi_device_token")
    cli_config = tmp_path / "probe-config.json"
    cli_config.write_text(json.dumps({"base_url": "http://127.0.0.1:1", "ingest_token": "ros_ing_CLI"}))

    env = {
        k: v for k, v in os.environ.items()
        if k not in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "PROBE_INGEST_TOKEN", "PROBE_KIMI_TAP_TOKEN",
                     "PROBE_KIMI_SESSIONS_DIR", "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS")
    }
    env.update(
        HOME=str(home),
        KIMI_CODE_HOME=str(kimi_home),
        KIMI_PLUGIN_ROOT=str(plugin),
        PROBE_KIMI_TAP_PLUGIN_DIR=str(capture_state),
        PROBE_CONFIG_PATH=str(cli_config),
        PROBE_BASE_URL="http://127.0.0.1:1",
        XDG_STATE_HOME=str(tmp_path / "xdg-state"),
        XDG_CONFIG_HOME=str(tmp_path / "xdg-config"),
        STUB_SPAWN=str(tmp_path / "spawn.args"),
    )
    env.pop("PROBE_TAP_SOURCE", None)  # the manifest's command sets it
    ctx = {
        "sid": sid, "env": env, "plugin": plugin, "state": capture_state, "tmp": tmp_path,
        "wire": session_dir / "agents" / "main" / "wire.jsonl", "fixtures": kimi_fixtures,
    }
    yield ctx
    for suffix in (".pid", ".shutdown", ".owner", ".stopping"):
        Path(f"/tmp/probe-research-tap-watcher-{sid}{suffix}").unlink(missing_ok=True)


def _run(ctx, event: str, payload: str, *, wait: bool = False, cwd: Path | None = None):
    return subprocess.run(
        ["/bin/bash", "-c", _command(event, wait=wait)],
        input=payload, text=True, capture_output=True, env=ctx["env"],
        cwd=str(cwd or ctx["plugin"]), timeout=60,
    )


def test_session_start_finds_the_wire_from_kimi_s_index(kimi) -> None:
    result = _run(kimi, "SessionStart", _payload("SessionStart", kimi["fixtures"], kimi["sid"]))
    assert result.returncode == 0, result.stderr
    spawned = (kimi["tmp"] / "spawn.args").read_text().splitlines()
    sid, _cwd, _py, root, log, pid_file, prefix, flag, transcript = spawned
    assert sid == kimi["sid"], "Probe stores the bare UUID, never session_<uuid>"
    assert (flag, Path(transcript)) == ("--transcript", kimi["wire"])
    assert Path(root) == kimi["plugin"]
    assert Path(log) == kimi["state"] / "logs" / f"{kimi['sid']}.log"
    assert (prefix, pid_file) == ("probe-research-tap", f"/tmp/probe-research-tap-watcher-{kimi['sid']}.pid")
    # The version stamp read the Kimi manifest, the folder its registry row names.
    assert (kimi["state"] / ".installed_version").read_text() == MANIFEST["version"]


def test_session_start_never_takes_the_probe_cli_token(kimi) -> None:
    """The CLI config's ingest token is Claude Code's capture token; the
    server refuses it on Kimi's route. Only Kimi's own pairing counts."""
    (kimi["state"] / ".token").unlink()
    result = _run(kimi, "SessionStart", _payload("SessionStart", kimi["fixtures"], kimi["sid"]))
    assert result.returncode == 0, result.stderr
    assert not (kimi["tmp"] / "spawn.args").exists()
    assert "no token configured" in (kimi["state"] / "logs" / f"{kimi['sid']}.log").read_text()


def test_kimi_s_own_token_variable_is_enough(kimi) -> None:
    (kimi["state"] / ".token").unlink()
    kimi["env"]["PROBE_KIMI_TAP_TOKEN"] = "ros_ing_kimi_env_token"
    _run(kimi, "SessionStart", _payload("SessionStart", kimi["fixtures"], kimi["sid"]))
    assert (kimi["tmp"] / "spawn.args").read_text().splitlines()[0] == kimi["sid"]


def test_session_start_without_a_wire_starts_nothing(kimi) -> None:
    (kimi["tmp"] / "kimi" / "session_index.jsonl").unlink()
    shutil.rmtree(kimi["wire"].parent.parent.parent)
    result = _run(kimi, "SessionStart", _payload("SessionStart", kimi["fixtures"], kimi["sid"]))
    assert result.returncode == 0
    assert not (kimi["tmp"] / "spawn.args").exists()


def test_ensure_daemon_strips_the_prefix_and_says_nothing_to_the_model(kimi) -> None:
    """Kimi injects a UserPromptSubmit hook's stdout into the model's context,
    so the cold path's session-start output must not reach it."""
    seen = kimi["tmp"] / "session-start.stdin"
    stub = kimi["plugin"] / "hooks" / "session-start.sh"
    stub.write_text(f'#!/bin/sh\ncat >"{seen}"\nprintf \'{{"continue": true}}\\n\'\n')
    result = _run(kimi, "UserPromptSubmit", _payload("UserPromptSubmit", kimi["fixtures"], kimi["sid"]))
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert json.loads(seen.read_text())["session_id"] == kimi["sid"]
    assert (kimi["state"] / "heal" / kimi["sid"]).is_file()


def test_ensure_daemon_leaves_a_live_daemon_alone(kimi) -> None:
    seen = kimi["tmp"] / "session-start.stdin"
    (kimi["plugin"] / "hooks" / "session-start.sh").write_text(f'#!/bin/sh\ncat >"{seen}"\n')
    Path(f"/tmp/probe-research-tap-watcher-{kimi['sid']}.pid").write_text(str(os.getpid()))
    result = _run(kimi, "UserPromptSubmit", _payload("UserPromptSubmit", kimi["fixtures"], kimi["sid"]))
    assert result.returncode == 0
    assert not seen.exists()


def test_turn_end_marks_the_canonical_session_at_the_wire_s_size(kimi) -> None:
    sessions = kimi["tmp"] / "xdg-state" / "probe" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{kimi['sid']}.state").write_text("daemon")
    result = _run(kimi, "Stop", _payload("Stop", kimi["fixtures"], kimi["sid"]))
    assert result.returncode == 0 and result.stdout == ""
    turn = json.loads((sessions / f"{kimi['sid']}.turn").read_text())
    assert turn["offset"] == kimi["wire"].stat().st_size


def _fake_daemon(sid: str, linger_s: float) -> subprocess.Popen:
    proc = subprocess.Popen(
        ["bash", "-c", f'trap "sleep {linger_s}; exit 0" TERM; while :; do sleep 0.05; done'],
        start_new_session=True,
    )
    Path(f"/tmp/probe-research-tap-watcher-{sid}.pid").write_text(str(proc.pid))
    time.sleep(0.3)
    return proc


def test_session_end_stops_the_canonical_session_and_waits_its_full_budget(kimi) -> None:
    """Kimi awaits its SessionEnd hooks up to the entry's timeout, so the
    `--wait` entry waits for the FINALIZE past Claude Code's 1.5s budget."""
    proc = _fake_daemon(kimi["sid"], linger_s=2.5)
    try:
        started = time.monotonic()
        _run(kimi, "SessionEnd", _payload("SessionEnd", kimi["fixtures"], kimi["sid"]), wait=True)
        elapsed = time.monotonic() - started
        assert Path(f"/tmp/probe-research-tap-watcher-{kimi['sid']}.shutdown").is_file()
        assert proc.wait(timeout=5) == 0
        assert elapsed >= 2.2, f"returned after {elapsed:.2f}s, before the daemon finished"
    finally:
        with contextlib.suppress(OSError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)


# --- the manifest ------------------------------------------------------------------

#: Kimi Code 2.1.1 validates each plugin hook entry against exactly these keys.
_KIMI_HOOK_KEYS = {"event", "matcher", "command", "timeout"}


def test_the_kimi_manifest_runs_the_same_scripts_on_the_same_events() -> None:
    """Parity with hooks/hooks.json, which Kimi never reads (and whose bytes
    Codex trusts by hash): one Kimi entry per hooks.json entry, same script,
    same flags, same timeout."""
    shared = json.loads((TAP / "hooks" / "hooks.json").read_text())["hooks"]
    expected = sorted(
        (event, hook["command"].rsplit("/hooks/", 1)[1].rstrip("'"), hook["timeout"])
        for event, groups in shared.items()
        for group in groups
        for hook in group["hooks"]
    )
    actual = sorted(
        # Kimi's copies silence stdout (Kimi would inject Claude's JSON into
        # the model's context); the script and its flags are what must match.
        (h["event"], h["command"].rsplit("/hooks/", 1)[1].rstrip("'").removesuffix(" >/dev/null"), h["timeout"])
        for h in MANIFEST["hooks"]
    )
    assert [(e, s.replace('"', ""), t) for e, s, t in actual] == [
        (e, s.replace('"', ""), t) for e, s, t in expected
    ]
    for hook in MANIFEST["hooks"]:
        assert set(hook) <= _KIMI_HOOK_KEYS, hook
        assert "PROBE_TAP_SOURCE=kimi_code" in hook["command"]


def test_the_kimi_manifest_ships_the_tap_s_version() -> None:
    claude = json.loads((TAP / ".claude-plugin" / "plugin.json").read_text())
    assert MANIFEST["name"] == claude["name"] == "probe-research-tap"
    assert MANIFEST["version"] == claude["version"]


# --- hook-env: the values and payload the scripts eval ---------------------------


def _hook_env(*args: str, stdin: str = "", env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", "-m", "tap", "hook-env", *args],
        input=stdin, text=True, capture_output=True, cwd=str(TAP),
        env={**os.environ, "PYTHONPATH": str(TAP), **(env or {})}, timeout=30,
    )


@pytest.mark.parametrize("source", ["pi", "cursor", "nonsense"])
def test_hook_env_knows_only_captured_hook_plugin_harnesses(source) -> None:
    result = _hook_env("--source", source, "--payload", stdin='{"session_id": "x"}')
    assert (result.returncode, result.stdout) == (hook_env.EXIT_UNKNOWN_SOURCE, "")


def test_hook_env_output_is_safe_to_eval(tmp_path) -> None:
    """The scripts `eval` this: a payload that tries to run a command must
    come back as inert data."""
    canary = tmp_path / "pwned"
    hostile = json.dumps({
        "session_id": f"session_{uuid.uuid4()}'; touch {canary}; '",
        "transcript_path": f"$(touch {canary})",
        "cwd": "`touch " + str(canary) + "`",
    })
    out = _hook_env("--source", "kimi_code", "--payload", stdin=hostile,
                    env={"PROBE_KIMI_TAP_PLUGIN_DIR": str(tmp_path / "s")}).stdout
    script = out + 'printf "%s" "$TAP_HOOK_INPUT"\n'
    echoed = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True, timeout=10)
    assert not canary.exists()
    assert json.loads(echoed.stdout)["transcript_path"] == f"$(touch {canary})"
