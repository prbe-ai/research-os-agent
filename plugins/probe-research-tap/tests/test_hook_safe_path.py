"""No program a tap hook starts loads anything from the session's folder (the agent's workspace).

Claude Code, Codex and Kimi Code run these hooks outside the agent's sandbox, in the session's folder,
which the agent can write. `python3 -c`, `python3 -` and `python3 -m tap` put the folder they start in
first on Python's path, and an empty or relative PATH or PYTHONPATH entry names it too, so a planted
`json.py`, `sitecustomize.py`, `tap/` package or `python3` ran with the hook's permissions. Each hook
first drops those entries, makes relative path variables absolute against the session's folder, and
moves to the tap's own folder (`hooks/session-folder.sh`); the session's folder stays data.

These drive the real scripts from a session folder holding every plant, with a stand-in `tap` package
in the plugin's own folder that logs each call (its arguments and the folder it ran in).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parents[1] / "hooks"
SCRIPTS = ("session-start.sh", "ensure-daemon.sh", "turn-end.sh", "session-end.sh")
PREFIX = "probe-research-tap"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX shell")

#: What the stand-in tap logs of its environment.
LOGGED_ENV = ("PATH", "NIX_PATH", "PROBE_CONFIG_PATH", "CODEX_HOME", "FOO_DIR")
#: The stand-in tap: logs its call, prints nothing (`hook-env`: no registry row), succeeds.
STAND_IN = (
    "import json, os, sys\n"
    "with open(os.environ['STAND_IN_LOG'], 'a') as handle:\n"
    "    handle.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd(),\n"
    f"                             'env': {{k: os.environ.get(k) for k in {LOGGED_ENV!r}}}}}) + '\\n')\n"
)


@pytest.fixture
def setup(tmp_path):
    """The real hooks in a plugin folder with the stand-in tap, a session folder the agent planted a
    `json.py` and a `tap/` package in, and an isolated home."""
    root = tmp_path / "plugin"
    shutil.copytree(HOOKS, root / "hooks")
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "0.10.59"}))
    (root / "tap").mkdir()
    (root / "tap" / "__init__.py").write_text("")
    (root / "tap" / "__main__.py").write_text(STAND_IN)
    # The hand-over's switch check (installed-tap.sh `tap_yields_to_shim`): logged, never yields.
    (root / "tap" / "hook_switches.py").write_text(STAND_IN + "sys.exit(1)\n")
    work, marker = tmp_path / "work", tmp_path / "planted-ran"
    for folder in ("tap", "lib", "bin"):
        (work / folder).mkdir(parents=True)
    for planted in ("json.py", "sitecustomize.py", "tap/__init__.py", "tap/__main__.py", "lib/json.py",
                    "lib/sitecustomize.py"):
        (work / planted).write_text(f"open({str(marker)!r}, 'a').write('{planted} ran\\n')\n")
    (work / "bin" / "python3").write_text(f"#!/bin/sh\necho 'bin/python3 ran' >>'{marker}'\nexit 1\n")
    (work / "bin" / "python3").chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "watch").mkdir()
    (tmp_path / "config.json").write_text(json.dumps({"ingest_token": "probe_ing_test"}))
    keep = ("PATH", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env.update({
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(tmp_path / "state"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"), "PROBE_TELEMETRY": "off",
        "PROBE_CONFIG_PATH": str(tmp_path / "config.json"),
        "PROBE_RESEARCH_TAP_PLUGIN_DIR": str(tmp_path / "claude-state"),
        "PROBE_KIMI_TAP_PLUGIN_DIR": str(tmp_path / "kimi-state"),
        "PROBE_TAP_WATCHER_DIR": str(tmp_path / "watch"), "STAND_IN_LOG": str(tmp_path / "stand-in.log"),
        "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS": "5000",
    })
    return root, work, marker, env, tmp_path


def _sid() -> str:
    return str(uuid.uuid4())


def _hook(setup, script: str, payload: dict, *args: str, source: str = "claude_code",
          **extra: str) -> subprocess.CompletedProcess:
    """The hook as its harness runs it: by its absolute path, in the session's folder."""
    root, work, _, env, _ = setup
    env = {**env, "PROBE_TAP_SOURCE": source, **extra}
    if source == "claude_code":
        env["CLAUDE_PLUGIN_ROOT"] = str(root)
    return subprocess.run(["/bin/bash", str(root / "hooks" / script), *args], input=json.dumps(payload),
                          capture_output=True, text=True, env=env, cwd=str(work), timeout=60)


