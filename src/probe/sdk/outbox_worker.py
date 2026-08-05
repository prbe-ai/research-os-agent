"""The detached outbox drainer: wake on enqueue, drain until empty, exit.

Model (eng review 2026-07-29, 3A): the FIRST enqueue forks one detached
worker; later enqueues see the live flock lease and skip, so a training loop
queueing hundreds of writes forks once per idle period, not per write. The
worker loops with capped backoff until the journal is empty, then exits --
there is no daemon to install or forget.

Hard stops, so this can never become the tap's zombie-uploader pitfall:
  * 401/403 -> the drain reports auth-blocked; ops stay queued untouched
    (T2-A) and the worker EXITS -- it never retries with rejected credentials.
  * `probe outbox pause` -> exits at the next loop turn. This is the outbox's
    OWN switch; it is deliberately not the capture-consent killswitch.

A worker that dies (crash, reboot) is re-kicked by the next probe command of
any kind; `probe run end` remains the synchronous, run-scoped barrier.

Lives in the SDK (parity F1, docs/2026-08-04-outbox-miles-parity.md) so both
CLI commands and ``Client(async_writes=True)`` writers kick the same worker;
``probe.cli.outbox_worker`` stays behind as a runnable shim because workers
spawned by older releases exec that module path.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

_BACKOFF_START_SECONDS = 2.0
_BACKOFF_CAP_SECONDS = 300.0
_LOG_NAME = "drainer.log"


def _lease_path(journal) -> str:
    # The WORKER lease, distinct from the per-pass .drain.lock: a worker holds
    # this for its whole life, so the between-passes gap never looks "free" to
    # maybe_spawn and forks a duplicate.
    return str(journal.dir / ".worker.lock")


def _lease_is_free(journal) -> bool:
    """Non-blocking probe of the worker lease. True when no worker holds it.
    The journal directory is known to exist (the caller read its status.json)."""
    import fcntl

    try:
        handle = open(_lease_path(journal), "a+")
    except OSError:
        return False
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return True
    except BlockingIOError:
        return False
    finally:
        handle.close()


def maybe_spawn(directory: str | None = None) -> bool:
    """Fork a detached worker when there is work and nobody owns the lease.

    O(1) by requirement, not aspiration: this runs after every enqueue and on
    every CLI invocation (including `probe log` in training loops), so it
    reads ONLY status.json plus one lock probe -- never the op files
    themselves (perf review: the old pending() call parsed the whole queue).
    Returns True when a worker was spawned.
    """
    from .journal import Journal

    status = Journal.read_status(directory)
    if (
        not status
        or not status.get("pending")
        or status.get("paused")
        or status.get("auth_blocked_since")  # pointless until re-auth (T2-A)
    ):
        return False
    journal = Journal(directory)
    if journal.paused or not _lease_is_free(journal):
        return False
    log_path = journal.dir / _LOG_NAME
    # 0o600 explicitly: the log carries drain errors and tracebacks, and the
    # journal's everything-0o600 invariant must not depend on the umask.
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        subprocess.Popen(  # noqa: S603 -- our own interpreter, our own module
            [sys.executable, "-m", "probe.sdk.outbox_worker", str(journal.dir)],
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            start_new_session=True,  # survives the parent CLI's exit
            close_fds=True,
            env=os.environ.copy(),  # PROBE_* env credentials inherit (T4 decision)
        )
    finally:
        os.close(log_fd)
    return True


def run(directory: str | None = None) -> int:
    """The worker loop. Exit codes: 0 empty, 3 auth-blocked, 4 paused."""
    import fcntl

    from . import journal as journal_module
    from .journal import Journal, drain

    journal = Journal(directory)
    journal._ensure()
    lease = open(_lease_path(journal), "a+")
    try:
        fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lease.close()
        return 0  # another worker owns this journal
    backoff = _BACKOFF_START_SECONDS
    while True:
        if journal.paused:
            return 4
        # Waits on the per-pass drain lock: a concurrent foreground
        # `probe outbox drain` finishes, then this pass sees what remains.
        report = drain(journal)
        if report.auth_blocked:
            print(
                f"auth-blocked; {report.remaining} op(s) kept queued "
                f"({journal_module.now_iso()})",
                flush=True,
            )
            return 3
        if report.remaining == 0:
            # Exit-race guard (red team): an enqueue during this final stretch
            # saw our lease held and skipped its spawn. Re-read status while
            # still holding the lease; anything new means another pass, not an
            # exit that strands the tail op until some future CLI command.
            fresh = Journal.read_status(str(journal.dir)) or {}
            if fresh.get("pending"):
                continue
            return 0
        if report.stopped_transient:
            print(
                f"transient failure, {report.remaining} left; retrying in "
                f"{backoff:.0f}s: {report.errors[-1] if report.errors else '?'}",
                flush=True,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_CAP_SECONDS)
        else:
            # Progress was made (e.g. dead letters moved aside); reset backoff.
            backoff = _BACKOFF_START_SECONDS


if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))
