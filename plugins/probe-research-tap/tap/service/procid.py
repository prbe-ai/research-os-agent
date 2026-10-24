"""Which process a pid names: its parent, its kernel start time, its names.

A pid alone is not an identity. Pids are reused, so "pid 4242 is alive" says
nothing about whether it is still the agent that registered a session. The
pair (pid, start time) is: a recycled pid names a process with a different
start time, and that reads as gone, never as alive.

This is the ONE definition of that pair. The tap's `owner.py` records an
agent's `{pid, start}` with it at SessionStart and the Probe service checks the
same pair with it, so both sides must compute the start time the same way (on
Linux, clock ticks since boot from /proc; on Windows the creation FILETIME;
elsewhere `ps`'s `lstart` in UTC).
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


def _windows_parents() -> dict:
    """{pid: parent pid} for every process (one Toolhelp snapshot)."""
    import ctypes
    from ctypes import wintypes

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260)]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return {}
    parents = {}
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            parents[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            ok = k32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snapshot)
    return parents


def proc_windows(pid: int) -> Proc | None:
    """OpenProcess + GetProcessTimes (the creation FILETIME is the start) +
    the image name; the parent from a Toolhelp snapshot. Never `os.kill`: on
    Windows that terminates the process."""
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            code = wintypes.DWORD()
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return None
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            image = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(32768)
            name = image.value if k32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size)) else ""
        finally:
            k32.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return None
    base = os.path.basename(name)
    names = {base, os.path.splitext(base)[0]} - {""}
    return Proc(_windows_parents().get(int(pid), 0), str(created), code.value != 259, frozenset(names))  # 259: STILL_ACTIVE


def proc(pid: int) -> Proc | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        return proc_windows(pid)
    if Path("/proc/self/stat").exists():
        return proc_linux(pid)
    return proc_ps(pid)


def members(pgid: int) -> list[tuple[int, str]]:
    """`(pid, start)` of every running (not zombie) process in process group
    `pgid`, its start time as `start_time` reads it; [] when none, when they
    cannot be listed, and on Windows (no process groups)."""
    found: list[tuple[int, str]] = []
    if os.name == "nt" or pgid <= 1:
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
    """Does a process `pid` exist (ours or not)? `os.kill(pid, 0)` on POSIX;
    on Windows the process is opened instead (there os.kill terminates it)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        info = proc_windows(pid)
        return info is not None and not info.zombie
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
    if os.name != "nt":  # on Windows os.kill(pid, 0) terminates it: proc() asks
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except OSError:
            pass  # exists but is not ours to signal: a reused pid, most likely
    info = proc(pid)
    if info is None and os.name == "nt":
        return False  # OpenProcess failed: gone (or another user's, never an owner of ours)
    if info is None:
        return None
    return not info.zombie and info.start == start
