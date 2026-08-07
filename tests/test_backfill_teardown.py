"""Leaving the wizard kills every agent it started. The outbox survives.

What happened: a Ctrl-C on a 106,007-file import printed "Cancelled by user"
and the agent carried on reading, with the wizard gone and no parent left to
stop it. Three separate reasons, all of them real:

  * `proc.kill()` reaps the DIRECT CHILD. An agent starts MCP servers,
    subagents and shells of its own, and those were orphaned, not killed.
  * `with ThreadPoolExecutor(...)` calls shutdown(wait=True) on exit, so an
    interrupt during the import pass BLOCKED until every running agent
    finished by itself -- three of them, 45-minute deadlines each.
  * Nothing guaranteed cleanup on any other exit route.

These use REAL subprocesses. A mock cannot orphan a grandchild, which is the
entire bug -- and this session has already shipped five bugs that lived in the
gap between "the mock accepted it" and "the real thing did".
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

import pytest

from probe.cli import backfill as bf


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _spawn_tree() -> subprocess.Popen:
    """A process that spawns a child of its own, both long-lived.

    The child is what `proc.kill()` misses. It is deliberately NOT a direct
    descendant we hold a handle to -- that is what an agent's MCP servers and
    subagents are.
    """
    program = (
        "import subprocess, sys, time;"
        "k = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']);"
        "print(k.pid, flush=True);"
        "time.sleep(300)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", program],
        stdout=subprocess.PIPE, text=True, start_new_session=True,
    )
    # Wait for the grandchild's pid on stdout.
    line = proc.stdout.readline().strip()
    proc.grandchild = int(line)  # type: ignore[attr-defined]
    return proc


@pytest.fixture
def clean_registry():
    bf._LIVE.clear()
    yield
    for proc in list(bf._LIVE):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except OSError:
            pass
    bf._LIVE.clear()


# -- the grandchild is the bug ----------------------------------------------


def test_killing_an_agent_kills_what_it_started(clean_registry):
    """`proc.kill()` reaps the direct child and leaves the rest running --
    reading the folder, with no parent left to stop them."""
    proc = _spawn_tree()
    grandchild = proc.grandchild
    assert _alive(grandchild)

    bf._kill_tree(proc)

    for _ in range(50):
        if not _alive(grandchild):
            break
        time.sleep(0.1)
    assert not _alive(grandchild), "the grandchild outlived the agent"
    assert proc.poll() is not None


def test_stop_all_kills_every_registered_agent(clean_registry):
    """The import pass runs several at once, and whichever thread takes the
    Ctrl-C can only see its own."""
    procs = [_spawn_tree() for _ in range(3)]
    with bf._LIVE_LOCK:
        bf._LIVE.update(procs)
    grandchildren = [p.grandchild for p in procs]
    assert all(_alive(g) for g in grandchildren)

    bf.stop_all()

    for _ in range(50):
        if not any(_alive(g) for g in grandchildren):
            break
        time.sleep(0.1)
    assert not any(_alive(g) for g in grandchildren)
    assert bf._LIVE == set(), "the registry must not keep reaped agents"


def test_stop_all_is_safe_to_call_twice(clean_registry):
    """It runs from the interrupt path AND from a finally, so it will be."""
    proc = _spawn_tree()
    with bf._LIVE_LOCK:
        bf._LIVE.add(proc)
    bf.stop_all()
    bf.stop_all()  # must not raise on an already-reaped process


def test_stop_all_with_nothing_running_is_a_no_op(clean_registry):
    bf.stop_all()
    assert bf._LIVE == set()


# -- the agent gets its own group so the group can be killed ----------------


def test_an_agent_runs_in_its_own_process_group(tmp_path, monkeypatch):
    """Without this there is no group to kill, only a pid."""
    seen: dict = {}

    class _Proc:
        stdout = iter(())
        pid = 1234

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    def fake_popen(argv, **kw):
        seen.update(kw)
        return _Proc()

    monkeypatch.setattr(bf, "which_agent", lambda a: "/bin/claude")
    monkeypatch.setattr(bf.subprocess, "Popen", fake_popen)
    bf.launch_agent(tmp_path, "prompt", workdir=tmp_path / "w", progress=False)
    assert seen.get("start_new_session") is True


# -- SIGTERM before SIGKILL, because the transcript is what resume replays ---


def test_a_stopped_agent_is_asked_before_it_is_killed(clean_registry):
    """Claude Code writes the missing tool_result on the way down, and that
    transcript is what `--resume` replays. A SIGKILL costs the unit its
    resumability, so TERM comes first."""
    program = (
        "import signal, sys, time\n"
        "got = []\n"
        "signal.signal(signal.SIGTERM, lambda *a: (sys.stdout.write('term\\n'),"
        " sys.stdout.flush(), sys.exit(0)))\n"
        "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", program],
                            stdout=subprocess.PIPE, text=True,
                            start_new_session=True)
    assert proc.stdout.readline().strip() == "ready"
    bf._kill_tree(proc)
    assert proc.stdout.read().strip() == "term", "it was killed, not asked"


def test_something_that_ignores_sigterm_is_still_killed(clean_registry):
    """Grace is not indefinite. An agent wedged past the grace period still
    goes -- leaving the wizard cannot depend on the agent's cooperation."""
    program = (
        "import signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        "time.sleep(300)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", program],
                            stdout=subprocess.PIPE, text=True,
                            start_new_session=True)
    assert proc.stdout.readline().strip() == "ready"
    started = time.monotonic()
    bf._kill_tree(proc)
    assert proc.poll() is not None, "an agent ignoring SIGTERM survived"
    assert time.monotonic() - started < 20, "the grace period is not a hang"


# -- the outbox is deliberately NOT killed ----------------------------------


def test_leaving_says_the_queue_keeps_going(tmp_path, monkeypatch):
    """Files already queued are already the user's. Abandoning them on the way
    out is the one thing leaving must not do -- and someone who interrupts must
    not be left guessing whether it did."""
    from probe.cli import backfill_run as br

    def interrupted(**kw):
        raise KeyboardInterrupt

    monkeypatch.setattr(br, "_execute", interrupted)
    lines = br.execute(client_factory=None, folder=tmp_path, agent=bf.Agent.CLAUDE)
    assert any("keeps uploading" in ln for ln in lines)
    assert any("outbox pause" in ln for ln in lines)


def test_every_exit_route_stops_the_agents(tmp_path, monkeypatch):
    """The interrupt paths already call `stop_all`; this is the route nobody
    thought of."""
    from probe.cli import backfill_run as br

    calls: list[int] = []
    monkeypatch.setattr(bf, "stop_all", lambda: calls.append(1))
    monkeypatch.setattr(br, "_execute", lambda **kw: ["done"])
    assert br.execute(client_factory=None, folder=tmp_path,
                      agent=bf.Agent.CLAUDE) == ["done"]
    assert calls, "a clean return left the agents running"

    calls.clear()
    monkeypatch.setattr(br, "_execute", lambda **kw: (_ for _ in ()).throw(RuntimeError("x")))
    with pytest.raises(RuntimeError):
        br.execute(client_factory=None, folder=tmp_path, agent=bf.Agent.CLAUDE)
    assert calls, "an exception left the agents running"
