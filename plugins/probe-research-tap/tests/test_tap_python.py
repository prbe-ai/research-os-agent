"""The interpreter the hooks run the tap on (hooks/tap-python.sh).

The tap needs Python 3.11 or newer, and its hooks ran it on the first `python3`
on PATH. On a stock Mac that is Apple's 3.9: every `python3 -m tap` (owner,
record, watch, finish) died on `ImportError: cannot import name 'StrEnum'`, the
hooks swallowed it, and the machine recorded nothing, with no sign anywhere but
its own session logs (one team, 2026-10-20..23: three days, no session).

These drive the real scripts. `python3` is a stand-in for that Mac's (it reports
3.9.6 and its enum has no StrEnum), or a real 3.9/3.10 where this machine has one.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

TAP = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX shell")

#: What the tap's hooks call besides python: just enough for a PATH that holds
#: no python of its own.
_TOOLS = (
    "awk", "basename", "bash", "cat", "chmod", "cut", "date", "dirname", "env", "find",
    "grep", "head", "id", "kill", "ls", "mkdir", "mktemp", "mv", "nohup", "ps", "rm", "sed",
    "seq", "sh", "sleep", "stat", "tail", "timeout", "touch", "tr", "uname", "wc",
)

#: `python3` as a stock Mac has it, to the tap: 3.9.6, and an enum without
#: StrEnum (new in 3.11) wherever the tap itself runs (`-m`), so the tap dies on
#: the ImportError it dies on there. The hooks' own `-c` snippets run as on 3.9.
_OLD_PYTHON3 = """#!/bin/sh
PRELUDE='import sys; sys.version_info = (3, 9, 6, "final", 0)'
S=""
if [ "$1" = "-S" ]; then S="-S"; shift; fi
case "$1" in
    -c) CODE="$2"; shift 2; exec "{real}" $S -c "$PRELUDE
$CODE" "$@" ;;
    -m) MOD="$2"; shift 2; exec "{real}" $S -c "$PRELUDE
import enum, runpy
del enum.StrEnum
runpy.run_module('$MOD', run_name='__main__', alter_sys=True)" "$@" ;;
esac
exec "{real}" $S "$@"
"""


#: How the hooks name an old python3 to the researcher.
_OLD = re.compile(r"python3 3\.(9|10)\.\d+")


def _real_old_pythons() -> list[str]:
    found = []
    for name in ("python3.9", "python3.10"):
        path = shutil.which(name)
        if path is None and shutil.which("uv"):
            done = subprocess.run(
                ["uv", "python", "find", "--no-project", name.removeprefix("python")],
                capture_output=True, text=True, timeout=30, check=False,
            )
            path = done.stdout.strip() if done.returncode == 0 else None
        if path and os.access(path, os.X_OK):
            found.append(path)
    return found


@pytest.fixture(params=["stand-in", *_real_old_pythons()])
def old_python3(request, tmp_path) -> Path:
    """A folder whose `python3` is below 3.11."""
    folder = tmp_path / "old-python"
    folder.mkdir()
    python3 = folder / "python3"
    if request.param == "stand-in":
        python3.write_text(_OLD_PYTHON3.format(real=sys.executable))
        python3.chmod(0o755)
    else:
        python3.symlink_to(request.param)
    return folder


@pytest.fixture
def machine(tmp_path, old_python3):
    """A copy of this tap and a HOME of its own, paired, with a transcript, on a
    PATH whose only python is the old `python3`."""
    root = tmp_path / "plugin"
    shutil.copytree(TAP, root, ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".venv"))
    home = tmp_path / "home"
    state = tmp_path / "state"
    (state / "logs").mkdir(parents=True)
    (state / ".token").write_text("ros_ing_faketoken", encoding="utf-8")
    (state / ".config").write_text(json.dumps({"api_base_url": "http://127.0.0.1:1"}), encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in _TOOLS:
        found = shutil.which(name)
        if found:
            (tools / name).symlink_to(found)
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(json.dumps({"type": "user", "message": {"content": "hi"}}) + "\n")
    session_id = f"pytest-py-{os.getpid()}-{time.monotonic_ns()}"
    env = {
        "HOME": str(home),
        "PATH": f"{old_python3}{os.pathsep}{tools}",
        "CLAUDE_PLUGIN_ROOT": str(root),
        "PROBE_RESEARCH_TAP_PLUGIN_DIR": str(state),
        "PROBE_TRANSCRIPT_STATE_DIR": str(tmp_path / "journal"),
        "PROBE_RESEARCH_TAP_PROJECTS_DIR": str(tmp_path / "projects"),
        "PROBE_TAP_HOSTED": "0",
        "PROBE_TELEMETRY": "off",
    }
    yield {"root": root, "home": home, "state": state, "env": env, "sid": session_id, "transcript": transcript}
    pid_file = Path(f"/tmp/probe-research-tap-watcher-{session_id}.pid")
    try:
        pid = int(pid_file.read_text().strip())
        os.killpg(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass
    for suffix in (".pid", ".shutdown", ".owner", ".stopping", ".version", ".hosted"):
        Path(f"/tmp/probe-research-tap-watcher-{session_id}{suffix}").unlink(missing_ok=True)


def _cli_python(home: Path, python: str) -> None:
    """The Probe CLI's interpreter, where the CLI records it at every start."""
    runtime = home / ".local" / "state" / "probe" / "service" / "runtime.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(json.dumps({"python": python, "cli_version": "0.296.1"}))


