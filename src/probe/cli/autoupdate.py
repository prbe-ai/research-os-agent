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

stdlib only. This module is imported by the hook's Python, which is the system
interpreter, not the CLI's environment.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

STATE_DIRNAME = "probe"
STATE_FILENAME = "autoupdate.json"
LOCK_FILENAME = "autoupdate.lock"

#: A lock older than this is treated as abandoned. Comfortably longer than the
#: 300s upgrade timeout so a slow-but-live upgrade is never stolen from.
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


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    last_attempt: Attempt | None = None


def state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / STATE_DIRNAME


def state_path() -> Path:
    return state_dir() / STATE_FILENAME


def lock_path() -> Path:
    return state_dir() / LOCK_FILENAME


def _read() -> dict:
    try:
        loaded = json.loads(state_path().read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


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
    # A `channel` key written by an older CLI is ignored, not migrated away: the
    # state file is also read by the plugin hook, which may be older than us.
    return Settings(enabled=bool(raw.get("enabled", False)), last_attempt=attempt)


def _write(payload: dict) -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # Atomic replace: a half-written state file read by a concurrent SessionStart
    # would parse as "off" and silently stop auto-updating.
    tmp = directory / f"{STATE_FILENAME}.tmp"
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(state_path())


def save(*, enabled: bool) -> Settings:
    raw = _read()
    raw["enabled"] = bool(enabled)
    _write(raw)
    return load()


def record_attempt(attempt: Attempt) -> None:
    """Persist the outcome of one run. This is the ONLY way a detached upgrade
    can tell anyone what happened."""
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
    _write(raw)


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
