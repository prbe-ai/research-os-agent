"""The SessionStart/SessionEnd shell hooks: what is left of the daemon lifecycle once the Probe service
records every session (service-only task 15: no wrapper, no `tap watch` of a hook's own, no signal).

Pinned here: session-end never signals anything (a recycled pid's group included), the waiting entry's
`tap finish` stays inside the harness's budget (Claude Code's exit budget, Codex's 3s clamp, the 15s
cap), stale sentinels are pruned, and the hooks leave no process behind. These drive the real scripts,
not a reimplementation.

Retired with the wrappers: `test_the_waiting_entry_finds_the_daemon_after_its_sibling_ran` (the waiting
entry found a wrapper's group its flagless sibling had already signalled, and waited on it). Nothing
signals or waits on a wrapper's group any more: both entries leave the session to the service, and the
waiting one waits on `tap finish` (the budget tests above).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
SESSION_START = HOOKS / "session-start.sh"
SESSION_END = HOOKS / "session-end.sh"

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="the hooks are POSIX shell + process groups"
)


def _pgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError):
        return None


def _gone(pid: int, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline and _alive(pid):
        time.sleep(0.2)
    return not _alive(pid)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


@pytest.fixture
def tap_env(tmp_path: Path):
    """A plugin state dir with a token, plus a transcript to tail."""
    plugin_dir = tmp_path / "state"
    (plugin_dir / "logs").mkdir(parents=True)
    (plugin_dir / ".token").write_text("ros_ing_faketoken", encoding="utf-8")

    config = tmp_path / "probe-config.json"
    config.write_text(
        json.dumps(
            {
                "version": 2,
                "current_context": "default",
                "contexts": {
                    "default": {
                        "base_url": "http://127.0.0.1:1",  # never connected to
                        "ingest_token": "ros_ing_faketoken",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps({"role": "user", "message": {"content": [{"text": "hi"}]}}) + "\n",
        encoding="utf-8",
    )

    session_id = f"pytest-{os.getpid()}-{int(time.time() * 1000)}"
    env = {
        **os.environ,
        "PROBE_RESEARCH_TAP_PLUGIN_DIR": str(plugin_dir),
        "PROBE_CONFIG_PATH": str(config),
        "CLAUDE_PLUGIN_ROOT": str(HOOKS.parent),
    }
    yield session_id, transcript, env, plugin_dir

    # Always tear the daemon down, even on assertion failure.
    pid_file = Path(f"/tmp/probe-research-tap-watcher-{session_id}.pid")
    if pid_file.is_file():
        try:
            pid = int(pid_file.read_text().strip())
            os.killpg(pid, signal.SIGTERM) if _pgid(pid) == pid else os.kill(pid, signal.SIGTERM)
        except (ValueError, OSError):
            pass
    for suffix in (".pid", ".shutdown", ".owner", ".stopping", ".version"):
        Path(f"/tmp/probe-research-tap-watcher-{session_id}{suffix}").unlink(missing_ok=True)


def _start(session_id: str, transcript: Path, env: dict, hook: Path = SESSION_START) -> subprocess.CompletedProcess:
    """The SessionStart hook, run to its end: it asks the Probe service to record the session
    (`tap record`) and starts nothing of its own (service-only task 15)."""
    payload = json.dumps(
        {"session_id": session_id, "transcript_path": str(transcript), "cwd": "/tmp"}
    )
    return subprocess.run(
        ["bash", str(hook)],
        input=payload,
        text=True,
        capture_output=True,
        env={**env, "PROBE_TAP_HOSTED": "0"},
        check=True,
        timeout=60,
    )


def test_session_end_never_group_kills_a_recycled_pid(tmp_path: Path) -> None:
    """A stale pid file must never let session-end signal a whole process group
    that the pid no longer owns.

    The hazard is narrow but real, and it is NOT "any non-leader pid hits some
    other group" — a PGID exists only because some process with that id led the
    group, so while our wrapper holds pid P no other group can have pgid P.
    What CAN happen: a group leader exits while its group lives on through a
    surviving member, leaving an orphaned group whose pgid is now a free pid.
    A stale pid file naming that id then makes `kill -TERM -<pid>` signal
    strangers.

    Build exactly that: leader + child in one group, kill the leader, leave a
    pid file naming the dead leader, and assert the child survives.
    """
    leader = subprocess.Popen(
        ["bash", "-c", "sleep 60 & sleep 60"], start_new_session=True
    )
    time.sleep(0.5)
    group_pgid = leader.pid
    assert _pgid(leader.pid) == group_pgid

    # Members of that group other than the leader.
    members = [
        int(line.split()[0])
        for line in subprocess.run(
            ["ps", "-eo", "pid,pgid"], capture_output=True, text=True
        ).stdout.splitlines()[1:]
        if len(line.split()) == 2
        and line.split()[1] == str(group_pgid)
        and line.split()[0] != str(leader.pid)
    ]
    assert members, "fixture did not produce a surviving group member"

    # The leader exits; the group lives on through its members, so pgid
    # `group_pgid` still names real processes while that pid is now free.
    leader.kill()
    leader.wait(timeout=10)

    session_id = f"pytest-recycled-{os.getpid()}"
    pid_file = Path(f"/tmp/probe-research-tap-watcher-{session_id}.pid")
    pid_file.write_text(str(group_pgid), encoding="utf-8")
    try:
        subprocess.run(
            ["bash", str(SESSION_END)],
            input=json.dumps({"session_id": session_id}),
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        time.sleep(1)
        assert any(_alive(pid) for pid in members), (
            "session-end group-killed an orphaned group via a stale pid file"
        )
    finally:
        for pid in members:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        pid_file.unlink(missing_ok=True)
        for suffix in (".shutdown", ".stopping"):
            Path(f"/tmp/probe-research-tap-watcher-{session_id}{suffix}").unlink(
                missing_ok=True
            )


def test_stale_sentinels_are_pruned(tap_env) -> None:
    """Sentinels leak forever without pruning (120 observed against 0 daemons):
    session-end never deletes one and session ids never recur."""
    session_id, transcript, env, _ = tap_env

    stale = Path("/tmp/probe-research-tap-watcher-pytest-stale-leak.shutdown")
    stale.touch()
    old = time.time() - (5 * 24 * 3600)
    os.utime(stale, (old, old))

    fresh = Path("/tmp/probe-research-tap-watcher-pytest-fresh-leak.shutdown")
    fresh.touch()
    try:
        _start(session_id, transcript, env)
        assert not stale.exists(), "stale sentinel was not pruned"
        assert fresh.exists(), "pruning must not touch a recent sentinel"
    finally:
        stale.unlink(missing_ok=True)
        fresh.unlink(missing_ok=True)


def test_hooks_are_executable_and_valid_shell() -> None:
    for script in (SESSION_START, SESSION_END):
        assert script.is_file(), script
        if shutil.which("bash"):
            subprocess.run(["bash", "-n", str(script)], check=True, timeout=30)

#: The stand-in's two processes, shaped as session-start.sh makes them. The
#: wrapper forwards TERM to its child and exits at once. The child (`tap watch`)
#: makes its last pass, which a second TERM does not cut short (the daemon only
#: sets a flag), and records that it finished. Its command line names `tap
#: watch` and the session id, as the real wrapper's does: session-end.sh
#: signals no other pid.
_WRAPPER = (
    'trap \'kill -TERM "$C" 2>/dev/null; exit 0\' TERM; bash -c "$1" daemon "$2" "$3" "$4" & C=$!; wait "$C"'
    ' # "$PY" -m tap watch --session-id "$4"'
)
#: The daemon names `tap watch` and the session id too, as `python -m tap watch
#: --session-id <id>` does: a hand-over is waited on only while its group runs it.
_DAEMON = (
    'trap "T=1" TERM; T=; while [ -z "$T" ]; do sleep 0.05; done; trap "" TERM; sleep "$1"; : >"$2"'
    ' # -m tap watch --session-id "$3"'
)
#: Forks the wrapper into its own session and exits, so the OS reaps it as it
#: reaps the real one, whose spawner exits too. Left a zombie of this test, it
#: still counts in `kill -0 -- -PGID` on Linux, though not on macOS: the sibling
#: test passed on Linux only because of it.
_DETACH = """
import os, sys
pid = os.fork()
if pid:
    sys.stdout.write(f"{pid}\\n")
    sys.stdout.flush()
    os._exit(0)