def _session_start(m) -> dict:
    done = subprocess.run(
        [shutil.which("bash"), str(m["root"] / "hooks" / "session-start.sh")],
        input=json.dumps({"session_id": m["sid"], "transcript_path": str(m["transcript"]), "cwd": "/tmp"}),
        text=True, capture_output=True, env=m["env"], timeout=60, check=True,
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


def _watch_commands(session_id: str, timeout: float = 20.0) -> list[str]:
    """The live `tap watch` processes of this session, by command line."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        listing = subprocess.run(["ps", "-ww", "-eo", "command="], capture_output=True, text=True).stdout
        # The wrapper's own command line names `-m tap watch` too, before "$SID".
        found = [line for line in listing.splitlines() if f"-m tap watch --session-id {session_id} " in line]
        if found:
            return found
        time.sleep(0.2)
    return []


def test_an_old_python3_runs_the_tap_on_the_probe_cli_interpreter(machine) -> None:
    """THE regression: python3 on PATH is 3.9, the Probe CLI's interpreter is new
    enough. The tap must run there, not crash-loop on python3."""
    _cli_python(machine["home"], sys.executable)
    (machine["state"] / ".python-missing").write_text("python3 3.9.6\n")

    answer = _session_start(machine)

    assert answer == {"continue": True}
    watches = _watch_commands(machine["sid"])
    assert watches, "no `tap watch` is running: the tap died on the old python3"
    assert all(line.startswith(f"{sys.executable} -m tap watch") for line in watches), watches
    time.sleep(1.5)
    assert _watch_commands(machine["sid"], timeout=1), "the tap did not stay up"
    log = (machine["state"] / "logs" / f"{machine['sid']}.log").read_text()
    assert "StrEnum" not in log
    assert not (machine["state"] / ".python-missing").exists(), "a stale verdict outlived a found interpreter"
    assert (machine["state"] / ".python").read_text().strip() == sys.executable


def test_with_no_python_new_enough_the_researcher_is_told_once_a_session(machine) -> None:
    """Nothing to run the tap on: say so where the researcher reads it, once a
    session (ensure-daemon.sh runs this hook again), and leave it for `probe doctor`."""
    first = _session_start(machine)
    again = _session_start(machine)

    message = first.get("systemMessage", "")
    assert "not recording this session" in message
    assert "Python 3.11 or newer" in message and _OLD.search(message), message
    assert again == {"continue": True}
    assert _OLD.fullmatch((machine["state"] / ".python-missing").read_text().strip())
    assert not Path(f"/tmp/probe-research-tap-watcher-{machine['sid']}.pid").exists()
    log = (machine["state"] / "logs" / f"{machine['sid']}.log").read_text()
    assert re.search(r"no Python 3\.11 or newer found \(python3 3\.(9|10)\.\d+\)", log), log


def _recording_python(tmp_path: Path, calls: Path, on_finish: str = "exit 0") -> Path:
    """An interpreter the tap runs on that notes each `tap finish` it is asked to run."""
    python = tmp_path / "cli-python"
    python.write_text(
        "#!/bin/sh\n"
        f'case "$*" in *"tap finish"*) echo $$ "$@" >"{calls}"; {on_finish} ;; esac\n'
        f'exec "{sys.executable}" "$@"\n'
    )
    python.chmod(0o755)
    return python


def _session_end(m, **env) -> float:
    started = time.monotonic()
    subprocess.run(
        [shutil.which("bash"), str(m["root"] / "hooks" / "session-end.sh"), "--wait"],
        input=json.dumps({"session_id": m["sid"], "reason": "other"}),
        text=True, capture_output=True, env={**m["env"], **env}, timeout=30, check=True,
    )
    return time.monotonic() - started


def test_session_end_finishes_a_hosted_session_on_the_interpreter_session_start_chose(machine, tmp_path) -> None:
    """The Probe service records the session (its `.hosted` marker): `tap finish`
    waits for its last pass, so it too must run where the tap can. SessionStart
    remembers its choice in the tap's state folder; SessionEnd reads it."""
    calls = tmp_path / "finish.args"
    cli = _recording_python(tmp_path, calls)
    _cli_python(machine["home"], str(cli))
    _session_start(machine)
    assert (machine["state"] / ".python").read_text().strip() == str(cli)

    Path(f"/tmp/probe-research-tap-watcher-{machine['sid']}.hosted").write_text(str(os.getpid()))
    _session_end(machine, CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS="4000")

    assert calls.is_file(), "`tap finish` never ran on an interpreter the tap runs on"
    assert calls.read_text().split()[1:5] == ["-m", "tap", "finish", "--session-id"]


def test_a_remembered_interpreter_that_hangs_still_ends_inside_the_exit_budget(machine, tmp_path) -> None:
    """SessionEnd starts no python to choose (Claude Code cancels the hook at
    1.5 s), and a `tap finish` that never returns is stopped at the deadline."""
    calls = tmp_path / "finish.args"
    cli = _recording_python(tmp_path, calls, on_finish="exec sleep 60")
    (machine["state"] / ".python").write_text(f"{cli}\n")
    Path(f"/tmp/probe-research-tap-watcher-{machine['sid']}.hosted").write_text(str(os.getpid()))

    elapsed = _session_end(machine)

    assert elapsed < 1.5, f"took {elapsed:.2f}s; Claude Code cancels at 1.5s"
    pid = int(calls.read_text().split()[0])
    deadline = time.time() + 2
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the hook left `tap finish` running")


def test_a_relative_path_entry_is_remembered_absolute(machine, tmp_path) -> None:
    """`command -v python3` answers `rel/bin/python3` for a relative PATH entry.
    Remembered as is, a SessionEnd in another folder would run another python,
    or none: the choice is remembered absolute (symlinks kept)."""
    project, elsewhere = tmp_path / "project", tmp_path / "elsewhere"
    (project / "rel" / "bin").mkdir(parents=True)
    elsewhere.mkdir()
    calls = tmp_path / "finish.args"
    shim = _recording_python(tmp_path, calls)
    (project / "rel" / "bin" / "python3").symlink_to(shim)
    env = {**machine["env"], "PATH": f"rel/bin{os.pathsep}{machine['env']['PATH']}"}
    payload = {"session_id": machine["sid"], "transcript_path": str(machine["transcript"]), "cwd": str(project)}
    subprocess.run(
        [shutil.which("bash"), str(machine["root"] / "hooks" / "session-start.sh")],
        input=json.dumps(payload), text=True, capture_output=True, env=env, cwd=project, timeout=60, check=True,
    )
    assert (machine["state"] / ".python").read_text().strip() == str(project / "rel" / "bin" / "python3")

    Path(f"/tmp/probe-research-tap-watcher-{machine['sid']}.hosted").write_text(str(os.getpid()))
    subprocess.run(
        [shutil.which("bash"), str(machine["root"] / "hooks" / "session-end.sh"), "--wait"],
        input=json.dumps({"session_id": machine["sid"], "reason": "other"}), text=True, capture_output=True,
        env={**env, "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS": "4000"}, cwd=elsewhere, timeout=30, check=True,
    )
    assert calls.is_file(), "SessionEnd in another folder did not run the interpreter SessionStart chose"


def test_a_relative_remembered_path_is_never_run(tmp_path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / ".python").write_text("rel/bin/python3\n")
    (tmp_path / "rel" / "bin").mkdir(parents=True)
    (tmp_path / "rel" / "bin" / "python3").symlink_to(sys.executable)
    done = subprocess.run(
        [shutil.which("bash"), "-c", f'. "{TAP}/hooks/tap-python.sh"; tap_python_cached "{state}"'],
        capture_output=True, text=True, cwd=tmp_path, timeout=30, check=False,
    )
    assert (done.returncode, done.stdout) == (1, "")


def _helper(script: str, env: dict) -> str:
    done = subprocess.run(
        [shutil.which("bash"), "-c", f'set -euo pipefail; . "{TAP}/hooks/tap-python.sh"; {script}'],
        capture_output=True, text=True, env=env, timeout=30, check=True,
    )
    return done.stdout


@pytest.mark.parametrize("case", ["claude", "claude-override", "codex", "codex-override", "codex-legacy"])
def test_session_end_reads_the_state_folder_the_tap_writes(tmp_path, case) -> None:
    """`tap_state_dir` is session-start.sh's folder, which is tap/config.py's."""
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": os.environ["PATH"]}
    source = "codex" if case.startswith("codex") else "claude_code"
    if case == "claude-override":
        env["PROBE_RESEARCH_TAP_PLUGIN_DIR"] = str(tmp_path / "elsewhere")
    if case == "codex-override":
        env["PRBE_CODEX_TAP_PLUGIN_DIR"] = str(tmp_path / "elsewhere")
    if case == "codex-legacy":
        (home / ".codex" / "state" / "prbe-codex-tap-plugin").mkdir(parents=True)
    expected = subprocess.run(
        [sys.executable, "-c", "from tap import config; print(config.plugin_dir())"],
        capture_output=True, text=True, check=True, timeout=30,
        env={**env, "PROBE_TAP_SOURCE": source, "PYTHONPATH": str(TAP)},
    ).stdout.strip()
    assert _helper(f"tap_state_dir {source}", env).strip() == expected


def test_a_windows_runtime_record_is_read_as_git_bash_runs_it(tmp_path) -> None:
    """On Git Bash the CLI's interpreter is under %LOCALAPPDATA% / %APPDATA%, and
    runtime.json doubles each backslash (tap/service/paths.py)."""
    local, roaming = tmp_path / "Local", tmp_path / "Roaming"
    runtime = local / "probe" / "service" / "runtime.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(json.dumps({"python": "C:\\Users\\Ada Lovelace\\probe\\python.exe", "cli_version": "1"}))
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], "LOCALAPPDATA": str(local), "APPDATA": str(roaming)}
    # No cygpath here, so each path comes back as Windows wrote it.
    out = _helper("OSTYPE=msys; tap_cli_pythons", env).splitlines()
    assert out == [
        "C:\\Users\\Ada Lovelace\\probe\\python.exe",
        f"{roaming}/uv/tools/probe-research/Scripts/python.exe",
    ]


def test_a_runtime_record_with_an_escape_falls_back_to_the_uv_interpreter(tmp_path) -> None:
    runtime = tmp_path / ".local" / "state" / "probe" / "service" / "runtime.json"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(json.dumps({"python": "/Users/jos\u00e9/python", "cli_version": "1"}))
    out = _helper("tap_cli_pythons", {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}).splitlines()
    assert out == [f"{tmp_path}/.local/share/uv/tools/probe-research/bin/python"]
