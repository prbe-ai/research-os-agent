"""Tap hook scripts of two releases on ONE session (plan C1 review).

From tap 0.10.18 an open session's events run the INSTALLED tap's scripts
(`hooks/installed-tap.sh`), so the scripts of release N+1 take over a session
whose watcher, pid files and markers release N made; and on a Claude Code
rollback the scripts of N take over what N+1 made. This holds the contract
both ways between the previous released tap (0.10.17's hooks, kept verbatim
in tests/fixtures/tap-0.10.17) and this tree's:

  * the watcher the other release's SessionStart started is found by the
    prompt check (no second one), and stopped by SessionEnd (its pid file
    gone, its `tap watch` ended);
  * a session the Probe service records (its `.hosted` marker) is left to it
    by the prompt check and finished through `tap finish` by SessionEnd;
  * turn-end leaves its mark where the other release's daemon reads it.

A stand-in `tap` package plays the tap's side (`tap watch` waits for its
stop; `tap record` writes the service's marker the way the real one does).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

TAP = Path(__file__).resolve().parent.parent
OLD_HOOKS = Path(__file__).resolve().parent / "fixtures" / "tap-0.10.17" / "hooks"
PREFIX = "probe-research-tap"
RELEASES = {"0.10.17": OLD_HOOKS, "this": TAP / "hooks"}

pytestmark = pytest.mark.skipif(os.name != "posix", reason="the hooks are POSIX shell + process groups")

_STAND_IN = '''
import os, signal, sys, time

LOG = os.environ["TAP_TEST_LOG"]


def note(*words):
    with open(LOG, "a") as handle:
        handle.write(" ".join(str(w) for w in words) + "\\n")


cmd = sys.argv[1] if len(sys.argv) > 1 else ""
sid = sys.argv[sys.argv.index("--session-id") + 1] if "--session-id" in sys.argv else ""
if cmd == "record":
    pid = os.environ.get("FAKE_SERVICE_PID")
    if not pid:
        sys.exit(1)
    open(f"/tmp/{os.environ['TAP_TEST_PREFIX']}-watcher-{sid}.hosted", "w").write(pid)
    note("record", sid, os.environ.get("TAP_TEST_RELEASE"))
elif cmd == "watch":
    def stop(*_):
        note("stopped", sid, os.getpid())
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    note("watching", sid, os.getpid(), os.environ.get("TAP_TEST_RELEASE"))
    # Never outlives a test by much, even one killed before its teardown: after
    # 30 s it exits 12 (tap/handback.py EXIT_HANDED_OVER), and its wrapper, told
    # the service took the session, removes its pid file and exits too.
    end = time.monotonic() + 30
    while time.monotonic() < end:
        time.sleep(0.05)
    sys.exit(12)
elif cmd == "finish":
    note("finish", sid)
sys.exit(0)
'''


def _watcher(sid: str, suffix: str) -> Path:
    return Path(f"/tmp/{PREFIX}-watcher-{sid}{suffix}")


def _release(root: Path, hooks: Path, version: str) -> Path:
    """A plugin folder: one release's hook scripts, the stand-in tap, a manifest."""
    shutil.copytree(hooks, root / "hooks")
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "plugin.json").write_text(json.dumps({"name": PLUGIN_NAME, "version": version}))
    (root / "tap").mkdir()
    for name in ("__init__.py", "main.py", "hosted.py"):
        (root / "tap" / name).write_text("")
    (root / "tap" / "__main__.py").write_text(_STAND_IN)
    return root


PLUGIN_NAME = "probe-research-tap"


#: Each session's stand-in log, by session id: what the `sid` teardown stops.
_LOGS: dict[str, Path] = {}


def _runs_session(pid: int, sid: str) -> bool:
    """Does pid `pid` still run something of this session's (its command line
    names the session id)? A pid reused since by anything else is left alone."""
    command = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True).stdout
    return sid in command