os.setsid()
null = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1, 2):
    os.dup2(null, fd)
os.execvp("bash", ["bash", "-c", *sys.argv[1:]])
"""


class _Daemon:
    def __init__(self, pid: int, marker: Path) -> None:
        self.pid = pid
        self.marker = marker

    def finished(self) -> bool:
        """Whether its last pass ran to the end."""
        return self.marker.exists()


def _fake_daemon_group(
    session_id: str, linger_s: float, prefix: str = "probe-research-tap"
) -> _Daemon:
    """A wrapper and daemon that take `linger_s` to finish after SIGTERM, the
    way the daemon's last pass delivers its FINALIZE."""
    marker = Path(f"/tmp/{prefix}-watcher-{session_id}.finished")
    marker.unlink(missing_ok=True)
    out = subprocess.run(
        [sys.executable, "-c", _DETACH, _WRAPPER, "wrapper", _DAEMON, str(linger_s), str(marker), session_id],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    daemon = _Daemon(int(out), marker)
    Path(f"/tmp/{prefix}-watcher-{session_id}.pid").write_text(str(daemon.pid))
    time.sleep(0.3)
    assert _pgid(daemon.pid) == daemon.pid, "the stand-in wrapper must lead its own group"
    return daemon


def _cleanup(session_id: str, daemon: _Daemon, prefix: str = "probe-research-tap") -> None:
    with contextlib.suppress(OSError):
        os.killpg(daemon.pid, signal.SIGKILL)
    for suffix in (".pid", ".shutdown", ".stopping", ".finished"):
        Path(f"/tmp/{prefix}-watcher-{session_id}{suffix}").unlink(missing_ok=True)


#: Claude Code's SessionEnd exit budget, which the `--wait` entry must fit.
BUDGET_ENV = "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS"
#: A researcher who raised that budget: the wait is then bounded by its own 15s.
RAISED = {BUDGET_ENV: "20000"}
#: The hook's deadline is each budget less this reserve, for what the deadline
#: cannot cover: the hook's own start, the deadline firing late, the exit. A
#: macOS runner spent up to 190ms of it, Linux ~5ms.
RESERVE_S = 0.4
#: A run must leave the harness this much of the reserve: it ends at least this
#: long before the budget.
HEADROOM_S = 0.1


def _inside(elapsed: float, wait_s: float) -> bool:
    """Whether a run with a `wait_s` deadline waited that long and ended at
    least HEADROOM_S before its budget."""
    return wait_s <= elapsed < wait_s + RESERVE_S - HEADROOM_S


#: A tap state folder with nothing in it: a SessionEnd under test must not find
#: the interpreter the developer's own tap remembered (hooks/tap-python.sh) and
#: run the real `tap finish` instead of the `python3` these tests put on PATH.
_NO_STATE = tempfile.mkdtemp(prefix="tap-hook-state-")


def _env(**overrides: str) -> dict:
    """The hook's environment with no budget and no source of the runner's own."""
    env = {k: v for k, v in os.environ.items() if k not in (BUDGET_ENV, "PROBE_TAP_SOURCE")}
    return {**env, "PROBE_RESEARCH_TAP_PLUGIN_DIR": _NO_STATE, "PRBE_CODEX_TAP_PLUGIN_DIR": _NO_STATE, **overrides}


def _session_end(session_id: str, reason: str, *flags: str, env: dict | None = None) -> float:
    started = time.monotonic()
    done = subprocess.run(
        ["bash", str(SESSION_END), *flags],
        input=json.dumps({"session_id": session_id, "reason": reason}),
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
        env=_env() if env is None else env,
    )
    elapsed = time.monotonic() - started
    # bash 3.2 reports a killed background job ("Terminated") on stderr.
    assert done.stderr == "", done.stderr
    return elapsed

def _hung_finish(tmp_path: Path) -> tuple[dict, Path]:
    """PATH with a `python3` whose `tap finish` never answers (a service that is stuck): the
    waiting entry's deadline is all that ends the hook. The file it returns holds the client's pid."""
    real = shutil.which("python3")
    assert real
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "python3"
    client = tmp_path / "finish.pid"
    fake.write_text(
        f'#!/bin/sh\ncase "$*" in *"tap finish"*) echo $$ >"{client}"; exec sleep 60 ;; esac\n'
        f'exec "{real}" "$@"\n'
    )
    fake.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}, client


