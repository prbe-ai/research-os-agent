"""Background auto-update: settings, the safe trigger, and the audit record.

THE TRIGGER BOUNDARY IS THE WHOLE POINT.

`probe update` replaces the installed tree in place (see updater.py's H8 note on
why its whole call graph is imported eagerly). That is fine when a human types
the command. It is NOT fine from an arbitrary CLI invocation, because `probe log`
runs inside training loops -- upgrading the package under hour nine of a run is a
confusing failure landing in the middle of somebody's experiment.

So auto-update fires from exactly ONE place: the plugin's SessionStart hook. And
even there it is spawned DETACHED. The hook is synchronous by contract (its
systemMessage cannot come from a background process) and the upgrade timeout is
300s, so applying inline would let a Claude Code session hang for up to five
minutes before you could type. Nothing is lost by deferring: a plugin update only
takes effect on restart anyway.

Because a detached process cannot report failure through the hook, every attempt
is RECORDED. Without that, an auto-updater that silently fails forever looks
exactly like one that works. `probe doctor` prints it.

stdlib only, and it stays that way: `probe.version_policy` -- which owns the paths
and constants this module and the plugin hook share -- is stdlib-only for the
hook's benefit, and a dependency here would defeat that.

(This docstring used to claim the hook imported THIS module. It never did: the
hook re-implemented the state read instead, which is the duplication
`version_policy` now removes.)
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from dataclasses import dataclass

from probe import version_policy

STATE_DIRNAME = version_policy.STATE_DIRNAME
STATE_FILENAME = version_policy.STATE_FILENAME
LOCK_FILENAME = version_policy.LOCK_FILENAME

#: A lock older than this is treated as abandoned. Comfortably longer than the
#: 300s upgrade timeout so a slow-but-live upgrade is never stolen from.
#:
#: Age is the right reaper for THIS lock -- an upgrade that outlives 900s is
#: genuinely stuck. It is the wrong reaper for the run lock in run_lock.py, where
#: a twenty-hour holder is the normal case; that one proves liveness instead.
STALE_LOCK_SECONDS = 900


# There was a `stable` channel here, meant as a legible promise that a new CLI
# would not land on a researcher mid-experiment. It was stored, passed on the
# command line and validated -- and never read by anything. `cli_latest()` always
# returned `manifest["cli"]["latest"]`, and the manifest has no `stable` key, so
# choosing it behaved exactly like `latest`. A setting that does nothing is worse
# than an absent one: it answers a real worry with a promise nothing keeps.
#
# If the worry needs answering, it needs a `stable` field in the manifest and a
# `cli_latest(manifest, channel)` that reads it -- not an enum.


@dataclass(frozen=True)
class Attempt:
    """The outcome of one auto-update run, for `probe doctor` to print.

    BOTH halves, because an auto-update is two upgrades. The plugin's outcome
    used to live only in the printed lines, which a detached run sends to
    /dev/null -- so a plugin that had silently stopped updating looked exactly
    like one that worked. That is the failure mode this record exists to
    prevent, one layer down.
    """

    at: int
    ok: bool
    detail: str = ""
    from_version: str | None = None
    to_version: str | None = None
    #: True when the plugin half has nothing to report -- it succeeded, or there
    #: was no Claude Code to update. A missing `claude` is a legitimate state for
    #: a CLI-only user, not an auto-update failure.
    plugin_ok: bool = True
    plugin_detail: str = ""
    plugin_version: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.ok and self.plugin_ok

    def describe(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.at))
        landed = ", ".join(
            part
            for part in (
                f"CLI {self.to_version}" if self.to_version else "",
                f"plugin {self.plugin_version}" if self.plugin_version else "",
            )
            if part
        )
        notes = "; ".join(
            note
            for note in (
                "" if self.ok else (self.detail or "unknown error"),
                f"plugin: {self.plugin_detail}" if self.plugin_detail else "",
            )
            if note
        )
        if self.succeeded:
            out = f"success -> {landed} ({when})" if landed else f"success ({when})"
            return f"{out}: {notes}" if notes else out
        # On a failure the versions are where things are STUCK, not where they
        # landed -- "FAILED -> plugin 0.1.0" reads like 0.1.0 was the goal.
        out = f"FAILED ({when})"
        if notes:
            out += f": {notes}"
        return f"{out} — still at {landed}" if landed else out


#: Why an eligible upgrade was not applied. These are NOT failures -- each one is
#: the system working correctly -- but they are indistinguishable from a dead
#: auto-updater unless they are recorded, which is the whole point of `Skip`.
SKIP_RUN_IN_FLIGHT = "run in flight"
SKIP_NOT_A_TTY = "not an interactive terminal"
SKIP_HOT_PATH_COMMAND = "hot-path command"
SKIP_LIVENESS_UNPROVABLE = "run liveness unprovable on this platform"


@dataclass(frozen=True)
class Skip:
    """The last time an available upgrade was deliberately NOT applied.

    A SIBLING of `last_attempt`, never a replacement for it. "We skipped 214
    times because this box has been training all week" and "the last real upgrade
    succeeded on Tuesday" are different facts, and `probe doctor` needs both: with
    only the second, a machine that is correctly idle for six days is
    byte-identical to one whose auto-updater died.

    `count` accumulates while the reason is unchanged, so a fortnight-long sweep
    reads as one durable state rather than as the most recent of many.
    """

    at: int
    reason: str
    count: int = 1
    available: str | None = None  # the version we would have moved to

    def describe(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.at))
        times = "once" if self.count == 1 else f"{self.count}x"
        target = f" -> {self.available}" if self.available else ""
        return f"skipped {times}{target} — {self.reason} (last: {when})"


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    last_attempt: Attempt | None = None
    last_skip: Skip | None = None


# One definition, shared with the plugin hook. See version_policy's docstring for
# why the hook cannot simply import this module.
state_dir = version_policy.state_dir
state_path = version_policy.state_path
lock_path = version_policy.lock_path
_read = version_policy.read_state


def load() -> Settings:
    """Current settings. Fail-soft: unreadable or corrupt state reads as OFF,
    because defaulting a data-egress-adjacent background process to ON when we
    cannot tell what the user chose is the wrong direction to guess."""
    raw = _read()
    attempt_raw = raw.get("last_attempt")
    attempt = None
    if isinstance(attempt_raw, dict):
        try:
            attempt = Attempt(
                at=int(attempt_raw["at"]),
                ok=bool(attempt_raw["ok"]),
                detail=str(attempt_raw.get("detail", "")),
                from_version=attempt_raw.get("from_version"),
                to_version=attempt_raw.get("to_version"),
                # Absent on records written before the plugin half was tracked.
                # Defaulting to True keeps an old success reading as a success.
                plugin_ok=bool(attempt_raw.get("plugin_ok", True)),
                plugin_detail=str(attempt_raw.get("plugin_detail", "")),
                plugin_version=attempt_raw.get("plugin_version"),
            )
        except (KeyError, TypeError, ValueError):
            attempt = None
    skip_raw = raw.get("last_skip")
    skip = None
    if isinstance(skip_raw, dict):
        try:
            skip = Skip(
                at=int(skip_raw["at"]),
                reason=str(skip_raw["reason"]),
                # Absent on records written before counting existed. One is the
                # reading that preserves the old meaning: we saw exactly this.
                count=int(skip_raw.get("count", 1)),
                available=skip_raw.get("available"),
            )
        except (KeyError, TypeError, ValueError):
            skip = None
    # A `channel` key written by an older CLI is ignored, not migrated away: the
    # state file is also read by the plugin hook, which may be older than us.
    # That tolerance is the forward-compatibility rule stated in version_policy:
    # additive keys only, unknown keys ignored, missing keys defaulted to the
    # reading that preserves old behaviour.
    return Settings(
        enabled=bool(raw.get("enabled", False)),
        last_attempt=attempt,
        last_skip=skip,
    )


def _write(payload: dict) -> None:
    # Atomic replace via a UNIQUE temp name. A half-written state file read by a
    # concurrent SessionStart parses as "off" and silently stops auto-updating --
    # and the old fixed `autoupdate.json.tmp` was SHARED by every writer here, so
    # two of them racing could produce exactly that. With skip records added
    # there are now three writers, which is what made the fix load-bearing.
    version_policy.atomic_write_json(state_path(), payload)


@contextmanager
def _state_write_lock():
    """Serialize read-modify-write over the state file.

    Atomic replacement makes each WRITE whole; it does nothing for the read that
    preceded it. `save()`, `record_attempt()` and `record_skip()` each read the
    file, change one key and write it all back, so without this an updater can
    read `enabled=true`, race a `save(enabled=false)`, and write its attempt back
    with the stale `true` -- silently undoing the user's opt-out.

    Advisory and best-effort: if the lock cannot be taken the write still
    happens, because losing a diagnostic is better than dropping one.
    """
    handle = None
    try:
        import fcntl

        path = state_dir() / "state.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path, "a+")  # noqa: SIM115 -- released in the finally
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except Exception:  # noqa: BLE001 -- no fcntl, or an unwritable dir
        if handle is not None:
            handle.close()
            handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                handle.close()  # releases the flock
            except OSError:
                pass


def save(*, enabled: bool) -> Settings:
    with _state_write_lock():
        raw = _read()
        raw["enabled"] = bool(enabled)
        _write(raw)
    return load()


def record_attempt(attempt: Attempt) -> None:
    """Persist the outcome of one run. This is the ONLY way a detached upgrade
    can tell anyone what happened."""
    with _state_write_lock():
        raw = _read()
        raw["last_attempt"] = {
            "at": attempt.at,
            "ok": attempt.ok,
            "detail": attempt.detail,
            "from_version": attempt.from_version,
            "to_version": attempt.to_version,
            "plugin_ok": attempt.plugin_ok,
            "plugin_detail": attempt.plugin_detail,
            "plugin_version": attempt.plugin_version,
        }
        # A real attempt CLEARS the skip record. Leaving a week-old "skipped, run
        # in flight" beside a fresh success would read as though we were blocked.
        raw.pop("last_skip", None)
        _write(raw)


def record_skip(reason: str, *, available: str | None = None, now: int | None = None) -> None:
    """Note that an available upgrade was deliberately not applied.

    Writes to `last_skip`, NEVER to `last_attempt`: the two answer different
    questions and doctor prints both. Consecutive skips for the same reason
    increment a counter instead of appending, so a fortnight-long sweep costs one
    small field rather than an unbounded log nothing prunes.

    Never raises. This runs on the CLI's every-command path, and a diagnostic
    that can break `probe log` is worse than no diagnostic.
    """
    try:
        stamp = int(time.time()) if now is None else now
        with _state_write_lock():
            raw = _read()
            previous = raw.get("last_skip")
            count = 1
            if isinstance(previous, dict) and previous.get("reason") == reason:
                try:
                    count = int(previous.get("count", 1)) + 1
                except (TypeError, ValueError):
                    count = 1
            raw["last_skip"] = {
                "at": stamp,
                "reason": reason,
                "count": count,
                "available": available,
            }
            _write(raw)
    except Exception:  # noqa: BLE001 -- see docstring
        pass


#: The spawner puts its own pid here so the detached child can wait for it.
WAIT_FOR_PID_ENV = "PROBE_AUTOUPDATE_WAIT_PID"

#: Upper bound on that wait. The parent is the CLI invocation that spawned us,
#: which is short-lived -- the one long-running command, `probe exec`, is on the
#: denylist and never spawns. The bound only matters if that ever stops being
#: true, and the run-lock recheck after the wait is the real backstop.
WAIT_FOR_PID_TIMEOUT = 1800


def pid_is_alive(pid: int) -> bool:
    """Signal 0 probes for existence without delivering anything."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True  # unknown -> assume alive, which delays rather than clobbers
    return True


