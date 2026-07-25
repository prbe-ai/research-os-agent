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
from enum import StrEnum
from pathlib import Path

STATE_DIRNAME = "probe"
STATE_FILENAME = "autoupdate.json"
LOCK_FILENAME = "autoupdate.lock"

#: A lock older than this is treated as abandoned. Comfortably longer than the
#: 300s upgrade timeout so a slow-but-live upgrade is never stolen from.
STALE_LOCK_SECONDS = 900


class Channel(StrEnum):
    """Which releases auto-update follows.

    Borrowed from Claude Code, and it earns its place here for a specific
    reason: a researcher mid-experiment does not want a new CLI landing on them.
    `stable` is a legible promise in a way that "auto-update is on" is not.
    """

    LATEST = "latest"
    STABLE = "stable"


DEFAULT_CHANNEL = Channel.LATEST


@dataclass(frozen=True)
class Attempt:
    """The outcome of one auto-update run, for `probe doctor` to print."""

    at: int
    ok: bool
    detail: str = ""
    from_version: str | None = None
    to_version: str | None = None

    def describe(self) -> str:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(self.at))
        if self.ok and self.to_version:
            return f"success -> {self.to_version} ({when})"
        if self.ok:
            return f"success ({when})"
        return f"FAILED ({when}): {self.detail or 'unknown error'}"


@dataclass(frozen=True)
class Settings:
    enabled: bool = False
    channel: Channel = DEFAULT_CHANNEL
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
    channel = raw.get("channel")
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
            )
        except (KeyError, TypeError, ValueError):
            attempt = None
    return Settings(
        enabled=bool(raw.get("enabled", False)),
        channel=Channel(channel) if channel in tuple(Channel) else DEFAULT_CHANNEL,
        last_attempt=attempt,
    )


def _write(payload: dict) -> None:
    directory = state_dir()
    directory.mkdir(parents=True, exist_ok=True)
    # Atomic replace: a half-written state file read by a concurrent SessionStart
    # would parse as "off" and silently stop auto-updating.
    tmp = directory / f"{STATE_FILENAME}.tmp"
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
    tmp.replace(state_path())


def save(*, enabled: bool, channel: Channel) -> Settings:
    raw = _read()
    raw["enabled"] = bool(enabled)
    raw["channel"] = str(channel)
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
