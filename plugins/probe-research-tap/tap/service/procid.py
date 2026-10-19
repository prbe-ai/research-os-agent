"""Which process a pid names: its parent, its kernel start time, its names.

A pid alone is not an identity. Pids are reused, so "pid 4242 is alive" says
nothing about whether it is still the agent that registered a session. The
pair (pid, start time) is: a recycled pid names a process with a different
start time, and that reads as gone, never as alive.

This is the ONE definition of that pair. The tap's `owner.py` records an
agent's `{pid, start}` with it at SessionStart and the Probe service checks the
same pair with it, so both sides must compute the start time the same way (on
Linux, clock ticks since boot from /proc; elsewhere `ps`'s `lstart` in UTC).
Moved here from the tap's `owner.py`, which now imports it.

Stdlib only, and synced byte-for-byte into the tap (`tap/service/procid.py`,
`make sync-service-client`).
"""

from __future__ import annotations

import contextlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

_PS_TIMEOUT_S = 5


@dataclass(frozen=True)
class Proc:
    ppid: int
    start: str
    zombie: bool
    names: frozenset


def proc_linux(pid: int) -> Proc | None:
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # `pid (comm) state ppid ...`: comm may hold spaces and parens, so split at
    # the LAST ')'. Field 22 (starttime, clock ticks since boot) is index 19 of
    # what follows it.
    close = raw.rfind(")")
    fields = raw[close + 2 :].split()
    if close < 0 or len(fields) < 20:
        return None
    comm = raw[raw.find("(") + 1 : close]
    names = {comm}
    with contextlib.suppress(OSError):
        argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0", 1)[0]
        names.add(os.path.basename(argv0.decode("utf-8", "replace")))
    try:
        ppid = int(fields[1])
    except ValueError:
        return None
    return Proc(ppid, fields[19], fields[0] == "Z", frozenset(names))


def proc_ps(pid: int) -> Proc | None:
    try:
        out = subprocess.run(
            ["ps", "-ww", "-o", "ppid=,stat=,lstart=,args=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            check=False,
            # `lstart` is LOCAL time: a laptop changing timezone would change
            # every recorded start and read every live owner as gone.
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC0"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    # `lstart` is always five tokens under LC_ALL=C: `Wed Sep 23 07:43:28 2026`.
    tokens = out.split()
    if len(tokens) < 8:
        return None
    try:
        ppid = int(tokens[0])
    except ValueError:
        return None
    return Proc(
        ppid,
        " ".join(tokens[2:7]),
        tokens[1].startswith("Z"),
        frozenset({os.path.basename(tokens[7])}),
    )


def proc(pid: int) -> Proc | None:
    if pid <= 0:
        return None
    if Path("/proc/self/stat").exists():
        return proc_linux(pid)
    return proc_ps(pid)


def start_time(pid: int) -> str | None:
    """The kernel start time of a RUNNING `pid`, or None (gone, a zombie, unreadable)."""
    info = proc(pid)
    if info is None or info.zombie:
        return None
    return info.start


def alive(pid: int, start: str) -> bool | None:
    """Is the process (pid, start) still running?

    True: that exact process. False: gone, exited and unreaped, or the pid now
    names another process. None: it exists but this platform cannot read its
    start time, so nothing can be said either way.
    """
    if pid <= 1 or not start:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # exists but is not ours to signal: a reused pid, most likely
    info = proc(pid)
    if info is None:
        return None
    return not info.zombie and info.start == start
