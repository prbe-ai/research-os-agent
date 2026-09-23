"""Keeps the session's Probe daemon worker alive, from inside `tap watch`.

`tap watch` is already the per-session process with a lifecycle on every
harness (spawned at SessionStart, respawned by `ensure-daemon` on each prompt,
stopped at SessionEnd or when the transcript's reader disappears). The worker
rides it as a CHILD PROCESS -- not a thread, because it makes network calls and
must be killable without touching capture.

    tap watch tick ──> poll()
                         state == daemon (or `on` with shadow) and no child ─> spawn
                         child died ─> respawn, at most RESPAWNS_PER_MINUTE, and not
                           at all after it exits EXIT_DO_NOT_RESPAWN (another worker
                           holds the session, or the ledger is newer than the code)
                           until the switch moves
                         state left daemon ─> the child notices and exits itself
    tap watch exit ──> stop(): SIGTERM and return. The worker is in its OWN
                       process group, runs its bounded final cycle and exits on
                       its own -- capture's FINALIZE never waits on a model.

While the worker is down its lease lapses and the agent records; nothing here
has to tell anyone.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from tap import companion_lease as lease
from tap import companion_ledger as ledger_mod
from tap import companion_worker as worker

log = logging.getLogger("tap.companion.supervisor")

RESPAWNS_PER_MINUTE = 5
#: How long `stop()` waits for the worker before returning; it is not killed.
STOP_WAIT_SECONDS = 2


#: How often the shadow setting (a config read) is looked at again.
SHADOW_RECHECK_SECONDS = 60


class Supervisor:
    def __init__(self, *, session_id: str, transcript: Path, cwd: Path) -> None:
        self.session_id = session_id
        self.transcript = transcript
        self.cwd = cwd
        self.child: subprocess.Popen | None = None
        self.spawns: list[float] = []
        self.gave_up_in: str | None = None  # the state a do-not-respawn exit happened in
        self._shadow = False
        self._shadow_checked = float("-inf")
        try:
            ledger_mod.prune()
        except Exception:  # noqa: BLE001 - housekeeping must never stop capture
            log.exception("companion ledger prune failed")

    def _shadow_on(self) -> bool:
        now = time.monotonic()
        if now - self._shadow_checked >= SHADOW_RECHECK_SECONDS:
            self._shadow = worker.shadow_enabled()
            self._shadow_checked = now
        return self._shadow

    def wanted(self) -> bool:
        state = lease.session_state(self.session_id)
        if state != self.gave_up_in:
            self.gave_up_in = None
        else:
            return False
        if state == worker.STATE_DAEMON:
            return True
        # Shadow runs only under `on`: never `read` or `off`, which promise no
        # (or no recording) Probe traffic at all.
        return state == worker.STATE_FULL and self._shadow_on()

    def poll(self) -> None:
        """Called every tick. Never raises: capture must not die for the daemon."""
        try:
            if self.child is not None and self.child.poll() is not None:
                code = self.child.returncode
                log.info("companion worker exited (%s)", code)
                if code == worker.EXIT_DO_NOT_RESPAWN:
                    self.gave_up_in = lease.session_state(self.session_id)
                self.child = None
            if self.child is None and self.wanted():
                now = time.monotonic()
                self.spawns = [t for t in self.spawns if now - t < 60]
                if len(self.spawns) >= RESPAWNS_PER_MINUTE:
                    return
                self.spawns.append(now)
                self.child = subprocess.Popen(  # noqa: S603 - our own module, fixed argv
                    [
                        sys.executable,
                        "-m",
                        "tap",
                        "companion",
                        "--session-id",
                        self.session_id,
                        "--transcript",
                        str(self.transcript),
                        "--cwd",
                        str(self.cwd),
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=str(Path(worker.__file__).resolve().parent.parent),
                    env={**os.environ},
                    # Its own group: a signal to the capture daemon's group must not
                    # cut the worker's final cycle short.
                    start_new_session=True,
                )
                log.info("companion worker started (pid %s)", self.child.pid)
        except Exception:  # noqa: BLE001
            log.exception("companion supervisor poll failed")

    def stop(self) -> None:
        """Ask the worker to finish, and do not wait: its final cycle is bounded
        by the worker itself (and it exits when it sees this process is gone)."""
        child, self.child = self.child, None
        if child is None or child.poll() is not None:
            return
        try:
            child.send_signal(signal.SIGTERM)
            child.wait(timeout=STOP_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            pass  # still finishing on its own
        except Exception:  # noqa: BLE001
            log.exception("companion supervisor stop failed")
