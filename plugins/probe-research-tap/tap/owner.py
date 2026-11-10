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
agent, so the hook walks up its own process tree to the first agent process
(`AGENT_NAMES`) and records that process's pid plus its kernel start time. The
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
import sys
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from tap import config as cfg
from tap.service import procid

#: Process names that own a session beside each registry harness's `binary` and
#: `process_title`. Cursor's CLI also runs as `agent` (comm `MainThread`), and the
#: app runs hooks below its extension host; pi titles its RPC mode `pi-rpc`.
OWNER_ALIASES = frozenset({  # harness-literal-ok: process titles
    "agent", "cursor", "Cursor", "Cursor Helper", "Cursor Helper (Plugin)",
    "pi-rpc",  # harness-literal-ok: process titles
})
#: Added only when the registry cannot be read, so recording never stops on a
#: half-written tap folder (the service's `caller.AGENT_NAMES` rule).
FALLBACK_NAMES = frozenset({  # harness-literal-ok: the fallback when the registry cannot be read
    "claude", "codex", "kimi", "kimi-code", "pi", "cursor-agent",  # harness-literal-ok: fallback
})


def build_agent_names() -> frozenset[str]:
    """`OWNER_ALIASES` plus every registry harness's binary and process title.

    Kimi Code on macOS is a native `kimi` (comm, argv[0] and executable); on Linux
    its node process retitles itself. Both names come from its registry row (11-09).
    """
    try:
        from tap import harness_registry

        harnesses = harness_registry.get_registry().all()
    except Exception:  # `tap owner` must never fail: the fixed names stand in
        return OWNER_ALIASES | FALLBACK_NAMES
    names = {n for h in harnesses for n in (h.binary, h.process_title) if n}
    return OWNER_ALIASES | frozenset(names)


#: Read once at import; tests monkeypatch it.
AGENT_NAMES = build_agent_names()

#: A hook sits a few shells below the agent (`sh -c` -> `bash -c` -> the hook,
#: plus ensure-daemon.sh on the self-heal path). Past this, the chain is not a
#: hook's and the owner is unknown.
MAX_WALK = 16


class OwnerState(StrEnum):
    ALIVE = "alive"
    GONE = "gone"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Owner:
    pid: int
    start: str
    name: str


#: Which process a pid names -- its parent, kernel start time and names -- is
#: ONE definition shared with the Probe service, which checks the `{pid, start}`
#: this module records (`tap/service/procid.py`, a byte copy of
#: `probe.service.procid`; `make sync-service-client`).
_Proc = procid.Proc
_proc_linux = procid.proc_linux
_proc_ps = procid.proc_ps
_proc = procid.proc


def find_owner(start_pid: int) -> Owner | None:
    """The nearest agent (AGENT_NAMES) ancestor of `start_pid`, never itself."""
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
    # False: gone, exited and unreaped, or the pid was reused. None: alive, but
    # its start time cannot be read here.
    return OwnerState.GONE if procid.alive(owner.pid, owner.start) is False else OwnerState.ALIVE


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
