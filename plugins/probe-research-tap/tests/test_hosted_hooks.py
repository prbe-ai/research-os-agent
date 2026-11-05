"""The shell hooks around a session the Probe service records (`.hosted`).

    session-start.sh  removes a dead wrapper's pid file, hosting or not, so
                      nothing reads "a wrapper runs this session" afterwards;
                      its 2-day prune keeps a `.hosted` marker whose pid is
                      this user's live Probe service (a session can be days old)
    session-end.sh    signals a pid only once its command line proves it is
                      this session's `tap watch` wrapper; a stale pid file is
                      only removed
    ensure-daemon.sh  takes its `.hosted` fast path only for a marker of this
                      user's naming a pid above 1

These drive the real scripts, with a stand-in `tap` package whose `record`
succeeds the way the real one does when the service records the session (it
writes the marker), and a stand-in service: a process named `probe.service`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
PREFIX = "probe-research-tap"

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX shell + process groups")


def _watcher(sid: str, suffix: str) -> Path:
    return Path(f"/tmp/{PREFIX}-watcher-{sid}{suffix}")


@pytest.fixture
def sid():
    own = f"pytest-hosted-{uuid.uuid4()}"
    yield own
    for suffix in (".pid", ".hosted", ".version", ".shutdown", ".owner", ".stopping"):
        _watcher(own, suffix).unlink(missing_ok=True)


@pytest.fixture
def service():
    """A live process whose command line names `probe.service`."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "probe.service"])
    time.sleep(0.2)
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


@pytest.fixture
def plugin(tmp_path, service):
    """The real hooks in a plugin folder of their own, a stand-in `tap`, a
    paired state folder and an isolated home."""
    root = tmp_path / "plugin"
    shutil.copytree(HOOKS, root / "hooks")
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "0.10.17"}))
    (root / "tap").mkdir()
    (root / "tap" / "__init__.py").write_text("")
    (root / "tap" / "__main__.py").write_text(
        "import os, sys\n"
        "if sys.argv[1] == 'record':\n"
        "    sid = sys.argv[sys.argv.index('--session-id') + 1]\n"
        f"    open(f'/tmp/{PREFIX}-watcher-{{sid}}.hosted', 'w').write(os.environ['FAKE_SERVICE_PID'])\n"
        "sys.exit(0)\n"
    )
    state = tmp_path / "state"
    (state / "logs").mkdir(parents=True)
    (state / ".token").write_text("probe_ing_test")
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_STATE_HOME": str(tmp_path / "xdg-state"),
        "XDG_CONFIG_HOME": str(tmp_path / "xdg-config"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "PROBE_OUTBOX_DIR": str(tmp_path / "outbox"),
        "PROBE_CONFIG_PATH": str(tmp_path / "absent.json"),
        "CLAUDE_PLUGIN_ROOT": str(root),
        "PROBE_RESEARCH_TAP_PLUGIN_DIR": str(state),
        "FAKE_SERVICE_PID": str(service),
        "PROBE_TAP_HOSTED": "1",
    }
    env.pop("PROBE_TAP_SOURCE", None)
    return root, env, tmp_path


def _session_start(plugin, sid: str) -> subprocess.CompletedProcess:
    root, env, tmp = plugin
    transcript = tmp / f"{sid}.jsonl"
    transcript.write_text("")
    return subprocess.run(
        ["bash", str(root / "hooks" / "session-start.sh")],
        input=json.dumps({"session_id": sid, "transcript_path": str(transcript), "cwd": str(tmp)}),
        capture_output=True, text=True, env=env, timeout=60, check=True,
    )


def _session_end(plugin, sid: str) -> None:
    root, env, _ = plugin
    subprocess.run(
        ["bash", str(root / "hooks" / "session-end.sh")],
        input=json.dumps({"session_id": sid, "reason": "other"}),
        capture_output=True, text=True, env=env, timeout=30, check=True,
    )


def _alive(proc: subprocess.Popen, wait: float = 1.0) -> bool:
    try:
        proc.wait(timeout=wait)
    except subprocess.TimeoutExpired:
        return True
    return False


# -- session-start.sh: a dead wrapper's pid file -----------------------------------------