def _hosted(session_id: str, prefix: str = "probe-research-tap") -> Path:
    marker = Path(f"/tmp/{prefix}-watcher-{session_id}.hosted")
    marker.write_text("12345")
    return marker


def _forget(session_id: str, prefix: str = "probe-research-tap") -> None:
    for suffix in (".hosted", ".shutdown", ".stopping", ".pid"):
        Path(f"/tmp/{prefix}-watcher-{session_id}{suffix}").unlink(missing_ok=True)


def test_a_hung_tap_finish_still_ends_inside_the_budget(tmp_path) -> None:
    """The Probe service records the session (a `.hosted` marker, no wrapper),
    and `tap finish` waits for its last pass. A service that never answers must
    not hold the hook past Claude Code's budget, as the client's own start-up
    and its call's 2s timeout did."""
    session_id = f"pytest-hosted-{os.getpid()}"
    path, client = _hung_finish(tmp_path)
    marker = _hosted(session_id)
    try:
        elapsed = _session_end(session_id, "other", "--wait", env=_env(**path))
        assert _inside(elapsed, 1.1), f"took {elapsed:.2f}s; Claude Code cancels at 1.5s"
        assert not marker.exists()
        assert _gone(int(client.read_text()), timeout=2), "the hook left `tap finish` running"
    finally:
        _forget(session_id)


