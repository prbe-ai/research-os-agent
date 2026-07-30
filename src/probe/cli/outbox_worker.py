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
    """Non-blocking probe of the drain lease. True when no worker holds it."""
    import fcntl

    journal._ensure()
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

    Cheap by design -- one status/lease probe -- because every enqueue and
    every CLI invocation calls it. Returns True when a worker was spawned.
    """
    from ..sdk.journal import Journal

    journal = Journal(directory)
    if journal.paused or not journal.pending():
        return False
    status = Journal.read_status(directory)
    if status and status.get("auth_blocked_since"):
        return False  # pointless until someone re-authenticates (T2-A)
    if not _lease_is_free(journal):
        return False
    log_path = journal.dir / _LOG_NAME
    with open(log_path, "ab") as log:
        subprocess.Popen(  # noqa: S603 -- our own interpreter, our own module
            [sys.executable, "-m", "probe.cli.outbox_worker", str(journal.dir)],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,  # survives the parent CLI's exit
            close_fds=True,
            env=os.environ.copy(),  # PROBE_* env credentials inherit (T4 decision)
        )
    return True


def run(directory: str | None = None) -> int:
    """The worker loop. Exit codes: 0 empty, 3 auth-blocked, 4 paused."""
    import fcntl

    from ..sdk import journal as journal_module
    from ..sdk.journal import Journal, drain

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
