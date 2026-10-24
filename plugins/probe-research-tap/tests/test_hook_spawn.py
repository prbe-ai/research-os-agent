"""The SessionStart/SessionEnd shell hooks — the surface that had no coverage
and where the daemon-lifecycle bug lived.

The daemon logic was well tested; the hooks that START and STOP it were not, so
a wrapper that inherited the hook's process group shipped and every tap died
seconds after SessionStart. These tests pin the two properties that failure
depended on:

  * the wrapper is a SESSION LEADER (pid == pgid), so a signal aimed at
    whatever spawned the hook cannot reach it;
  * session-end only uses the `kill -TERM -<pid>` process-GROUP form when the
    target actually leads that group, so it can never signal an unrelated
    group that merely happens to be led by a process with that id.

These drive the real scripts, not a reimplementation — the bug was IN the
shell, so a python model of it would have stayed green.
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


def _start(session_id: str, transcript: Path, env: dict, hook: Path = SESSION_START) -> int:
    payload = json.dumps(
        {"session_id": session_id, "transcript_path": str(transcript), "cwd": "/tmp"}
    )
    subprocess.run(
        ["bash", str(hook)],
        input=payload,
        text=True,
        capture_output=True,
        env=env,
        check=True,
        timeout=60,
    )
    pid_file = Path(f"/tmp/probe-research-tap-watcher-{session_id}.pid")
    deadline = time.time() + 20
    while time.time() < deadline:
        if pid_file.is_file():
            raw = pid_file.read_text().strip()
            if raw.isdigit() and _alive(int(raw)):
                return int(raw)
        time.sleep(0.1)
    pytest.fail(f"no live wrapper recorded in {pid_file}")


def test_wrapper_is_its_own_session_leader(tap_env) -> None:
    """THE regression guard: pid == pgid.

    Before the fix the wrapper inherited the hook's process group, so a SIGTERM
    delivered to that group killed every tap 14-34s after SessionStart while
    the session was still running and its transcript still growing.
    """
    session_id, transcript, env, _ = tap_env
    wrapper_pid = _start(session_id, transcript, env)

    assert _pgid(wrapper_pid) == wrapper_pid, (
        "wrapper is not a session leader — it shares the spawner's process "
        "group and dies with it"
    )


def test_wrapper_survives_a_kill_of_the_spawning_group(tap_env) -> None:
    """The actual failure, reproduced: signal the group the hook ran in and the
    wrapper must be unaffected."""
    session_id, transcript, env, _ = tap_env
    wrapper_pid = _start(session_id, transcript, env)

    spawner_pgid = os.getpgid(0)
    assert _pgid(wrapper_pid) != spawner_pgid, "wrapper still shares our group"

    # Signal our own group the way a host tearing down a finished hook would.
    # The wrapper is elsewhere now, so this cannot reach it.
    os.killpg(spawner_pgid, 0)  # group exists and is signalable
    time.sleep(2)
    assert _alive(wrapper_pid), "wrapper died with the spawning group"


def test_session_end_stops_the_daemon(tap_env) -> None:
    session_id, transcript, env, _ = tap_env
    wrapper_pid = _start(session_id, transcript, env)

    subprocess.run(
        ["bash", str(SESSION_END)],
        input=json.dumps({"session_id": session_id}),
        text=True,
        capture_output=True,
        env=env,
        check=True,
        timeout=30,
    )

    deadline = time.time() + 15
    while time.time() < deadline and _alive(wrapper_pid):
        time.sleep(0.2)
    assert not _alive(wrapper_pid), "session-end did not stop the wrapper"
    assert Path(
        f"/tmp/probe-research-tap-watcher-{session_id}.shutdown"
    ).is_file(), "shutdown sentinel must remain as the last-resort stop signal"


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


@pytest.mark.skipif(
    not Path("/proc/self/stat").exists(),
    reason="a #! script's process name is its file name only on Linux",
)
def test_an_agent_that_dies_without_session_end_ends_its_session(tap_env, tmp_path) -> None:
    """A hard-killed agent never runs SessionEnd. The daemon must notice its
    owner is gone and finalize, instead of shipping into a dead session until
    ten quiet minutes pass (and, worse, ending LIVE sessions on the same rule).

    The owner here is a script named `claude`: Linux names a #! process after
    its script, so SessionStart's walk up its own ancestry stops there.
    """
    session_id, transcript, env, _ = tap_env
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps({"session_id": session_id, "transcript_path": str(transcript), "cwd": "/tmp"})
    )
    agent = tmp_path / "claude"
    agent.write_text(f'#!/bin/bash\nbash "{SESSION_START}" <"{payload}" >/dev/null 2>&1\n')
    agent.chmod(0o755)
    pid_file = Path(f"/tmp/probe-research-tap-watcher-{session_id}.pid")
    owner_file = Path(f"/tmp/probe-research-tap-watcher-{session_id}.owner")

    # The agent starts the session, then exits with no SessionEnd.
    proc = subprocess.run(
        [str(agent)], env={**env, "PROBE_RESEARCH_TAP_INTERVAL_SECONDS": "1"}, timeout=60
    )
    assert proc.returncode == 0
    record = json.loads(owner_file.read_text())
    assert record["name"] == "claude"
    assert not _alive(record["pid"]), "the recorded owner should be the exited script"

    wrapper_pid = int(pid_file.read_text().strip())
    deadline = time.time() + 30
    while time.time() < deadline and _alive(wrapper_pid):
        time.sleep(0.2)
    assert not _alive(wrapper_pid), "the daemon outlived its agent"
    assert Path(f"/tmp/probe-research-tap-watcher-{session_id}.shutdown").is_file()


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
    return {**env, "PROBE_RESEARCH_TAP_PLUGIN_DIR": _NO_STATE, **overrides}


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


@pytest.mark.parametrize(
    ("reason", "waits"),
    [("prompt_input_exit", True), ("other", True), ("clear", False), ("resume", False)],
)
def test_session_end_waits_for_the_final_delivery(reason, waits) -> None:
    """Returning at once let the agent exit before the FINALIZE left: in a
    container that exit kills the daemon mid-delivery. `/clear` and `/resume`
    keep the agent running, so they must not freeze the researcher. (Under a
    raised exit budget, so the 1.5s last pass fits.) The wait ends when the
    daemon does, not at its 15s deadline."""
    session_id = f"pytest-wait-{os.getpid()}-{reason}"
    daemon = _fake_daemon_group(session_id, linger_s=1.5)
    try:
        elapsed = _session_end(session_id, reason, "--wait", env=_env(**RAISED))
        if waits:
            assert daemon.finished(), f"returned after {elapsed:.2f}s, before the daemon finished"
            assert 1.2 <= elapsed < 2.0, f"took {elapsed:.2f}s for a 1.5s last pass"
        else:
            assert elapsed < 1.0, f"{reason} waited {elapsed:.2f}s"
    finally:
        _cleanup(session_id, daemon)


def test_a_daemon_that_is_already_done_is_not_waited_for() -> None:
    """Nothing left to send: the hook returns at once, not at its deadline."""
    session_id = f"pytest-done-{os.getpid()}"
    daemon = _fake_daemon_group(session_id, linger_s=0)
    try:
        elapsed = _session_end(session_id, "other", "--wait", env=_env(**RAISED))
        assert daemon.finished()
        assert elapsed < 0.5, f"waited {elapsed:.2f}s for a daemon with nothing left to send"
    finally:
        _cleanup(session_id, daemon)


def test_session_end_wait_is_bounded() -> None:
    """A daemon stuck on a dead network cannot hold the agent's exit past the
    hook's own 15s cap: a budget too long for the arithmetic still caps there,
    well inside the entry's own 20s timeout, and exits 0."""
    session_id = f"pytest-bounded-{os.getpid()}"
    prefix = "probe-research-tap"
    daemon = _fake_daemon_group(session_id, linger_s=60, prefix=prefix)
    try:
        elapsed = _session_end(session_id, "other", "--wait", env=_env(**{BUDGET_ENV: "99999999999999999999"}))
        assert 15 <= elapsed < 16, f"took {elapsed:.2f}s"
    finally:
        _cleanup(session_id, daemon, prefix=prefix)