def test_an_empty_pid_file_being_written_is_left(plugin, sid, tmp_path):
    """A wrapper truncates its pid file, then writes its pid: an empty one may
    be a wrapper starting right now."""
    pid_file = _watcher(sid, ".pid")
    pid_file.write_text("")
    _session_start(plugin, sid)
    assert pid_file.exists()

# -- session-start.sh: the 2-day prune of `.hosted` markers -------------------------------


def _aged(path: Path, value: str) -> Path:
    path.write_text(value)
    old = time.time() - 3 * 86400 - 60
    os.utime(path, (old, old))
    return path


def test_another_sessions_start_keeps_a_live_services_marker(plugin, sid, service):
    """Audit #4: nothing rewrites a marker while the service records the
    session, and every SessionStart pruned markers older than 2 days, so a
    3-day-old live session read `not started` from then on."""
    other = f"pytest-hosted-other-{uuid.uuid4()}"
    live = _aged(_watcher(other, ".hosted"), str(service))
    try:
        _session_start(plugin, sid)
        assert live.exists(), "a live session's .hosted marker was pruned"
        assert time.time() - live.stat().st_mtime < 3600, "a kept marker is looked at again in 2 days, not every start"
    finally:
        live.unlink(missing_ok=True)


def test_the_prune_still_removes_markers_naming_no_live_service(plugin, sid):
    stranger = subprocess.Popen(["sleep", "60"])
    names = {
        "dead": str(_dead_pid()),
        "not-the-service": str(stranger.pid),
        "init": "1",
        "own-group": "0",
        "garbage": "x",
    }
    markers = {why: _aged(_watcher(f"pytest-hosted-{why}-{uuid.uuid4()}", ".hosted"), value)
               for why, value in names.items()}
    try:
        _session_start(plugin, sid)
        assert [why for why, marker in markers.items() if marker.exists()] == []
    finally:
        stranger.kill()
        stranger.wait()
        for marker in markers.values():
            marker.unlink(missing_ok=True)


def test_a_fresh_marker_is_never_looked_at(plugin, sid):
    fresh = _watcher(f"pytest-hosted-fresh-{uuid.uuid4()}", ".hosted")
    fresh.write_text(str(_dead_pid()))
    try:
        _session_start(plugin, sid)
        assert fresh.exists()
    finally:
        fresh.unlink(missing_ok=True)

# -- session-end.sh: only this session's wrapper is signalled ---------------------------


@pytest.mark.parametrize("leader", [True, False], ids=["group-leader", "not-a-leader"])
def test_session_end_never_signals_a_process_a_stale_pid_file_names(plugin, sid, service, leader):
    """Audit #2: the pid file a dead wrapper left beside a hosted session's
    marker is read at SessionEnd, and its pid may name anything by then: a
    group leader got `kill -TERM -<pid>` (its whole group), any other process
    `kill -TERM <pid>`. Neither is this session's wrapper."""
    _watcher(sid, ".hosted").write_text(str(service))
    stranger = subprocess.Popen(["sleep", "60"], start_new_session=leader)
    try:
        _watcher(sid, ".pid").write_text(str(stranger.pid))
        _session_end(plugin, sid)
        assert _alive(stranger), "SessionEnd signalled a process that is not this session's tap"
        assert not _watcher(sid, ".pid").exists(), "the stale pid file is still there"
        assert not _watcher(sid, ".stopping").exists()
    finally:
        stranger.kill()
        stranger.wait()


def test_session_end_never_signals_another_sessions_wrapper(plugin, sid):
    other = f"pytest-hosted-another-{uuid.uuid4()}"
    wrapper = subprocess.Popen(
        ["bash", "-c", 'sleep 60; : "$PY" -m tap watch --session-id "$1"', "wrapper", other], start_new_session=True
    )
    try:
        _watcher(sid, ".pid").write_text(str(wrapper.pid))
        _session_end(plugin, sid)
        assert _alive(wrapper)
    finally:
        wrapper.kill()
        wrapper.wait()

# -- ensure-daemon.sh: the `.hosted` fast path --------------------------------------------


