"""`python -m tap start` — the ONE place a capture daemon is spawned.

Before this, the same detached crash-recovery spawn existed twice: as a bash
string in `hooks/session-start.sh` and as a TypeScript string in
`probe-research-pi/src/daemon.ts::buildWrapperScript`. Two copies of a
process-lifecycle contract (pid file, shutdown sentinel, restart window,
TERM forwarding) is where lifecycle bugs live, and a third caller was about
to arrive: the probe CLI's self-heal.

WHY GATES LIVE HERE AND NOT IN CALLERS. Each caller would otherwise have to
re-implement the killswitch, `.disabled_paths`, token and transcript checks,
and they would drift. Callers get exit codes instead, which is also what
lets a caller RENDER the reason without knowing how it was decided.

The interpreter check is the one gate that is new rather than moved. The
wrapper respawns a dying daemon five times per minute and then gives up
silently; an interpreter below 3.11 fails that way every time, which reads
as "capture mysteriously does not work". Refusing up front, with the version
in the message, turns a silent loop into one line.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from tap import config as cfg

EXIT_OK = 0
EXIT_NOT_PAIRED = 2
EXIT_KILLSWITCH = 3
EXIT_DISABLED_PATH = 4
EXIT_NO_TRANSCRIPT = 5
EXIT_INTERPRETER_TOO_OLD = 6

MIN_PYTHON = (3, 11)

#: Verbatim from `daemon.ts::buildWrapperScript`, which is verbatim from
#: `hooks/session-start.sh`. Positional args, not env, so the wrapper text is
#: identical for every caller and a snapshot test can pin it.
WRAPPER = "\n".join(
    [
        'SID="$1"; CWD="$2"; PY="$3"; LOG="$4"; PIDF="$5"; PREFIX="$6"; shift 6',
        'echo $$ >"$PIDF"',
        'SHUTDOWN="/tmp/${PREFIX}-watcher-${SID}.shutdown"',
        "RESTART_COUNT=0",
        "WINDOW_START=$(date +%s)",
        'CHILD_PID=""',
        "trap '[ -n \"$CHILD_PID\" ] && kill -TERM \"$CHILD_PID\" 2>/dev/null; exit 0' TERM INT",
        "while true; do",
        '  [ -f "$SHUTDOWN" ] && exit 0',
        "  NOW=$(date +%s)",
        "  if [ $((NOW - WINDOW_START)) -ge 60 ]; then",
        "    WINDOW_START=$NOW",
        "    RESTART_COUNT=0",
        "  fi",
        '  if [ "$RESTART_COUNT" -ge 5 ]; then',
        '    echo "[$(date -u +%FT%TZ)] tap: too many restarts in 1min, giving up" >>"$LOG"',
        "    exit 1",
        "  fi",
        '  "$PY" -m tap watch --session-id "$SID" --cwd "$CWD" "$@" >>"$LOG" 2>&1 &',
        "  CHILD_PID=$!",
        '  wait "$CHILD_PID" 2>/dev/null || true',
        '  CHILD_PID=""',
        '  [ -f "$SHUTDOWN" ] && exit 0',
        "  RESTART_COUNT=$((RESTART_COUNT + 1))",
        "  sleep 5",
        "done",
    ]
)


def _looks_like_the_uploader(pid: int) -> bool:
    """Ask the OS what the process IS before believing a /tmp filename.

    Same reasoning as `probe.cli.capture._looks_like_the_uploader`: /tmp is
    world-writable, so anyone can plant a pid file, and pid reuse produces
    the same accident with nobody being malicious. A name check is what stops
    `already-running` from being claimed on an unrelated process.
    """
    try:
        # Fixed binary, no shell: argv is ours end to end.
        completed = subprocess.run(
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "tap" in completed.stdout


def daemon_state(session_id: str) -> tuple[str, int | None]:
    """`("running" | "stale" | "none", pid)`.

    THREE states, not two. "stale" is a pid file naming a dead pid, or a live
    pid that is not the tap — both mean no daemon is watching, and both must
    be cleared rather than read as `already-running`.
    """
    pf = cfg.pid_file(session_id)
    try:
        raw = pf.read_text(encoding="utf-8").strip()
    except OSError:
        return ("none", None)
    try:
        pid = int(raw)
    except ValueError:
        return ("stale", None)
    if pid <= 0:
        return ("stale", None)
    try:
        os.kill(pid, 0)
    except OSError:
        return ("stale", pid)
    return ("running" if _looks_like_the_uploader(pid) else "stale", pid)


def _default_spawn(argv: list[str]) -> None:
    # Fixed /bin/sh, no shell interpolation: every element of argv is ours.
    child = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env={**os.environ, "PROBE_TAP_SOURCE": cfg.capture_source()},
    )
    del child  # Detached on purpose; nothing here waits on it.


def main(
    argv: list[str] | None = None,
    *,
    spawn: Callable[[list[str]], None] = _default_spawn,
    version_info: tuple[int, ...] | None = None,
) -> int:
    parser = argparse.ArgumentParser(prog="tap start")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--plugin-root", required=False, default=None, type=Path)
    args = parser.parse_args(argv)

    if (version_info or sys.version_info)[:2] < MIN_PYTHON:
        running = ".".join(str(p) for p in (version_info or sys.version_info)[:3])
        print(
            f"tap: python {running} is below the required 3.11; not starting capture",
            file=sys.stderr,
        )
        return EXIT_INTERPRETER_TOO_OLD

    if cfg.killswitch_active():
        return EXIT_KILLSWITCH
    if cfg.cwd_disabled(args.cwd):
        return EXIT_DISABLED_PATH
    if not cfg.load_token():
        return EXIT_NOT_PAIRED
    if not args.transcript.exists():
        return EXIT_NO_TRANSCRIPT

    state, _pid = daemon_state(args.session_id)
    if state == "running":
        return EXIT_OK
    if state == "stale":
        with contextlib.suppress(OSError):
            cfg.pid_file(args.session_id).unlink()

    log_dir = cfg.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"{args.session_id}.log"

    # A resumed session's previous run leaves its sentinel behind on purpose
    # (session-end.sh never deletes one). With no live wrapper it is stale,
    # and a fresh wrapper's first check would exit on it immediately.
    with contextlib.suppress(OSError):
        cfg.shutdown_sentinel(args.session_id).unlink()

    spawn(
        [
            "/bin/sh",
            "-c",
            WRAPPER,
            "sh",
            args.session_id,
            str(args.cwd),
            sys.executable,
            str(log_file),
            str(cfg.pid_file(args.session_id)),
            cfg.watcher_prefix(),
            "--transcript",
            str(args.transcript),
        ]
    )
    return EXIT_OK