@pytest.fixture
def sid():
    own = f"pytest-xv-{uuid.uuid4()}"
    yield own
    # Whatever a test left running (it failed midway, or a hook did not stop
    # it): the wrapper its `.pid` names and every `tap watch` its log saw. The
    # sentinel first, so a wrapper still looping starts no other.
    with contextlib.suppress(OSError):
        _watcher(own, ".shutdown").touch()
    pids = []
    with contextlib.suppress(OSError, ValueError):
        pids.append(int(_watcher(own, ".pid").read_text().strip()))
    log = _LOGS.pop(own, None)
    with contextlib.suppress(OSError, ValueError, IndexError):
        for line in log.read_text().splitlines() if log is not None else []:
            words = line.split()
            if words[:2] == ["watching", own]:
                pids.append(int(words[2]))
    for pid in pids:
        if pid > 1 and _runs_session(pid, own):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, signal.SIGKILL)
    for suffix in (".pid", ".hosted", ".version", ".shutdown", ".owner", ".stopping"):
        _watcher(own, suffix).unlink(missing_ok=True)


@pytest.fixture
def service():
    """A live process whose command line names `probe.service` (what the hooks
    take for this user's Probe service)."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "probe.service"])
    time.sleep(0.2)
    try:
        yield proc.pid
    finally:
        proc.kill()
        proc.wait()


class Session:
    def __init__(self, tmp: Path, sid: str, hosted_by: int | None) -> None:
        self.tmp, self.sid = tmp, sid
        self.roots = {
            "0.10.17": _release(tmp / "cache" / "0.10.17", OLD_HOOKS, "0.10.17"),
            "this": _release(tmp / "cache" / "0.10.18", TAP / "hooks", "0.10.18"),
        }
        self.state = tmp / "tap-state"
        (self.state / "logs").mkdir(parents=True)
        (self.state / ".token").write_text("probe_ing_test")
        self.transcript = tmp / f"{sid}.jsonl"
        self.transcript.write_text('{"type": "user"}\n')
        self.log = tmp / "tap.log"
        _LOGS[sid] = self.log
        (tmp / "home").mkdir()
        self.hosted_by = hosted_by

    def env(self, release: str) -> dict:
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(self.tmp / "home"),
            "XDG_STATE_HOME": str(self.tmp / "xdg-state"),
            "XDG_CONFIG_HOME": str(self.tmp / "xdg-config"),
            "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
            "PROBE_OUTBOX_DIR": str(self.tmp / "outbox"),
            "PROBE_CONFIG_PATH": str(self.tmp / "absent.json"),
            "PROBE_RESEARCH_TAP_PLUGIN_DIR": str(self.state),
            "CLAUDE_PLUGIN_ROOT": str(self.roots[release]),
            "PROBE_TAP_HOSTED": "1" if self.hosted_by else "0",
            "TAP_TEST_LOG": str(self.log),
            "TAP_TEST_PREFIX": PREFIX,
            "TAP_TEST_RELEASE": release,
        }
        if self.hosted_by:
            env["FAKE_SERVICE_PID"] = str(self.hosted_by)
        return env

    def hook(self, release: str, script: str, payload: dict, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["/bin/bash", str(self.roots[release] / "hooks" / script), *args],
                              input=json.dumps({"session_id": self.sid, **payload}), text=True,
                              capture_output=True, env=self.env(release), timeout=30, cwd=str(self.tmp))

    def notes(self, word: str) -> list[list[str]]:
        try:
            lines = self.log.read_text().splitlines()
        except OSError:
            return []
        return [line.split() for line in lines if line.split()[:2] == [word, self.sid]]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().split(") ")[-1].split()[0] != "Z"
    except OSError:
        return True


def _wait(predicate, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


PAIRS = [("0.10.17", "this"), ("this", "0.10.17")]
IDS = ["0.10.17-started-this-tree-continues", "this-tree-started-0.10.17-continues-(rollback)"]


@pytest.mark.parametrize(("starts", "continues"), PAIRS, ids=IDS)
def test_the_other_releases_watcher_is_found_and_stopped(tmp_path, sid, starts, continues):
    s = Session(tmp_path, sid, hosted_by=None)
    started = s.hook(starts, "session-start.sh", {"transcript_path": str(s.transcript), "cwd": str(tmp_path),
                                                 "hook_event_name": "SessionStart", "source": "startup"})
    assert started.returncode == 0, started.stderr
    assert _wait(lambda: s.notes("watching")), (started.stderr, s.log.read_text() if s.log.exists() else "")
    wrapper = int(_watcher(sid, ".pid").read_text().strip())
    assert _alive(wrapper)
    # The prompt check of the other release: the watcher is there, so nothing new.
    prompt = s.hook(continues, "ensure-daemon.sh", {"hook_event_name": "UserPromptSubmit", "prompt": "go"})
    assert prompt.returncode == 0, prompt.stderr
    time.sleep(0.5)
    assert len(s.notes("watching")) == 1, "a second watcher for one session"
    assert int(_watcher(sid, ".pid").read_text().strip()) == wrapper
    # Turn end: the mark the daemon reads, where it reads it.
    sessions = tmp_path / "xdg-state" / "probe" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{sid}.state").write_text("daemon\n")
    turn = s.hook(continues, "turn-end.sh", {"hook_event_name": "Stop", "transcript_path": str(s.transcript)})
    assert turn.returncode == 0, turn.stderr
    assert json.loads((sessions / f"{sid}.turn").read_text())["offset"] == s.transcript.stat().st_size
    # Session end of the other release stops what the first one started.
    ended = s.hook(continues, "session-end.sh", {"hook_event_name": "SessionEnd", "reason": "prompt_input_exit"},
                   "--wait")
    assert ended.returncode == 0, ended.stderr
    watch_pid = int(s.notes("watching")[0][2])
    assert _wait(lambda: not _alive(wrapper) and not _alive(watch_pid)), "the watcher outlived its SessionEnd"
    assert s.notes("stopped"), "its `tap watch` got no stop"
    assert not _watcher(sid, ".pid").exists()
    assert _watcher(sid, ".shutdown").exists()


@pytest.mark.parametrize(("starts", "continues"), PAIRS, ids=IDS)
def test_a_session_the_service_records_is_left_to_it_and_finished_through_it(tmp_path, sid, service, starts,
                                                                             continues):
    s = Session(tmp_path, sid, hosted_by=service)
    started = s.hook(starts, "session-start.sh", {"transcript_path": str(s.transcript), "cwd": str(tmp_path),
                                                 "hook_event_name": "SessionStart", "source": "startup"})
    assert started.returncode == 0, started.stderr
    assert s.notes("record") and _watcher(sid, ".hosted").read_text().strip() == str(service)
    prompt = s.hook(continues, "ensure-daemon.sh", {"hook_event_name": "UserPromptSubmit", "prompt": "go"})
    assert prompt.returncode == 0, prompt.stderr
    assert len(s.notes("record")) == 1 and not s.notes("watching"), "the service's session was started again"
    sessions = tmp_path / "xdg-state" / "probe" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / f"{sid}.state").write_text("daemon\n")
    assert s.hook(continues, "turn-end.sh", {"hook_event_name": "Stop",
                                             "transcript_path": str(s.transcript)}).returncode == 0
    assert (sessions / f"{sid}.turn").exists()
    ended = s.hook(continues, "session-end.sh", {"hook_event_name": "SessionEnd", "reason": "prompt_input_exit"},
                   "--wait")
    assert ended.returncode == 0, ended.stderr
    assert s.notes("finish"), "SessionEnd never asked the service to finish the session"
    assert not _watcher(sid, ".hosted").exists()
    assert _watcher(sid, ".shutdown").exists(), "the sentinel the service's recorder watches"