def _calls(setup, until: str | None = None) -> list[dict]:
    """The stand-in tap's calls; `until`: wait (up to 10 s) for one of that command, or a plant's run."""
    _, _, marker, _, tmp = setup
    log = tmp / "stand-in.log"
    deadline = time.monotonic() + (10 if until else 0)
    while True:
        calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        if until is None or any(c["argv"][:1] == [until] for c in calls) or marker.exists():
            return calls
        if time.monotonic() > deadline:
            return calls
        time.sleep(0.05)


def _assert_nothing_planted_ran(setup, done: subprocess.CompletedProcess | None = None) -> None:
    root, _, marker, _, _ = setup
    assert not marker.exists(), (marker.read_text(), done.stderr[-2000:] if done else "")
    for call in _calls(setup):
        assert call["cwd"] == str(root), f"the tap ran in {call['cwd']}, not its own folder"


@pytest.mark.parametrize("names_folder", [True, False], ids=["payload-cwd", "no-payload-cwd"])
def test_session_start_never_runs_a_file_planted_in_the_session_folder(setup, names_folder):
    """Every Python start of a session start: the payload's fields (`python3 -c`), the sign-in check
    (`python3 -`), `tap owner`, `tap record` and `tap redaction-notice` (`-m tap`). The session's
    folder still reaches `tap record`, from the payload or, naming none, from where the hook began."""
    _, work, _, _, tmp = setup
    sid = _sid()
    transcript = tmp / f"{sid}.jsonl"
    transcript.write_text("")
    payload = {"session_id": sid, "transcript_path": str(transcript), "hook_event_name": "SessionStart"}
    if names_folder:
        payload["cwd"] = str(work)
    done = _hook(setup, "session-start.sh", payload)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.splitlines()[-1])["continue"] is True, "the hook still answers"
    _assert_nothing_planted_ran(setup, done)
    calls = _calls(setup)
    assert [c["argv"][0] for c in calls] == ["owner", "record", "redaction-notice"], "the sign-in check passed"
    record = calls[1]["argv"]
    assert record[record.index("--cwd") + 1] == str(work), "the session's folder, as data"


def test_the_prompts_heal_never_runs_a_file_planted_in_the_session_folder(setup):
    """ensure-daemon.sh parses a payload its pattern cannot read (`python3 -c`), then asks again
    through session-start.sh, which still finds the session's folder (the payload names none)."""
    _, work, _, _, tmp = setup
    sid = _sid()
    transcript = tmp / f"{sid}.jsonl"
    transcript.write_text("")
    payload = {"session_id": sid, "transcript_path": str(transcript), "tool_input": {"session_id": "other"}}
    done = _hook(setup, "ensure-daemon.sh", payload)
    assert done.returncode == 0, done.stderr
    _assert_nothing_planted_ran(setup, done)
    record = next(c["argv"] for c in _calls(setup) if c["argv"][0] == "record")
    assert record[record.index("--session-id") + 1] == sid
    assert record[record.index("--cwd") + 1] == str(work), "the session's folder reached the heal's ask"


def test_a_turn_end_never_runs_a_file_planted_in_the_session_folder(setup):
    """turn-end.sh writes the turn's offset (`python3 -c`) and starts a gone service again
    (`nohup python3 -m tap service ensure`, in the background)."""
    _, _, _, env, tmp = setup
    sid = _sid()
    transcript = tmp / f"{sid}.jsonl"
    transcript.write_text("{}\n")
    sessions = Path(env["XDG_STATE_HOME"]) / "probe" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{sid}.state").write_text("daemon\n")
    dead = subprocess.Popen(["true"])
    dead.wait()
    (tmp / "watch" / f"{PREFIX}-watcher-{sid}.hosted").write_text(str(dead.pid))
    done = _hook(setup, "turn-end.sh", {"session_id": sid, "transcript_path": str(transcript)})
    assert done.returncode == 0, done.stderr
    calls = _calls(setup, until="service")
    _assert_nothing_planted_ran(setup, done)
    assert [c["argv"][:2] for c in calls] == [["service", "ensure"]]
    assert (sessions / f"{sid}.turn").exists(), "the turn's offset was written"