@pytest.mark.parametrize(
    ("budget", "wait_s"),
    [
        (None, 1.1),
        ("soon", 1.1),
        ("1500", 1.1),
        # Zero-padded is still 1500ms to Claude Code, not a 7-digit budget.
        ("0001500", 1.1),
        # Claude Code reads 0 as unset.
        ("0", 1.1),
        ("0500", 0.1),
        # Shorter than one 50ms poll.
        ("420", 0.02),
        ("0000001", 0.0),
    ],
    ids=["unset", "garbage", "default", "zero-padded", "zero", "short", "under-a-poll", "below-start-up"],
)
def test_claude_code_wait_ends_inside_its_exit_budget(tmp_path, budget, wait_s) -> None:
    """Claude Code bounds the whole SessionEnd phase at its exit budget (1.5s
    unless the researcher sets one) and kills a hook still running then, with
    "Hook cancelled" on the terminal. The wait for the service's `tap finish`
    must end, exit 0, inside it: 1.1s from the hook's start at the default."""
    session_id = f"pytest-budget-{os.getpid()}"
    path, _client = _hung_finish(tmp_path)
    _hosted(session_id)
    env = _env(**path) if budget is None else _env(**path, **{BUDGET_ENV: budget})
    try:
        elapsed = _session_end(session_id, "prompt_input_exit", "--wait", env=env)
        assert _inside(elapsed, wait_s), f"took {elapsed:.2f}s for a {wait_s}s wait"
    finally:
        _forget(session_id)


def test_codex_wait_ends_inside_its_3s_clamp(tmp_path) -> None:
    """Codex (0.159) clamps every SessionEnd hook's timeout to 3s, whatever the
    entry asks, and kills the hook then. The wait must end, exit 0, inside it:
    at 2.6s, the same 400ms reserve Claude Code's budget gets. Claude Code's
    budget variable is not Codex's and must not move it."""
    session_id = f"pytest-codex-clamp-{os.getpid()}"
    prefix = "prbe-codex-tap"
    path, _client = _hung_finish(tmp_path)
    _hosted(session_id, prefix)
    try:
        elapsed = _session_end(session_id, "other", "--wait",
                               env=_env(**path, PROBE_TAP_SOURCE="codex", **{BUDGET_ENV: "20000"}))
        assert _inside(elapsed, 2.6), f"took {elapsed:.2f}s; Codex kills the hook at 3s"
    finally:
        _forget(session_id, prefix)


def test_session_end_wait_is_bounded(tmp_path) -> None:
    """A service stuck on a dead network cannot hold the agent's exit past the
    hook's own 15s cap: a budget too long for the arithmetic still caps there,
    well inside the entry's own 20s timeout, and exits 0."""
    session_id = f"pytest-bounded-{os.getpid()}"
    path, _client = _hung_finish(tmp_path)
    _hosted(session_id)
    try:
        elapsed = _session_end(session_id, "other", "--wait", env=_env(**path, **{BUDGET_ENV: "99999999999999999999"}))
        assert 15 <= elapsed < 16, f"took {elapsed:.2f}s"
    finally:
        _forget(session_id)


@pytest.mark.parametrize("reason", ["clear", "resume"])
def test_a_clear_or_resume_never_waits(tmp_path, reason) -> None:
    """The agent keeps running on `clear` and `resume`: a wait would freeze the researcher's /clear."""
    session_id = f"pytest-nowait-{reason}-{os.getpid()}"
    path, _client = _hung_finish(tmp_path)
    _hosted(session_id)
    try:
        assert _session_end(session_id, reason, "--wait", env=_env(**path)) < 0.8
    finally:
        _forget(session_id)