def test_codex_wait_ends_inside_its_3s_clamp() -> None:
    """Codex (0.159) clamps every SessionEnd hook's timeout to 3s, whatever the
    entry asks, and kills the hook then. The wait must end, exit 0, inside it:
    at 2.6s, the same 400ms reserve Claude Code's budget gets. Claude Code's
    budget variable is not Codex's and must not move it."""
    session_id = f"pytest-codex-clamp-{os.getpid()}"
    prefix = "prbe-codex-tap"
    daemon = _fake_daemon_group(session_id, linger_s=60, prefix=prefix)
    try:
        elapsed = _session_end(
            session_id, "other", "--wait", env=_env(PROBE_TAP_SOURCE="codex", **{BUDGET_ENV: "20000"})
        )
        assert _inside(elapsed, 2.6), f"took {elapsed:.2f}s; Codex kills the hook at 3s"
    finally:
        _cleanup(session_id, daemon, prefix=prefix)


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
def test_claude_code_wait_ends_inside_its_exit_budget(budget, wait_s) -> None:
    """Claude Code bounds the whole SessionEnd phase at its exit budget (1.5s
    unless the researcher sets one) and kills a hook still running then, with
    "Hook cancelled" on the terminal. The wait must end, exit 0, inside it:
    1.1s from the hook's start at the default, its start-up included."""
    session_id = f"pytest-budget-{os.getpid()}"
    daemon = _fake_daemon_group(session_id, linger_s=60)
    env = _env() if budget is None else _env(**{BUDGET_ENV: budget})
    try:
        elapsed = _session_end(session_id, "prompt_input_exit", "--wait", env=env)
        assert _inside(elapsed, wait_s), f"took {elapsed:.2f}s for a {wait_s}s wait"
        assert not Path(f"/tmp/probe-research-tap-watcher-{session_id}.stopping").exists()
    finally:
        _cleanup(session_id, daemon)