def _ensure(plugin, sid: str, **extra: str) -> Path:
    """Run ensure-daemon.sh with a session-start.sh that only says it ran;
    the file it writes when it does."""
    _, env, tmp = plugin
    fake = tmp / "fake-root"
    (fake / "hooks").mkdir(parents=True, exist_ok=True)
    ran = tmp / "session-start-ran"
    (fake / "hooks" / "session-start.sh").write_text(f'#!/bin/sh\necho ran >>"{ran}"\n')
    (fake / "hooks" / "session-start.sh").chmod(0o755)
    subprocess.run(
        ["/bin/bash", str(HOOKS / "ensure-daemon.sh")],
        input=json.dumps({"session_id": sid}),
        capture_output=True, text=True, env={**env, "PLUGIN_ROOT": str(fake), **extra}, timeout=10, check=True,
    )
    return ran


@pytest.mark.parametrize("content", ["0", "1"])
@pytest.mark.parametrize("ostype", ["linux-gnu", "msys"])
def test_a_marker_naming_pid_0_or_1_is_not_a_live_service(plugin, sid, content, ostype):
    """`kill -0 0` asks the hook's own process group, which is always there,
    and Git Bash trusted any number: either read a marker naming no service as
    a live one, so the session was never started again."""
    _watcher(sid, ".hosted").write_text(content)
    ran = _ensure(plugin, sid, OSTYPE=ostype)
    assert ran.exists(), "the fast path took a marker naming pid 0 or 1 for a live service"


def test_a_marker_naming_the_live_service_still_takes_the_fast_path(plugin, sid, service):
    _watcher(sid, ".hosted").write_text(str(service))
    assert not _ensure(plugin, sid).exists()


def test_the_fast_path_requires_a_marker_of_this_users():
    """/tmp is shared: another user's marker could name any process. A file of
    another user cannot be made without privileges here, so the check itself
    is pinned."""
    text = (HOOKS / "ensure-daemon.sh").read_text()
    assert 'if [ -f "$HOSTF" ] && [ ! -L "$HOSTF" ] && [ -O "$HOSTF" ] && [ -s "$HOSTF" ]; then' in text
    assert '{ [ -f "$1" ] && [ ! -L "$1" ] && [ -O "$1" ] && [ -s "$1" ]; } || return 0' in text


@pytest.mark.parametrize("content", ["0", "1"])
def test_a_pid_file_naming_pid_0_or_1_is_not_a_live_daemon(plugin, sid, content):
    """`kill -0 0` asks the hook's own process group: a pid file saying 0 read
    as a live daemon on every prompt, so the session was never started again."""
    _watcher(sid, ".pid").write_text(content)
    assert _ensure(plugin, sid).exists(), "the fast path took a pid file naming pid 0 or 1 for a live daemon"


@pytest.mark.parametrize("which", [".pid", ".hosted"])
def test_a_watcher_file_that_is_a_link_is_never_read(plugin, sid, tmp_path, which):
    """A link (to a tty, a FIFO: a read would block the prompt) or to a file
    that names a live process is not this session's watcher file."""
    target = tmp_path / "elsewhere"
    target.write_text(str(os.getpid()))
    _watcher(sid, which).symlink_to(target)
    assert _ensure(plugin, sid).exists(), "a link was read as a live watcher"


def test_a_marker_that_is_a_fifo_never_blocks_the_prune(plugin, sid):
    """`head` on a FIFO waits for a writer forever: the prune looks only at
    regular files."""
    fifo = _watcher(f"pytest-hosted-fifo-{uuid.uuid4()}", ".hosted")
    os.mkfifo(fifo)
    old = time.time() - 3 * 86400 - 60
    os.utime(fifo, (old, old))
    try:
        _session_start(plugin, sid)  # times out (60 s) if it blocks
        assert not fifo.exists()
    finally:
        fifo.unlink(missing_ok=True)


# -- session-end.sh: a hand-over is never signalled -----------------------------------------


