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
import sys
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
    names = {os.path.basename(tokens[7])}
    executable = get_executable_path(pid)
    if executable:
        names.add(os.path.basename(executable))
    return Proc(ppid, " ".join(tokens[2:7]), tokens[1].startswith("Z"), frozenset(names))


#: `PROC_PIDPATHINFO_MAXSIZE` (macOS `<sys/proc_info.h>`).
_PATH_MAX = 4096


def get_executable_path(pid: int) -> str | None:
    """The file a process runs, from the macOS kernel (`proc_pidpath`); None
    elsewhere or when it cannot be had. `ps`'s `args` cannot say where a path
    with a space ends: the Claude app keeps Claude Code under `~/Library/
    Application Support/`, whose first word is `Application`. No file is
    touched, so no macOS privacy prompt."""
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
    except ImportError:  # a Python built without it
        return None
    for library in ("/usr/lib/libproc.dylib", "/usr/lib/libSystem.B.dylib"):
        try:
            pidpath = ctypes.CDLL(library).proc_pidpath
        except (OSError, AttributeError):
            continue
        pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(_PATH_MAX)
        if pidpath(pid, buffer, _PATH_MAX) <= 0:
            return None
        return buffer.value.decode("utf-8", "replace") or None
    return None


def proc(pid: int) -> Proc | None:
    if pid <= 0:
        return None
    if Path("/proc/self/stat").exists():
        return proc_linux(pid)
    return proc_ps(pid)


def members(pgid: int) -> list[tuple[int, str]]:
    """`(pid, start)` of every running (not zombie) process in process group
    `pgid`, its start time as `start_time` reads it; [] when none or when they
    cannot be listed."""
    found: list[tuple[int, str]] = []
    if pgid <= 1:
        return found
    if Path("/proc/self/stat").exists():
        try:
            entries = [e for e in os.listdir("/proc") if e.isdigit()]
        except OSError:
            return found
        for entry in entries:
            try:
                raw = Path(f"/proc/{entry}/stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            close = raw.rfind(")")
            fields = raw[close + 2 :].split()
            if close < 0 or len(fields) < 20 or fields[0] in ("Z", "X"):
                continue
            with contextlib.suppress(ValueError):
                if int(fields[2]) == pgid:
                    found.append((int(entry), fields[19]))
        return found
    try:
        out = subprocess.run(
            ["ps", "-A", "-ww", "-o", "pid=,pgid=,stat=,lstart="],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_S,
            check=False,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC0"},  # as `proc_ps`: the same start string
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return found
    for line in out.splitlines():
        tokens = line.split()
        if len(tokens) < 8 or tokens[2].startswith("Z"):
            continue
        with contextlib.suppress(ValueError):
            if int(tokens[1]) == pgid:
                found.append((int(tokens[0]), " ".join(tokens[3:8])))
    return found


def exists(pid: int) -> bool:
    """Does a process `pid` exist (ours or not)? `os.kill(pid, 0)`."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


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
