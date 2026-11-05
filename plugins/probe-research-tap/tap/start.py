"""`python -m tap start` — the ONE place a caller asks for a session to be captured: the gates, then the Probe service.

Callers (the Probe service for a pi session, the probe CLI's self-heal) get exit codes instead of
re-implementing the killswitch, `.disabled_paths`, token and transcript checks.
Past the gates, the Probe service records the session (`tap record`, plan A2):
nothing records here (service-only task 15). A service that does not take it is
EXIT_NOT_RECORDED, and the caller asks again later.

The interpreter check stays: the tap needs 3.11, and refusing up front, with the
version in the message, turns a silent failure into one line.
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
#: Past every gate, the Probe service did not take the session (none could start, an older one).
EXIT_NOT_RECORDED = 7

MIN_PYTHON = (3, 11)

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
            ["/bin/ps", "-ww", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "tap" in completed.stdout


def _finishing(session_id: str) -> bool:
    """Its shutdown sentinel is down: the session's stop came (pi's quit, a
    SessionEnd) and its recorder is making its last pass, if it has not ended."""
    return cfg.shutdown_sentinel(session_id).exists()


def _hosted(session_id: str) -> int | None:
    """The service's pid when it records THIS session now (its marker, then the
    service asked: a marker outlives a recorder that ended).

    None while the session is finishing (`_finishing`): its recorder still
    answers `recording` until its last pass ends, then FINALIZEs. A start in
    that window is a resume (pi `--continue` right after a quit): read as
    recorded, it returned without clearing the sentinel or asking the service
    again, and the recorder ended with nothing recording the resumed session.
    So it reads as not recorded, and `main` clears the sentinel and asks the
    service, which takes the session over from its finishing recorder."""
    from tap import record_client

    if _finishing(session_id):
        return None
    pid = record_client.hosted_pid(session_id)
    if pid is None:
        return None
    return pid if record_client.recording(argparse.Namespace(session_id=session_id)) == 0 else None


def _record(args) -> bool:
    from tap import record_client

    return record_client.try_record(
        argparse.Namespace(session_id=args.session_id, cwd=args.cwd, transcript=args.transcript,
                           transcript_dir=None, plugin_root=args.plugin_root,
                           transcript_identity=getattr(args, "transcript_identity", None))
    )


def daemon_state(session_id: str) -> tuple[str, int | None]:
    """`("running" | "stale" | "none", pid)`.

    THREE states, not two. "stale" is a pid file naming a dead pid, or a live
    pid that is not the tap — both mean no daemon is watching, and both must
    be cleared rather than read as `already-running`. No pid file, but the
    Probe service records the session (its marker, plan A2): "running", the
    service's pid.
    """
    pf = cfg.pid_file(session_id)
    try:
        raw = pf.read_text(encoding="utf-8").strip()
    except OSError:
        hosted = _hosted(session_id)
        return ("running", hosted) if hosted is not None else ("none", None)
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


def main(
    argv: list[str] | None = None,
    *,
    version_info: tuple[int, ...] | None = None,
    record: Callable[[argparse.Namespace], bool] = _record,
) -> int:
    parser = argparse.ArgumentParser(prog="tap start")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--cwd", required=True, type=Path)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--plugin-root", required=False, default=None, type=Path)
    # The file the Probe service accepted for a fenced caller (`dev:ino`): its recorder reads that one only.
    parser.add_argument("--transcript-identity", required=False, default=None)
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

    # The Probe service records it (plan A2); a sentinel a finished run left must not stop it.
    with contextlib.suppress(OSError):
        cfg.shutdown_sentinel(args.session_id).unlink()
    return EXIT_OK if record(args) else EXIT_NOT_RECORDED