@pytest.mark.parametrize("wait", [False, True], ids=["first-entry", "waiting-entry"])
def test_session_end_never_signals_a_process_a_stale_hand_over_names(plugin, sid, service, wait):
    """CRITICAL: with no pid file, SessionEnd took the pid from `.stopping`
    (world-writable /tmp; a stale one lives 2 days), skipped the proof, and
    group-killed it when it led a group. A hand-over is only waited on, and
    only while its group runs this session's `tap watch`."""
    _watcher(sid, ".hosted").write_text(str(service))
    stranger = subprocess.Popen(["setsid", "sleep", "60"])
    time.sleep(0.2)
    try:
        _watcher(sid, ".stopping").write_text(str(stranger.pid))
        root, env, _ = plugin
        started = time.monotonic()
        subprocess.run(
            ["bash", str(root / "hooks" / "session-end.sh"), *(["--wait"] if wait else [])],
            input=json.dumps({"session_id": sid, "reason": "other"}),
            capture_output=True, text=True, env={**env, "CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS": "3000"},
            timeout=30, check=True,
        )
        assert _alive(stranger), "SessionEnd signalled the process a stale hand-over named"
        assert time.monotonic() - started < 2.5, "it waited on a group that is not this session's tap"
    finally:
        stranger.kill()
        stranger.wait()


def test_a_hand_over_that_is_a_link_or_anothers_is_not_read(plugin, sid, tmp_path):
    stranger = subprocess.Popen(["setsid", "sleep", "60"])
    time.sleep(0.2)
    target = tmp_path / "planted"
    target.write_text(str(stranger.pid))
    try:
        _watcher(sid, ".stopping").symlink_to(target)
        _session_end(plugin, sid)
        assert _alive(stranger)
    finally:
        stranger.kill()
        stranger.wait()


def test_session_start_clears_the_last_hand_over(plugin, sid):
    """A resumed session's SessionStart: no wrapper of it runs, so the last
    SessionEnd's hand-over is stale and goes with the sentinel."""
    _watcher(sid, ".stopping").write_text(str(os.getpid()))
    _watcher(sid, ".shutdown").touch()
    _session_start(plugin, sid)
    assert not _watcher(sid, ".stopping").exists()
    assert not _watcher(sid, ".shutdown").exists()

# -- the delta review ------------------------------------------------------------------


def _path_without(tmp_path: Path, name: str) -> str:
    """PATH as a folder of links to everything on it but `name` (a slim
    container image)."""
    bare = tmp_path / f"bin-without-{name}"
    bare.mkdir()
    for directory in os.environ["PATH"].split(os.pathsep):
        try:
            names = os.listdir(directory)
        except OSError:
            continue
        for entry in names:
            link = bare / entry
            if entry != name and not link.exists() and not link.is_symlink():
                link.symlink_to(Path(directory) / entry)
    assert shutil.which(name, path=str(bare)) is None
    return str(bare)

def test_the_prompt_check_starts_no_process_for_a_live_service(plugin, sid, service):
    """The common path of every prompt: the payload read is its one process
    (`$(cat)`); the marker and pid file reads are bash's own (no `$(...)`)."""
    root, env, tmp = plugin
    _watcher(sid, ".hosted").write_text(str(service))
    trace = subprocess.run(
        ["bash", "-x", str(HOOKS / "ensure-daemon.sh")],
        input=json.dumps({"session_id": sid}), capture_output=True, text=True,
        env={**env, "PLUGIN_ROOT": str(root), "PS4": "+$BASHPID "}, timeout=10, check=True,
    ).stderr
    # Each traced line opens with `+` per nesting level, then the shell's pid.
    shells = {line.split()[0].lstrip("+") for line in trace.splitlines() if line.startswith("+")}
    shells = {pid for pid in shells if pid.isdigit()}
    assert len(shells) <= 2, f"the fast path ran in {len(shells)} shells: {sorted(shells)}"


def test_a_version_record_that_is_a_link_is_never_read_or_written_through(plugin, sid, tmp_path):
    """/tmp is shared: a `.version` link planted beside a session's pid file
    must not be read as its wrapper's tap, nor have this hook write through it."""
    target = tmp_path / "planted-target"
    target.write_text("untouched")
    _watcher(sid, ".version").symlink_to(target)
    root, env, _ = plugin
    _session_start((root, {**env, "PROBE_TAP_HOSTED": "0"}, tmp_path), sid)
    assert target.read_text() == "untouched"