def test_a_hung_tap_finish_still_ends_inside_the_budget(tmp_path) -> None:
    """The Probe service records the session (a `.hosted` marker, no wrapper),
    and `tap finish` waits for its last pass. A service that never answers must
    not hold the hook past Claude Code's budget, as the client's own start-up
    and its call's 2s timeout did."""
    session_id = f"pytest-hosted-{os.getpid()}"
    marker = Path(f"/tmp/probe-research-tap-watcher-{session_id}.hosted")
    real = shutil.which("python3")
    assert real
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "python3"
    client = tmp_path / "finish.pid"
    fake.write_text(
        f'#!/bin/sh\ncase "$*" in *"tap finish"*) echo $$ >"{client}"; exec sleep 60 ;; esac\n'
        f'exec "{real}" "$@"\n'
    )
    fake.chmod(0o755)
    marker.write_text("12345")
    try:
        elapsed = _session_end(
            session_id, "other", "--wait", env=_env(PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
        )
        assert _inside(elapsed, 1.1), f"took {elapsed:.2f}s; Claude Code cancels at 1.5s"
        assert not marker.exists()
        assert _gone(int(client.read_text()), timeout=2), "the hook left `tap finish` running"
    finally:
        for suffix in (".hosted", ".shutdown", ".stopping"):
            Path(f"/tmp/probe-research-tap-watcher-{session_id}{suffix}").unlink(missing_ok=True)


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


def test_the_waiting_entry_finds_the_daemon_after_its_sibling_ran() -> None:
    """A session that loaded an older plugin's two SessionEnd entries runs both
    at once, in either order. When the flagless one has already signalled and
    removed the pid file, the wrapper is gone (it exits on TERM) but its group
    is still delivering: the waiting one must find that group and wait for it."""
    session_id = f"pytest-sibling-{os.getpid()}"
    daemon = _fake_daemon_group(session_id, linger_s=1.5)
    try:
        assert _session_end(session_id, "other") < 1.0
        assert not Path(f"/tmp/probe-research-tap-watcher-{session_id}.pid").exists()
        # The state this is about: the wrapper gone (reaped), its group alive.
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline and _pgid(daemon.pid) is not None:
            time.sleep(0.02)
        if _pgid(daemon.pid) is not None:
            pytest.skip("nothing reaps orphans here (PID 1 without an init): the wrapper stays a zombie")
        os.killpg(daemon.pid, 0)
        elapsed = _session_end(session_id, "other", "--wait", env=_env(**RAISED))
        assert 1.0 <= elapsed < 2.0, f"took {elapsed:.2f}s for what is left of a 1.5s last pass"
        assert daemon.finished(), "the waiting entry returned before the daemon finished"
        assert not Path(f"/tmp/probe-research-tap-watcher-{session_id}.stopping").exists()
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


def test_cleanup_never_signals_a_helper_bash_has_reaped(tmp_path) -> None:
    """Once the deadline has ended and `wait` has reaped it, its pid is free
    and may be another process's: the hook's exit must not signal it."""
    session_id = f"pytest-reaped-{os.getpid()}"
    daemon = _fake_daemon_group(session_id, linger_s=60)
    record = tmp_path / "deadline.pid"
    # A 700ms budget: a 0.300s deadline, recorded by the `sleep` that is it.
    path = _fake_on_path(tmp_path, "sleep", f'[ "$1" = 0.300 ] && echo $$ >"{record}"\nexec "$REAL" "$@"')
    # Every `kill` the hook runs, its subshells' included, logged to a file of
    # its own: no redirection inside the hook can hide one (bash 3.2 has no
    # BASH_XTRACEFD, and `bash -x` writes to the stderr a helper silences).
    kills = tmp_path / "kills.log"
    wrapper = tmp_path / "kill.bash"
    wrapper.write_text(f'kill() {{ printf "%s\\n" "$*" >>"{kills}"; builtin kill "$@"; }}\n')
    try:
        _session_end(session_id, "other", "--wait", env=_env(**path, **{BUDGET_ENV: "700"}, BASH_ENV=str(wrapper)))
        deadline = record.read_text().strip()
        calls = kills.read_text().splitlines()
        assert any(call.split()[-1:] == [deadline] for call in calls), f"no kill naming the deadline was logged: {calls}"
        signals = [call for call in calls if deadline in call.split() and "-0" not in call.split()]
        assert not signals, f"signalled the reaped deadline {deadline}: {signals}"
    finally:
        _cleanup(session_id, daemon)


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


# -- a resume after an update ------------------------------------------------
#
# A resumed session hands its live daemon to the new agent process, and that
# daemon runs the code its own tap version loaded. So the restart that applies a
# tap update kept the OLD daemon: anthrogen's dev pod ran tap 0.9.3 for days
# after its tap was updated to 0.9.14 (2026-10-07), and every local check read
# the new version. These drive the real hook against a real daemon.


def _version_file(session_id: str) -> Path:
    return Path(f"/tmp/probe-research-tap-watcher-{session_id}.version")


def _this_tap() -> str:
    return json.loads((HOOKS.parent / ".claude-plugin" / "plugin.json").read_text())["version"]


def _gone(pid: int, timeout: float = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline and _alive(pid):
        time.sleep(0.2)
    return not _alive(pid)


def test_a_spawn_records_the_tap_version(tap_env) -> None:
    session_id, transcript, env, _ = tap_env
    _start(session_id, transcript, env)
    assert _version_file(session_id).read_text() == _this_tap()


def _versioned_tap(tmp_path: Path, version: str) -> Path:
    """This tap copied into a marketplace cache folder named `version` (as
    Claude Code and Codex install one), its manifests saying `version` too."""
    root = tmp_path / "cache" / "research-os-agent" / "probe-research-tap" / version
    shutil.copytree(
        HOOKS.parent, root, ignore=shutil.ignore_patterns("tests", "__pycache__", "*.pyc", ".venv")
    )
    for manifest in root.glob(".*-plugin/plugin.json"):
        data = json.loads(manifest.read_text())
        manifest.write_text(json.dumps({**data, "version": version}))
    return root


def _start_older_tap(tmp_path, session_id, transcript, env, version) -> int:
    """A live daemon an older tap started: from its versioned folder, with no
    version record (taps before 0.9.16 wrote none)."""
    root = _versioned_tap(tmp_path, version)
    pid = _start(
        session_id, transcript, {**env, "CLAUDE_PLUGIN_ROOT": str(root)},
        hook=root / "hooks" / "session-start.sh",
    )
    _version_file(session_id).unlink()
    return pid


def test_a_resume_replaces_a_daemon_an_older_tap_started(tap_env, tmp_path) -> None:
    session_id, transcript, env, plugin_dir = tap_env
    old = _start_older_tap(tmp_path, session_id, transcript, env, "0.9.3")

    new = _start(session_id, transcript, env)

    assert new != old, "the resume kept the older tap's daemon"
    assert _gone(old), "the older tap's wrapper is still running beside the new one"
    assert _alive(new)
    assert _version_file(session_id).read_text() == _this_tap()
    log = (plugin_dir / "logs" / f"{session_id}.log").read_text()
    assert f"tap 0.9.3 daemon (wrapper {old}) still runs this session; replacing it with {_this_tap()}" in log


def test_a_resume_replaces_a_daemon_a_lower_version_recorded(tap_env) -> None:
    session_id, transcript, env, _ = tap_env
    old = _start(session_id, transcript, env)
    _version_file(session_id).write_text("0.9.14")

    new = _start(session_id, transcript, env)

    assert new != old
    assert _gone(old)


@pytest.mark.parametrize("version", ["0.4.4", "0.3.0"])
def test_a_protocol_1_daemon_is_never_replaced(tap_env, tmp_path, version) -> None:
    """Before 0.4.5 a daemon uploads on protocol 1, and a new daemon refuses
    that session every tick ("legacy transcript coverage is unverified")."""
    session_id, transcript, env, _ = tap_env
    old = _start_older_tap(tmp_path, session_id, transcript, env, version)

    again = _start(session_id, transcript, env)

    assert again == old
    assert _alive(old)


def test_a_protocol_1_version_record_is_never_replaced(tap_env) -> None:
    session_id, transcript, env, _ = tap_env
    first = _start(session_id, transcript, env)
    _version_file(session_id).write_text("0.4.4")
    assert _start(session_id, transcript, env) == first


def test_a_daemon_of_unknown_version_is_left_alone(tap_env) -> None:
    """No record and no versioned folder (a checkout, Kimi Code's copy): it
    could be a protocol-1 tap, so it is not replaced on a guess."""
    session_id, transcript, env, _ = tap_env
    first = _start(session_id, transcript, env)
    _version_file(session_id).unlink()
    assert _start(session_id, transcript, env) == first
    assert _alive(first)


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="the fallback reads /proc")
def test_without_ps_the_older_daemon_is_found_in_proc_and_replaced(tap_env, tmp_path) -> None:
    """A slim container has no `ps`: the wrapper's command line comes from
    /proc and its group from `kill -0 -- -PID`, as in session-end.sh."""
    session_id, transcript, env, _ = tap_env
    old = _start_older_tap(tmp_path, session_id, transcript, env, "0.9.3")
    no_ps = tmp_path / "bin-without-ps"
    no_ps.mkdir()
    for directory in env["PATH"].split(os.pathsep):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for name in names:
            link = no_ps / name
            if name != "ps" and not link.exists() and not link.is_symlink():
                link.symlink_to(Path(directory) / name)
    assert shutil.which("ps", path=str(no_ps)) is None

    new = _start(session_id, transcript, {**env, "PATH": str(no_ps)})

    assert new != old
    assert _gone(old), "the older daemon's group outlived its replacement"
    assert _alive(new)


def test_a_resume_under_the_same_tap_keeps_its_daemon(tap_env) -> None:
    session_id, transcript, env, _ = tap_env
    first = _start(session_id, transcript, env)
    again = _start(session_id, transcript, env)
    assert again == first
    assert _alive(first)


def test_an_older_tap_never_replaces_a_newer_daemon(tap_env) -> None:
    session_id, transcript, env, _ = tap_env
    first = _start(session_id, transcript, env)
    _version_file(session_id).write_text("99.0.0")

    again = _start(session_id, transcript, env)

    assert again == first
    assert _alive(first)
    assert _version_file(session_id).read_text() == "99.0.0"


def test_a_live_pid_that_is_not_this_sessions_tap_is_never_signalled(tap_env) -> None:
    """/tmp is world-writable and pids are reused: a pid file naming some other
    live process, with no version record, must not get it killed. Its pid file
    is stale (its command line is not this session's `tap watch`): it goes, and
    the session gets its own wrapper."""
    session_id, transcript, env, _ = tap_env
    stranger = subprocess.Popen(["sleep", "60"])
    pid_file = Path(f"/tmp/probe-research-tap-watcher-{session_id}.pid")
    try:
        pid_file.write_text(str(stranger.pid))
        subprocess.run(
            ["bash", str(SESSION_START)],
            input=json.dumps(
                {"session_id": session_id, "transcript_path": str(transcript), "cwd": "/tmp"}
            ),
            text=True,
            capture_output=True,
            env=env,
            check=True,
            timeout=60,
        )
        time.sleep(0.5)
        assert stranger.poll() is None, "the hook signalled a process that is not its tap"
        assert pid_file.read_text().strip() != str(stranger.pid), "the stale pid file still names the stranger"
    finally:
        stranger.kill()
        stranger.wait()


def test_a_version_record_is_pruned_only_once_its_pid_file_is_gone(tap_env) -> None:
    """Written once and never refreshed: a days-old live session must keep it."""
    session_id, transcript, env, _ = tap_env
    old = time.time() - (5 * 24 * 3600)
    orphan = Path("/tmp/probe-research-tap-watcher-pytest-version-orphan.version")
    kept = Path("/tmp/probe-research-tap-watcher-pytest-version-live.version")
    kept_pid = kept.with_suffix(".pid")
    for path in (orphan, kept, kept_pid):
        path.write_text("0.9.15")
        os.utime(path, (old, old))
    try:
        _start(session_id, transcript, env)
        assert not orphan.exists(), "an orphaned version record was not pruned"
        assert kept.exists(), "a version record beside a pid file was pruned"
    finally:
        for path in (orphan, kept, kept_pid):
            path.unlink(missing_ok=True)