def test_the_original_entry_still_returns_at_once() -> None:
    """hooks.json dropped the flagless entry in 0.9.25, but a Codex session
    started on an older release still runs it, against this script once its
    start folder is replaced: its behavior, and its 3s budget, must not change."""
    session_id = f"pytest-nowait-{os.getpid()}"
    daemon = _fake_daemon_group(session_id, linger_s=1.5)
    try:
        assert _session_end(session_id, "other") < 1.0
    finally:
        _cleanup(session_id, daemon)


# -- the hook's own helpers: the deadline, the poller, `tap finish` ----------


def _fake_on_path(tmp_path: Path, name: str, script: str) -> dict:
    """The hook's environment with `name` on PATH replaced by `script`
    (/bin/sh), which reaches the real one as "$REAL"."""
    real = shutil.which(name)
    assert real, name
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / name
    fake.write_text(f'#!/bin/sh\nREAL="{real}"\n{script}\n')
    fake.chmod(0o755)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}

#: Runs the hook as a child subreaper, the way a container's PID 1 adopts what
#: a process leaves behind, and lists what the hook left: a child it never
#: reaped, or one still running, both become ours once it exits. A PID 1 that
#: does not reap (an agent run as a container's entry point) keeps each one.
_ADOPT = r"""
import ctypes, json, os, subprocess, sys, time
if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
    sys.exit("prctl(PR_SET_CHILD_SUBREAPER) failed")
run = json.loads(sys.stdin.read())
hook = subprocess.run(run["argv"], input=run["input"], text=True, capture_output=True, env=run["env"], timeout=30)
time.sleep(0.3)
left = []
for pid in filter(str.isdigit, os.listdir("/proc")):
    try:
        stat = open(f"/proc/{pid}/stat").read()
    except OSError:
        continue
    name, rest = stat[stat.index("(") + 1 : stat.rindex(")")], stat[stat.rindex(")") + 1 :].split()
    if rest[1] == str(os.getpid()):
        left.append(f"{pid} {name} {rest[0]}")
        try:
            os.kill(int(pid), 9)
        except OSError:
            pass
while True:
    try:
        os.waitpid(-1, 0)
    except ChildProcessError:
        break
print(json.dumps({"rc": hook.returncode, "stderr": hook.stderr, "left": left}))
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="a child subreaper is Linux's prctl")
@pytest.mark.parametrize(
    "case", ["no-session", "at-the-deadline", "daemon-done", "clear", "hung-tap-finish"]
)
def test_the_hook_leaves_no_process_behind(tmp_path, case) -> None:
    """Every helper is stopped AND reaped before the hook exits: in a container
    whose PID 1 does not reap, anything left becomes a zombie for good."""
    session_id = f"pytest-adopt-{os.getpid()}-{case}"
    payload = {"session_id": session_id, "reason": "clear" if case == "clear" else "other"}
    env = _env(**{BUDGET_ENV: "500"})
    daemon = None
    hosted = Path(f"/tmp/probe-research-tap-watcher-{session_id}.hosted")
    if case == "no-session":
        payload = {}
    elif case == "hung-tap-finish":
        hosted.write_text("12345")
        env.update(_fake_on_path(tmp_path, "python3", 'case "$*" in *"tap finish"*) exec sleep 60 ;; esac\nexec "$REAL" "$@"'))
    else:
        daemon = _fake_daemon_group(session_id, linger_s=0.2 if case == "daemon-done" else 60)
    try:
        out = subprocess.run(
            [sys.executable, "-c", _ADOPT],
            input=json.dumps({"argv": ["bash", str(SESSION_END), "--wait"], "input": json.dumps(payload), "env": env}),
            text=True,
            capture_output=True,
            check=True,
            timeout=60,
        )
        result = json.loads(out.stdout)
        assert result["rc"] == 0, result
        assert result["stderr"] == "", result["stderr"]
        assert result["left"] == [], f"the hook left {result['left']} behind"
    finally:
        if daemon is not None:
            _cleanup(session_id, daemon)
        for suffix in (".hosted", ".shutdown", ".stopping"):
            Path(f"/tmp/probe-research-tap-watcher-{session_id}{suffix}").unlink(missing_ok=True)