def wait_for_pid_exit(pid: int, *, timeout: float = WAIT_FOR_PID_TIMEOUT) -> bool:
    """Block until `pid` exits. True if it did, False on timeout.

    THIS IS WHY IT EXISTS. The version check runs in Typer's root callback, which
    fires BEFORE the command is dispatched, and this CLI imports lazily all over
    the place -- `from ..sdk.run import Run` inside a function, and many more. So
    a naive spawn has `uv tool upgrade` replacing files on disk while the command
    that triggered it is still reaching for them, producing a `ModuleNotFoundError`
    from a command that has worked every day for a year. Once, unreproducibly, and
    only in the minutes after a release.

    The updater already protects ITSELF from this by importing its whole call
    graph eagerly. That protects the wrong process; this protects the parent.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not pid_is_alive(pid):
            return True
        time.sleep(0.25)
    return not pid_is_alive(pid)


def acquire_lock() -> bool:
    """Single writer across concurrent Claude Code sessions.

    Several sessions starting at once would otherwise each spawn `uv tool
    upgrade` against the same install. O_EXCL is the whole mechanism; a lock
    older than STALE_LOCK_SECONDS is treated as abandoned so a crashed upgrade
    cannot wedge auto-update permanently.
    """
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        age = time.time() - path.stat().st_mtime
        if age > STALE_LOCK_SECONDS:
            path.unlink(missing_ok=True)
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    except OSError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def release_lock() -> None:
    try:
        lock_path().unlink(missing_ok=True)
    except OSError:
        pass
