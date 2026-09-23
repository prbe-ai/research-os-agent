"""Which agent process owns a session, and is it still running?

A capture daemon has to know when its session is over. SessionEnd tells it when
the agent exits cleanly. Nothing tells it when the agent is SIGKILLed, the
laptop sleeps through a reboot, or the terminal is closed under it, and a
session whose end is never announced is never mined.

The daemon used to guess: "nobody holds the transcript open, and it has been
quiet for ten minutes". Claude Code does not hold its transcript open, so every
session paused for ten minutes read as over. One real session was finalized 29
times in three days, each ending re-mined by the engine and each resumption
reopened by the next prompt.

This module replaces the guess with a fact. SessionStart runs as a child of the
agent, so the hook walks up its own process tree to the first `claude` or
`codex` process and records that process's pid plus its kernel start time. The
daemon then asks one question per tick: is that exact process still alive?

  alive    -> the session is live, however long it has been quiet
  gone     -> the agent exited without SessionEnd: finalize now
  unknown  -> no record (pi, an older hook, a platform we cannot read):
              never end on silence; SessionEnd or the server's idle sweep will

The start time is what makes "gone" safe: a recycled pid names a process with
a different start time, and that reads as gone, never as alive.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from tap import config as cfg

#: Process names that own a session. Claude Code sets its process title to
#: `claude` (native and npm installs alike); Codex's native binary is `codex`
#: (its npm launcher is a `node` parent, which the walk passes through).
AGENT_NAMES = frozenset({"claude", "codex"})

#: A hook sits a few shells below the agent (`sh -c` -> `bash -c` -> the hook,
#: plus ensure-daemon.sh on the self-heal path). Past this, the chain is not a
#: hook's and the owner is unknown.
MAX_WALK = 16

_PS_TIMEOUT_S = 5


class OwnerState(StrEnum):
    ALIVE = "alive"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Owner:
    pid: int
    start: str
    name: str


@dataclass(frozen=True)
class _Proc:
    ppid: int
    start: str
    zombie: bool
    names: frozenset[str]


def _proc_linux(pid: int) -> _Proc | None:
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
    return _Proc(ppid, fields[19], fields[0] == "Z", frozenset(names))


def _proc_ps(pid: int) -> _Proc | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,stat=,lstart=,args=", "-p", str(pid)],
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
    return _Proc(
        ppid,
        " ".join(tokens[2:7]),
        tokens[1].startswith("Z"),
        frozenset({os.path.basename(tokens[7])}),
    )


def _proc(pid: int) -> _Proc | None:
    if pid <= 0:
        return None
    if Path("/proc/self/stat").exists():
        return _proc_linux(pid)
    return _proc_ps(pid)


def find_owner(start_pid: int) -> Owner | None:
    """The nearest `claude`/`codex` ancestor of `start_pid`, never itself."""
    info = _proc(start_pid)
    for _ in range(MAX_WALK):
        if info is None or info.ppid <= 1:
            return None
        pid = info.ppid
        info = _proc(pid)
        if info is None:
            return None
        name = next(iter(sorted(info.names & AGENT_NAMES)), None)
        if name is not None and not info.zombie:
            return Owner(pid, info.start, name)
    return None


def record(session_id: str, start_pid: int) -> Owner | None:
    """Find the owner and write it where the daemon reads it; None if unknown.

    Rewritten on every SessionStart, so a session resumed in a new agent
    process hands the running daemon its new owner.
    """
    owner = find_owner(start_pid)
    path = cfg.owner_file(session_id)
    if owner is None:
        # A stale record would end the session when an unrelated old process
        # exits. Unknown is the safe state.
        path.unlink(missing_ok=True)
        return None
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"pid": owner.pid, "start": owner.start, "name": owner.name}, handle)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return owner


def _read(session_id: str) -> Owner | None:
    path = cfg.owner_file(session_id)
    try:
        # /tmp is shared: only a record this user wrote may end this user's session.
        if path.stat().st_uid != os.getuid():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        owner = Owner(int(data["pid"]), str(data["start"]), str(data["name"]))
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return owner if owner.pid > 1 and owner.start else None


def state(session_id: str) -> OwnerState:
    owner = _read(session_id)
    if owner is None:
        return OwnerState.UNKNOWN
    try:
        os.kill(owner.pid, 0)
    except ProcessLookupError:
        return OwnerState.GONE
    except OSError:
        pass  # exists but is not ours to signal: a reused pid, most likely
    info = _proc(owner.pid)
    if info is None:
        return OwnerState.ALIVE  # alive, but its start time cannot be read here
    if info.zombie or info.start != owner.start:
        return OwnerState.GONE  # exited and unreaped, or the pid was reused
    return OwnerState.ALIVE


def keep(session_id: str) -> None:
    """Refresh the record's mtime, so SessionStart's stale-file prune spares it."""
    with contextlib.suppress(OSError):
        os.utime(cfg.owner_file(session_id))


def main(argv: list[str] | None = None) -> int:
    """`python -m tap owner --session-id SID --from-pid PID` (SessionStart).

    Always exits 0 and prints nothing: an unknown owner only means the daemon
    will not end the session on its own.
    """
    parser = argparse.ArgumentParser(prog="tap owner")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--from-pid", required=True, type=int)
    args = parser.parse_args(argv)
    with contextlib.suppress(Exception):
        record(args.session_id, args.from_pid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
