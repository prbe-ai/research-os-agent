"""`python -m tap cloud-catchup`: start a background sync of a cloud agent's
threads (Capy) when a local coding agent starts a session.

A cloud agent runs on its vendor's servers, so no hook ever fires for it on
this machine; `probe <agent> sync` copies its threads on a 10-minute timer.
A local agent starting is a second, cheap moment to catch up (plan R13): when
this machine has a cloud agent connected and its last sync is older than
MIN_INTERVAL_S, the persistent `probe` CLI is started detached to sync.

Fail-open and bounded: no network here, it never waits for the sync, and it
never fails a session start. The CLI's own lock turns a run that overlaps the
timer into a no-op, and the killswitch (`.disabled`) is honoured here too.
"""

from __future__ import annotations

import math
import shutil
import subprocess
import time

from tap.harness_registry import FAMILY_CLOUD, get_registry

#: No catch-up when the last sync is younger than this: the timer runs every
#: 10 minutes, and a burst of session starts must not stack syncs.
MIN_INTERVAL_S = 300
#: The CLI's per-agent files in the capture state folder (cli/capy_cli.py).
SYNC_STATE = "sync-state.json"
KILLSWITCH = ".disabled"


def due(state_dir, connection_file: str, now: float) -> bool:
    if not (state_dir / connection_file).exists() or (state_dir / KILLSWITCH).exists():
        return False
    try:
        age = now - (state_dir / SYNC_STATE).stat().st_mtime
    except OSError:
        age = math.inf
    return age >= MIN_INTERVAL_S


def main(argv: list[str] | None = None) -> int:
    del argv
    try:
        probe = shutil.which("probe")
        if not probe:
            return 0
        now = time.time()
        for harness in get_registry().all():
            if harness.family != FAMILY_CLOUD or harness.capture is None:
                continue
            if not due(harness.capture.state_dir(), f"{harness.id}.json", now):
                continue
            subprocess.Popen(
                [probe, harness.id, "sync", "--quiet"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
    except Exception:  # noqa: BLE001 -- a session start never fails for this
        return 0
    return 0
