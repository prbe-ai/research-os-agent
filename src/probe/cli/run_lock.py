"""Is an experiment running on this box right now?

WHY THIS EXISTS.

`probe update` replaces the installed tree in place. Doing that under hour nine of
a training run is a confusing failure landing in the middle of somebody's
experiment, which is why auto-update used to fire from exactly one place -- the
plugin's SessionStart hook -- where it was uncorrelated with training.

Triggering from CLI invocations removes that accident. The more a training loop
shells out to `probe log`, the more likely one of those calls is the one that
finds the cache stale. Trigger and hazard become correlated, so the hazard needs
naming directly instead of being avoided by luck.

WHY THE COMMAND DENYLIST IS NOT ENOUGH.

The denylist in main.py stops `probe log` from TRIGGERING an upgrade. It cannot
see a run in a DIFFERENT process. Type `probe ls` in a second terminal while a
training run is going and the denylist has no opinion at all -- `ls` is not a
hot-path command and its stdout is a terminal, so the gate opens and the upgrade
lands on the run anyway. Only a lock shared across processes closes that.

THE SHAPE: PER-RUN ENTRIES, NOT ONE FILE.

Concurrent runs on one box are normal -- sweeps, multi-GPU, several notebooks. A
single lock file means run B's exit releases run A's protection. So each run owns
one entry and "is anything live" is a scan.

TWO MECHANISMS, BECAUSE THE SURFACES GENUINELY DIFFER.

  flock   The SDK and `probe exec` both own a process for the whole life of the
          run. They hold an flock, and the KERNEL releases it on exit -- including
          SIGKILL, OOM-kill, and panic. No reaper, no heartbeat, no cleanup
          command, and no way for a crashed run to wedge auto-update forever.
          This is the property this design rests on.

  lease   The bare `probe run start` ... `probe run end` bracket has nothing of
          ours alive in between. `run start` exits in milliseconds, so its pid is
          dead while the run it represents is very much alive -- pid liveness
          would either reap the lock instantly (protecting nothing) or never
          (wedging forever). A lease with an expiry is the honest answer, and it
          renews off traffic the run already sends: every run-scoped CLI write
          touches it.

The lease tier is BEST EFFORT and says so. A bare-bracket run that goes quiet
past LEASE_SECONDS -- a long eval, a slow checkpoint -- looks finished. Nothing
short of a live process fixes that, and pretending otherwise would be worse than
documenting it.

FAILS CLOSED.

Everything else in this subsystem fails open, on the principle that a broken
version check must never block a command. This one inverts that on purpose,
because the costs are asymmetric: skipping an upgrade costs one TTL, and applying
one into a live run costs somebody's afternoon. Unreadable state, an unexpected
error, or a platform where liveness cannot be proven at all all read as "a run
may be live".
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from probe import version_policy

RUNS_DIRNAME = "runs"
FLOCK_SUFFIX = ".flock"
LEASE_SUFFIX = ".lease"

#: How long a bare-bracket lease stays valid without renewal. Long enough that an
#: ordinary logging cadence renews it without thinking; short enough that a run
#: killed between `start` and `end` frees the box within the hour rather than
#: never. Renewal is free -- it rides on writes the run already makes.
LEASE_SECONDS = 1800

#: Set to "1" to assert no run is live regardless of state. For tests and for a
#: user who knows their box is idle and wants the stuck lease gone now.
OVERRIDE_ENV = "PROBE_ASSUME_NO_RUNS"

#: Above this many entries, stop scanning and report live. Far more than any
#: real concurrent-run count (a big sweep is tens), so crossing it means cleanup
#: has failed -- and the every-command path must not pay for that.
MAX_SCAN_ENTRIES = 512


def runs_dir() -> Path:
    return version_policy.state_dir() / RUNS_DIRNAME


def _safe_name(run_id: str) -> str:
    """A filesystem-safe stem for a run ref.

    Run refs are ids or slugs and can carry `/`. Collapsing the unsafe characters
    is enough: two refs mapping to one name would merely share an entry, which is
    conservative in the direction this module already errs.
    """
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in run_id) or "run"


def _fcntl():
    """The fcntl module, or None where it does not exist (Windows).

    Imported lazily and reported honestly rather than swallowed. The outbox does
    swallow it -- `_kick_drainer` catches everything because delivery is
    best-effort -- but doing that HERE would invert the fail-closed rule on
    exactly the platform where we have no lock at all.
    """
    try:
        import fcntl

        return fcntl
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# flock tier: SDK and `probe exec`.
# ---------------------------------------------------------------------------


class RunLock:
    """An flock held for as long as this object is open.

    Keep a reference for the run's lifetime. Dropping it, closing it, or dying
    releases the lock -- the last of those being the point.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.path = runs_dir() / f"{_safe_name(run_id)}{FLOCK_SUFFIX}"
        self._handle = None

    def acquire(self) -> bool:
        """Take the lock. False when it cannot be taken, which is never fatal --
        an unprotected run is worse than a failed run, so callers proceed."""
        module = _fcntl()
        if module is None:
            return False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.path, "a+")  # noqa: SIM115 -- held on purpose
        except OSError:
            return False
        try:
            module.flock(handle.fileno(), module.LOCK_EX | module.LOCK_NB)
        except (OSError, ValueError):
            handle.close()
            return False
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": os.getpid(), "run": self.run_id, "at": int(time.time())}))
            handle.flush()
        except OSError:
            pass  # the LOCK is the signal; its contents are only for humans
        self._handle = handle
        return True

    def release(self) -> None:
        """Drop the lock and remove the entry. Idempotent, never raises."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.close()  # closing the fd releases the flock
        except OSError:
            pass
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass

    def __enter__(self) -> RunLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def acquire(run_id: str) -> RunLock | None:
    """Take a process-held lock for `run_id`, or None if it could not be taken.

    Never raises: instrumentation must not be able to break the run it measures.
    """
    try:
        lock = RunLock(run_id)
        return lock if lock.acquire() else None
    except Exception:  # noqa: BLE001 -- see docstring
        return None


# ---------------------------------------------------------------------------
# lease tier: the bare `run start` / `run end` bracket.
# ---------------------------------------------------------------------------


def touch_lease(run_id: str, *, seconds: int = LEASE_SECONDS) -> None:
    """Start or renew the lease for `run_id`. Never raises.

    Called by `probe run start` and again by every run-scoped write the run
    makes, which is what turns renewal into something nobody has to remember.
    """
    try:
        path = runs_dir() / f"{_safe_name(run_id)}{LEASE_SUFFIX}"
        version_policy.atomic_write_json(
            path, {"run": run_id, "expires_at": int(time.time()) + int(seconds)}
        )
    except Exception:  # noqa: BLE001
        pass


def renew_lease_if_stale(run_id: str, *, seconds: int = LEASE_SECONDS) -> None:
    """Extend an EXISTING lease once it is half spent. Never raises.

    Called from the CLI's shared run-handle path, so every run-scoped write a
    detached run makes keeps it alive. Two deliberate restrictions:

    - Only renews a lease that already exists. A run holding an flock (SDK,
      `probe exec`) must not also sprout a lease file it will never clear.
    - Only writes past the halfway mark. `probe log` in a tight training loop
      would otherwise perform a small write on every single call, and this sits
      on the every-command path where a prior perf review already required O(1).
    """
    try:
        path = runs_dir() / f"{_safe_name(run_id)}{LEASE_SUFFIX}"
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return  # no lease here: an flock-tier run, or none at all
        if not isinstance(data, dict):
            return
        expires_at = float(data.get("expires_at", 0))
        if expires_at - time.time() > (seconds / 2):
            return  # still comfortably valid; skip the write
        touch_lease(run_id, seconds=seconds)
    except Exception:  # noqa: BLE001
        pass


def clear_lease(run_id: str) -> None:
    """Drop the lease at `probe run end`. Never raises."""
    try:
        (runs_dir() / f"{_safe_name(run_id)}{LEASE_SUFFIX}").unlink(missing_ok=True)
    except OSError:
        pass


def _lease_is_live(path: Path, now: float) -> bool:
    """An unreadable or malformed lease counts as LIVE.

    Fail-closed applies per entry, not only to the directory: a truncated lease
    is exactly as uninformative as an unreadable directory, and guessing "dead"
    there is guessing in the expensive direction.
    """
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return True
    if not isinstance(data, dict):
        return True
    try:
        return float(data.get("expires_at", 0)) > now
    except (TypeError, ValueError):
        return True


def _flock_is_held(path: Path) -> bool:
    """Probe without disturbing the holder.

    Take the lock non-blocking; success means nobody had it, so release at once
    and report dead. A second `open()` produces a new open file description, so
    this reads correctly even when the holder is THIS process. Mirrors
    `outbox_worker._lease_is_free`, inverted.
    """
    module = _fcntl()
    if module is None:
        return True  # cannot prove absence -> assume presence
    try:
        handle = open(path, "a+")  # noqa: SIM115 -- closed below
    except OSError:
        return True
    try:
        module.flock(handle.fileno(), module.LOCK_EX | module.LOCK_NB)
    except BlockingIOError:
        return True  # somebody holds it: a live run
    except (OSError, ValueError):
        return True  # unknown -> fail closed
    else:
        try:
            module.flock(handle.fileno(), module.LOCK_UN)
        except (OSError, ValueError):
            pass
        # Nobody held it. The file is a leftover from a process that died; drop
        # it so the directory does not grow without bound.
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# The question the apply gate asks.
# ---------------------------------------------------------------------------


def any_live() -> bool:
    """True when a run may be in flight. FAILS CLOSED.

    Every uncertainty resolves to True: an unreadable directory, an unexpected
    error, a malformed lease, or a platform with no fcntl at all. The only False
    is a directory we read successfully in which nothing was held or unexpired.
    """
    if os.environ.get(OVERRIDE_ENV) == "1":
        return False
    try:
        directory = runs_dir()
        if not directory.is_dir():
            return False  # never any runs here: the one confident False
        if _fcntl() is None:
            # Windows. The lease tier would still work, but the flock tier is
            # what SDK and `exec` runs use, and we cannot see those at all --
            # so this platform cannot answer the question. See the module
            # docstring: unprovable reads as live.
            return True
        now = time.time()
        entries = sorted(directory.iterdir())
    except OSError:
        return True
    if len(entries) > MAX_SCAN_ENTRIES:
        # This runs on the CLI's every-command path. A directory that has grown
        # past any plausible number of concurrent runs means something is wrong
        # with cleanup, and walking all of it would make an ordinary command
        # slow in proportion to the mess. Refuse rather than scan: unknown reads
        # as live, same as every other uncertainty here.
        return True
    for entry in entries:
        try:
            if entry.name.endswith(FLOCK_SUFFIX):
                if _flock_is_held(entry):
                    return True
            elif entry.name.endswith(LEASE_SUFFIX):
                if _lease_is_live(entry, now):
                    return True
                try:
                    entry.unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception:  # noqa: BLE001 -- one bad entry must not read as "all clear"
            return True
    return False


def live_runs() -> list[str]:
    """Names of the runs currently holding the box, for `probe doctor`.

    Diagnostic only. `any_live()` is the gate; this exists so a user can see WHY
    their box has not updated in six days without guessing.
    """
    found: list[str] = []
    try:
        directory = runs_dir()
        if not directory.is_dir():
            return found
        now = time.time()
        for entry in sorted(directory.iterdir()):
            if entry.name.endswith(FLOCK_SUFFIX) and _flock_is_held(entry):
                found.append(entry.name[: -len(FLOCK_SUFFIX)])
            elif entry.name.endswith(LEASE_SUFFIX) and _lease_is_live(entry, now):
                found.append(entry.name[: -len(LEASE_SUFFIX)])
    except OSError:
        pass
    return found