def test_a_session_end_never_runs_a_file_planted_in_the_session_folder(setup):
    """session-end.sh parses the payload (`python3 -S -c`) and waits on `python3 -m tap finish`."""
    _, _, _, _, tmp = setup
    sid = _sid()
    (tmp / "watch" / f"{PREFIX}-watcher-{sid}.hosted").write_text(str(os.getpid()))
    done = _hook(setup, "session-end.sh", {"session_id": sid, "reason": "other"}, "--wait")
    assert done.returncode == 0, done.stderr
    _assert_nothing_planted_ran(setup, done)
    assert [c["argv"][0] for c in _calls(setup)] == ["finish"]


@pytest.mark.parametrize("script", SCRIPTS)
def test_a_registry_harnesss_hook_never_runs_a_tap_planted_in_the_session_folder(setup, script):
    """`python3 -m tap hook-env` (every hook, for a harness the registry describes: Kimi Code) put the
    session's folder ahead of the tap's PYTHONPATH, so a planted `tap/` ran in the tap's place."""
    _, work, _, _, _ = setup
    done = _hook(setup, script, {"session_id": _sid(), "cwd": str(work), "hook_event_name": "Stop"},
                 source="kimi_code")
    assert done.returncode == 0, done.stderr
    _assert_nothing_planted_ran(setup, done)
    assert [c["argv"][0] for c in _calls(setup)][:1] == ["hook-env"]


def test_relative_paths_keep_meaning_the_session_folder(setup):
    """The payload's folder and transcript, PROBE_CONFIG_PATH and the state folder, named relatively,
    mean the session's folder's, as before the hook moved to the tap's: the sign-in check reads that
    config, the log lands in that state folder, and `tap record` gets both paths whole."""
    _, work, _, _, _ = setup
    sid = _sid()
    (work / "t.jsonl").write_text("")
    (work / "config.json").write_text(json.dumps({"ingest_token": "probe_ing_test"}))
    payload = {"session_id": sid, "transcript_path": "t.jsonl", "cwd": ".", "hook_event_name": "SessionStart"}
    done = _hook(setup, "session-start.sh", payload, PROBE_CONFIG_PATH="config.json",
                 PROBE_RESEARCH_TAP_PLUGIN_DIR="state")
    assert done.returncode == 0, done.stderr
    _assert_nothing_planted_ran(setup, done)
    record = next(c["argv"] for c in _calls(setup) if c["argv"][0] == "record")
    assert record[record.index("--cwd") + 1] == str(work)
    assert record[record.index("--transcript") + 1] == str(work / "t.jsonl")
    assert (work / "state" / "logs" / f"{sid}.log").exists(), "the state folder is the session folder's"


def test_a_turn_end_reads_a_relative_transcript_in_the_session_folder(setup):
    _, work, _, env, _ = setup
    sid = _sid()
    (work / "t.jsonl").write_text("{}\n{}\n")
    sessions = Path(env["XDG_STATE_HOME"]) / "probe" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{sid}.state").write_text("daemon\n")
    done = _hook(setup, "turn-end.sh", {"session_id": sid, "transcript_path": "t.jsonl"})
    assert done.returncode == 0, done.stderr
    assert json.loads((sessions / f"{sid}.turn").read_text())["offset"] == 6


@pytest.mark.parametrize("script", SCRIPTS)
def test_relative_search_entries_never_reach_a_hook(setup, script):
    """An empty or relative PATH entry found a planted `python3` in the session's folder (the switch
    check ran one before the hook left it), and a relative PYTHONPATH entry, made absolute against it at
    Python's start, ran a planted `sitecustomize.py`. Every hook drops both before any program starts."""
    _, work, _, env, tmp = setup
    ledger = Path(env["HOME"]) / ".local" / "state" / "probe" / "service" / "machine" / "hooks"
    ledger.mkdir(parents=True, exist_ok=True)
    (ledger / "migrations.json").write_text("{}")
    sid = _sid()
    (tmp / "t.jsonl").write_text("")
    payload = {"session_id": sid, "transcript_path": str(tmp / "t.jsonl"), "cwd": str(work), "reason": "other"}
    done = _hook(setup, script, payload, PATH=f"bin::{env['PATH']}", PYTHONPATH=".:lib")
    assert done.returncode == 0, done.stderr
    _assert_nothing_planted_ran(setup, done)
    assert any(c["argv"][:1] == ["yield"] for c in _calls(setup)), "the switch check ran, on a safe python3"


def test_off_windows_a_windows_looking_entry_is_relative(setup):
    """On Linux and macOS an entry is absolute only if it starts with `/`: `\\bin` and `\\\\host\\bin` name
    folders in the folder the hook started in (and `C:/lib` is two entries, `C` and `/lib`). An OSTYPE inherited from a Windows shell (`msys`) does
    not change that: `uname` decides."""
    _, work, _, env, tmp = setup
    sid = _sid()
    (tmp / "t.jsonl").write_text("")
    payload = {"session_id": sid, "transcript_path": str(tmp / "t.jsonl"), "cwd": str(work)}
    done = _hook(setup, "session-start.sh", payload, OSTYPE="msys",
                 PATH=f"\\bin:\\\\host\\bin:bin:{env['PATH']}", PYTHONPATH="C:/lib;\\\\host\\lib")
    assert done.returncode == 0, done.stderr
    _assert_nothing_planted_ran(setup, done)
    for call in _calls(setup):
        assert all(entry.startswith("/") for entry in call["env"]["PATH"].split(":")), call["env"]["PATH"]


def test_only_the_taps_own_path_variables_are_made_absolute(setup):
    """A listed variable naming one path relatively becomes absolute against the session's folder,
    whole (a name may hold `:`); a `~` path, an absolute one and any variable not on the list (a
    NIX_PATH binding, a `*_DIR` the tap never reads) reach the tap byte-identical."""
    _, work, _, _, tmp = setup
    sid = _sid()
    (tmp / "t.jsonl").write_text("")
    (work / "a:b.json").write_text(json.dumps({"ingest_token": "probe_ing_test"}))
    nix = "nixpkgs=channels/nixpkgs:home-manager=relative/hm:./x"
    payload = {"session_id": sid, "transcript_path": str(tmp / "t.jsonl"), "cwd": str(work)}
    done = _hook(setup, "session-start.sh", payload, PROBE_CONFIG_PATH="a:b.json", NIX_PATH=nix,
                 CODEX_HOME="~/codex", FOO_DIR="relative")
    assert done.returncode == 0, done.stderr
    record = next(c for c in _calls(setup) if c["argv"][0] == "record")
    assert record["env"]["PROBE_CONFIG_PATH"] == str(work / "a:b.json"), "read whole, and the sign-in check passed"
    assert (record["env"]["NIX_PATH"], record["env"]["CODEX_HOME"], record["env"]["FOO_DIR"]) == (
        nix, "~/codex", "relative")


@pytest.mark.parametrize("handed", ["", "relative/folder", "."])
def test_a_relative_or_empty_session_folder_handed_over_is_ignored(setup, handed):
    """TAP_SESSION_DIR (the previous script's session folder) counts only when absolute; otherwise
    the folder this hook started in is the session's, and nothing is joined to it."""
    _, work, _, _, tmp = setup
    sid = _sid()
    (tmp / "t.jsonl").write_text("")
    done = _hook(setup, "session-start.sh", {"session_id": sid, "transcript_path": str(tmp / "t.jsonl")},
                 TAP_SESSION_DIR=handed)
    assert done.returncode == 0, done.stderr
    record = next(c["argv"] for c in _calls(setup) if c["argv"][0] == "record")
    assert record[record.index("--cwd") + 1] == str(work)


def test_every_hook_leaves_the_session_folder_before_anything_else():
    """The guard behind the tests above: each hook's first command sources hooks/session-folder.sh and
    leaves the session's folder (before the installed copy's hand-over and any program), and that file
    starts no program of its own but `/usr/bin/uname`, asked only where OSTYPE claims Windows."""
    first = '[ -f "${BASH_SOURCE[0]%/*}/session-folder.sh" ] && . "${BASH_SOURCE[0]%/*}/session-folder.sh" \\'
    for name in SCRIPTS:
        lines = (HOOKS / name).read_text(encoding="utf-8").splitlines()
        code = [line for line in lines[1:] if line.strip() and not line.lstrip().startswith("#")]
        assert code[0] == first and "tap_leave_session_folder" in code[1], name
    helper = (HOOKS / "session-folder.sh").read_text(encoding="utf-8")
    body = "\n".join(line for line in helper.splitlines() if not line.lstrip().startswith("#"))
    assert re.findall(r"\$\(([^)]*)\)", body) == ["/usr/bin/uname -s 2>/dev/null"]
    assert not re.search(r"`|python|exec |command -v", body), "session-folder.sh starts no other program"
