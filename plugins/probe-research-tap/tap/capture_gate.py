"""Probe off means the session is not captured: the tap's own gate (tap 0.10.19).

The owner's decision (2026-10-24): while a session's switch (`probe session
state`, `/probe`) reads `off`, no transcript line written in that time is
uploaded, by anything; moving it back captures FROM THE MOVE, and what was
written while off is never sent later. The Probe service enforced it first
(CLI 0.307.0, `probe.service.capture_gate`), but only inside its own process:
every `tap watch` drains every session of the shared journal, so another
session's watch, an off session's own watch before it hands over (for its
whole life when sandboxed, under `PROBE_TAP_HOSTED=0`, with no service, or a
CLI on Python 3.10) still uploaded the off lines. This module is the same gate
in the tap itself, read by its own drains in every process:

    the switch      read as `probe.sdk.session_marker` reads it (`switch`):
                    the session's `<sid>.state`, its legacy files (never
                    `off`), `PROBE_SESSION_STATE`, `PROBE_SESSION_TRACKING`,
                    the machine's `defaults` in the Probe config, `full`.
                    FAILS CLOSED: a file that is there and cannot be read
                    now reads `off`
    the gap file    `<state>/sessions/<sid>.gaps`, the service's documented
                    format (`probe.service.capture_gate`): the session's off
                    stretches as transcript byte ranges `[from, to)`, `to`
                    null while still off. FAILS CLOSED: a file that is there
                    and does not read covers the whole session (`WHOLE`)
    every drain     `Journal.stage` asks `verdict` first: a session that
                    reads `off`, or has a gap still open, is BLOCKED (no new
                    batch; one staged before the switch moved may still
                    flush); otherwise every line whose START offset lies in
                    a gap is read past and sent as nothing (no event, no
                    title, no oversize placeholder), so the server's cursor
                    moves past it and no recorder can ever send it.
                    `reconcile.find_gaps` leaves a blocked session out, and
                    the watch loop asks nothing of the server about one
    the recorder    the session's OWN recorder writes the gap file
                    (`Recorder`): a `tap watch` of it, or a service from
                    before CLI 0.307.0 that hosts this tap. Seen `off`: a gap
                    opens where the switch was last seen on (0 at a first
                    look: what was not sent yet counts as off), so a later
                    flip back on never backfills, whichever process records
                    then; seen on again: the open gap closes at the
                    transcript's complete-line end now. A Probe service that
                    gates (CLI 0.307.0 and later) records its sessions
                    itself: there this module only gates (`host`)
    settling        a host's thread that settles a session (`settling`) is
                    never blocked for it: it stages from the cursor with the
                    gap lines skipped (the open gap runs to the end), and
                    FINALIZEs a session that ended while off

WRITERS. A gap file has one writer at a time: every write is a
read-modify-write under the session's lock file (`<sid>.gaps.lock`, an
exclusive `flock`, a one-byte lock on Windows), replacing the file whole
(written to a temporary file, flushed to disk, renamed). A writer only ever
OPENS a gap (none open), CLOSES the open one at a mark, drops the closed ones
the server's cursor is past (`Recorder.settled`), or (the service) merges what
it holds and drops what it settled: none ever overwrites a gap another writer
added. The service and a `tap watch` of the same session coexist for a moment
(a hand-over), and each takes the lock. Where locking itself is unavailable
(the lock file cannot be opened, `flock` answers ENOLCK or ENOTSUP: no lock
daemon on a network folder) a write goes ahead without it rather than not at
all; only "held elsewhere" waits.

WRITE FAILURES. A stretch the recorder saw and could not write yet (a full
disk, a read-only or unwritable sessions folder) stays in its memory
(`Recorder.held`) and counts in every verdict of its process (`_RECORDED`), as
a host's gaps do: the session stays blocked while a stretch is open, and its
lines are skipped once it closes, so its own drains never send them; the write
is tried again at every look until it lands.

Stdlib only; imports between the tap-core modules stay relative.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

try:
    import fcntl
except ImportError:  # Windows: `_lock`
    fcntl = None  # type: ignore[assignment]

#: What a host checks for: this tap gates its own drains, so the Probe service
#: installs no wraps of its own (`probe.service.capture_gate.install`).
NATIVE = 1

OFF = "off"
GAP_FILE_SUFFIX = ".gaps"
LOCK_SUFFIX = ".gaps.lock"
GAP_FILE_VERSION = 1
#: Past this many gaps the two oldest merge (what lay between them is dropped
#: too, never sent): the service's own bound.
MAX_GAPS = 64
#: What a gap file that is there and does not read covers: the whole session.
WHOLE = ((0, None),)
#: How long a writer waits for the session's lock before it gives up for now
#: (its caller tries again at its next look).
LOCK_WAIT_S = 5.0

# `probe.sdk.session_marker`'s switch, restated (neither package imports the
# other); `tests/test_tap_capture_gate.py` holds the two equal.
STATES = ("full", "daemon", "read-only", "off")
_READ_ONLY_WORDS = ("read", "read-only", "readonly", "read_only", "ro")
_OFF_WORDS = ("off", "0", "false", "no", "disabled")
_ON_WORDS = ("on", "1", "true", "yes", "enabled")
_SESSION_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-")

_CACHE_MAX = 4096
_CACHE_LOCK = threading.Lock()
_SWITCHES: dict[tuple, tuple] = {}
_GAP_FILES: dict[str, tuple] = {}
_LOCAL = threading.local()
#: A host's own gaps for a session (`host`): the Probe service's in-memory
#: record, which may be a moment ahead of its gap file.
_HOST: Callable[[str], object] | None = None
#: The stretches a `Recorder` of this process saw and has not written to the
#: gap file yet: `{sid: gaps}` (`Recorder.held`).
_RECORDED: dict[str, list] = {}
_RECORDED_LOCK = threading.Lock()


# -- where a session's files are ----------------------------------------------------


def valid_session_id(raw: object) -> bool:
    """`session_marker.valid_session_id`: safe to name a file after."""
    return isinstance(raw, str) and 8 <= len(raw) <= 200 and set(raw) <= _SESSION_CHARS


def _home() -> Path:
    home = os.environ.get("HOME")
    return Path(home) if home else Path(os.path.expanduser("~"))


def sessions_dir() -> Path:
    """`session_marker.sessions_dir()`, in this thread's environment (a hosted
    recorder's is its session's)."""
    xdg = os.environ.get("XDG_STATE_HOME")
    return (Path(xdg) if xdg else _home() / ".local" / "state") / "probe" / "sessions"


def config_path() -> Path:
    """`session_marker.config_path()`."""
    override = os.environ.get("PROBE_CONFIG_PATH")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else _home() / ".config") / "probe" / "config.json"


def gap_file(sid: str, sessions: Path | None = None) -> Path:
    return (sessions_dir() if sessions is None else sessions) / f"{sid}{GAP_FILE_SUFFIX}"


def _signature(path: Path) -> tuple | None:
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


# -- the switch ------------------------------------------------------------------


def normalize_state(raw: object) -> str | None:
    """`session_marker.normalize_state`."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if value in STATES:
        return value
    if value in _READ_ONLY_WORDS:
        return "read-only"
    if value in _OFF_WORDS:
        return OFF
    if value in _ON_WORDS:
        return "full"
    return None


def _legacy(value: object) -> str | None:
    """`session_marker._legacy_state_from_tracking`: on -> full, off -> read-only (never off)."""
    if isinstance(value, bool):
        return "full" if value else "read-only"
    if isinstance(value, str):
        word = value.strip().lower()
        if word in _OFF_WORDS:
            return "read-only"
        if word in _ON_WORDS:
            return "full"
    return None


def _ladder(sid: str, sessions: Path, config: Path) -> str:
    if valid_session_id(sid):
        try:
            raw = (sessions / f"{sid}.state").read_text(encoding="utf-8").strip().lower()
            if raw in STATES:
                return raw
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            return OFF  # there, and cannot be read now: fail closed
        try:
            legacy = (sessions / f"{sid}.tracking").read_text(encoding="utf-8").strip().lower()
        except (OSError, ValueError):
            legacy = None  # the legacy files never say `off`: the ladder below decides
        if legacy == "on":
            return "full"
        if legacy == "off":
            return "read-only"
        with contextlib.suppress(OSError):
            if (sessions / f"{sid}.off").is_file():
                return "read-only"
    explicit = normalize_state(os.environ.get("PROBE_SESSION_STATE"))
    if explicit is not None:
        return explicit
    legacy_env = _legacy(os.environ.get("PROBE_SESSION_TRACKING"))
    if legacy_env is not None:
        return legacy_env
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except ValueError:
        data = {}  # what the file says: a typo never stops recording (session_marker)
    except OSError:
        return OFF  # there, and cannot be read now: fail closed
    defaults = data.get("defaults") if isinstance(data, dict) else None
    if isinstance(defaults, dict):
        stored = normalize_state(defaults.get("session_state"))
        if stored is None:
            stored = _legacy(defaults.get("session_tracking"))
        if stored is not None:
            return stored
    return "full"


def switch(sid: object) -> str:
    """The session's switch in this thread's environment, read again only when
    one of its files, the config, or an override changed (a stat each).
    `off` when it cannot be read (fail closed)."""
    try:
        sid = str(sid)
        sessions, config = sessions_dir(), config_path()
        files = (sessions / f"{sid}.state", sessions / f"{sid}.tracking", sessions / f"{sid}.off")
        signature = (
            *(_signature(f) if valid_session_id(sid) else None for f in files), _signature(config),
            os.environ.get("PROBE_SESSION_STATE"), os.environ.get("PROBE_SESSION_TRACKING"),
        )
        key = (str(sessions), sid, str(config))
        with _CACHE_LOCK:
            cached = _SWITCHES.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        state = _ladder(sid, sessions, config)
        with _CACHE_LOCK:
            if len(_SWITCHES) >= _CACHE_MAX:
                _SWITCHES.clear()
            _SWITCHES[key] = (signature, state)
        return state
    except Exception:  # noqa: BLE001 -- cannot tell: fail closed
        return OFF


def is_off(sid: object) -> bool:
    return switch(sid) == OFF


# -- gaps ------------------------------------------------------------------------


def _valid(raw: object) -> list[list] | None:
    """`[[from, to|None], ...]`, or None when one of them does not read."""
    if not isinstance(raw, list | tuple):
        return None
    out: list[list] = []
    for gap in raw:
        if not (isinstance(gap, list | tuple) and len(gap) == 2):
            return None
        start, end = gap
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            return None
        if end is not None and (not isinstance(end, int) or isinstance(end, bool) or end < start):
            return None
        out.append([start, end])
    return out


def normalized(gaps) -> list[list]:
    """In order of where they start, each once; an open gap last (it covers
    everything after its start, so one starting past it adds nothing)."""
    unique = {(g[0], g[1]) for g in (_valid(gaps) or [])}
    out: list[list] = []
    for start, end in sorted(unique, key=lambda g: (g[0], float("inf") if g[1] is None else g[1])):
        if out and out[-1][1] is None:
            break
        out.append([start, end])
    return out


def is_open(gaps) -> bool:
    return bool(gaps) and any(g[1] is None for g in gaps)


def in_gap(gaps, offset: int) -> bool:
    return any(start <= offset and (end is None or offset < end) for start, end in gaps)


def opened(gaps, mark: int) -> list[list]:
    """`gaps` with everything from `mark` on in one: an open gap starting
    later starts at `mark` instead (another writer's opening, seen later,
    never shortens it), one starting earlier stays as it is."""
    out = normalized(list(normalized(gaps)) + [[max(0, int(mark)), None]])
    while len(out) > MAX_GAPS:
        first, second = out.pop(0), out.pop(0)
        out.insert(0, [first[0], second[1]])
    return out


def closed(gaps, mark: int) -> list[list]:
    """`gaps` with the open one closed at `mark`. A mark before its start (a
    writer that saw the switch on before another saw it off again) leaves it
    open: the next look, with a mark of now, closes it."""
    return normalized([[g[0], int(mark) if g[1] is None and int(mark) >= g[0] else g[1]] for g in normalized(gaps)])


def read_gaps_raw(sid: str, sessions: Path | None = None) -> list[list] | None:
    """The gaps the session's gap file holds (none without one), read again
    only when the file changed. WHOLE for a file that is there and does not
    read (truncated, not JSON, another version, a gap that does not read):
    what it says, so remembered. None when it cannot be read NOW (an OSError
    but its absence): never remembered, tried again at the next call."""
    path = gap_file(sid, sessions)
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return []
    except OSError:
        return None
    signature = (st.st_mtime_ns, st.st_size, st.st_ino)
    with _CACHE_LOCK:
        cached = _GAP_FILES.get(str(path))
    if cached is not None and cached[0] == signature:
        return [list(g) for g in cached[1]]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        gaps = _valid(data.get("gaps")) if isinstance(data, dict) and data.get("v") == GAP_FILE_VERSION else None
        gaps = [list(g) for g in WHOLE] if gaps is None else normalized(gaps)
    except FileNotFoundError:
        return []
    except ValueError:  # UnicodeDecodeError included: what the file says
        gaps = [list(g) for g in WHOLE]
    except OSError:
        return None
    with _CACHE_LOCK:
        if len(_GAP_FILES) >= _CACHE_MAX:
            _GAP_FILES.clear()
        _GAP_FILES[str(path)] = (signature, gaps)
    return [list(g) for g in gaps]


def read_gaps(sid: str, sessions: Path | None = None) -> list[list]:
    """`read_gaps_raw`, FAILING CLOSED: a file that cannot be read now covers
    the whole session too."""
    gaps = read_gaps_raw(sid, sessions)
    return [list(g) for g in WHOLE] if gaps is None else gaps


# -- writing the gap file -----------------------------------------------------------


def _lock(fd: int) -> bool | None:
    """True locked, False held elsewhere, None: this folder cannot be locked
    at all (ENOLCK, ENOTSUP: no lock daemon on a network folder)."""
    if fcntl is None:
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    except OSError:
        return None


def _unlock(fd: int) -> None:
    with contextlib.suppress(OSError):
        if fcntl is None:
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def locked(sid: str, sessions: Path | None = None, wait: float | None = None):
    """The session's gap-file lock, held for one read-modify-write: every
    writer's (the Probe service's too). OSError when another writer holds it
    past `wait` (LOCK_WAIT_S; the caller tries again later). Where it cannot
    be had at all (its file cannot be opened, or the folder cannot be locked:
    `_lock`), the block runs without it: written unlocked, never not at all."""
    if not valid_session_id(sid):
        raise OSError(f"no gap file can be named for session {sid!r}")
    sessions = sessions_dir() if sessions is None else sessions
    sessions.mkdir(parents=True, exist_ok=True)
    path = sessions / f"{sid}{LOCK_SUFFIX}"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
    except OSError:
        fd = None  # a folder or another user's file stands there, no permission: unlocked
    if fd is None:
        yield
        return
    try:
        with contextlib.suppress(OSError):
            os.utime(path)  # fresh: `session_marker.prune` never removes a lock in use
        deadline = time.monotonic() + max(0.0, LOCK_WAIT_S if wait is None else wait)
        while True:
            held = _lock(fd)
            if held is not False:
                break  # locked, or this folder cannot be locked at all (unlocked)
            if time.monotonic() >= deadline:
                raise OSError(f"the gap file lock of session {sid} is held elsewhere")
            time.sleep(0.01)
        try:
            yield
        finally:
            if held:
                _unlock(fd)
    finally:
        os.close(fd)


def write_gaps(sid: str, gaps, transcript: object = None, sessions: Path | None = None) -> bool:
    """Replace the session's gap file with `gaps` (none: remove it), written
    to a temporary file of this user's alone, flushed to disk, then renamed
    over the old one. Under `locked`. False when it could not be written."""
    sessions = sessions_dir() if sessions is None else sessions
    path = gap_file(sid, sessions)
    gaps = normalized(gaps)
    if not gaps:
        try:
            path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True
    named = str(transcript) if isinstance(transcript, str | Path) and str(transcript) else None
    record = {"v": GAP_FILE_VERSION, "sid": sid, "transcript": named, "gaps": gaps, "updated_at": time.time()}
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        sessions.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        return False
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _update(sid: str, change: Callable[[list], list], transcript: object) -> list[list] | None:
    """One read-modify-write of the session's gap file under its lock. A file
    that does not read counts as WHOLE (so a change keeps it covering every
    line before it); one that cannot be read now is left alone. The gaps
    the file holds after it, or None when it could not be done now."""
    try:
        with locked(sid):
            current = read_gaps_raw(sid)
            if current is None:
                return None
            changed = normalized(change(current))
            if changed == current:
                return current
            return changed if write_gaps(sid, changed, transcript) else None
    except OSError:
        return None


def record_off(sid: str, mark: int, transcript: object = None) -> list[list] | None:
    """The session reads `off`: a gap opens at `mark` unless one is open."""
    return _update(sid, lambda gaps: opened(gaps, mark), transcript)


def record_on(sid: str, mark: int, transcript: object = None) -> list[list] | None:
    """The session reads on again: its open gap closes at `mark`."""
    return _update(sid, lambda gaps: closed(gaps, mark), transcript)


# -- the drains ---------------------------------------------------------------------


class Verdict(NamedTuple):
    #: Stage nothing new of the session now (its switch reads `off`, or a gap
    #: of it is still open).
    blocked: bool
    #: Its gaps: a line that starts in one is read past and sent as nothing.
    gaps: list


def host(gaps_of: Callable[[str], object] | None) -> None:
    """A host that records sessions itself (the Probe service) adds its own
    gaps of a session (`gaps_of(sid)`) to every verdict in this process, and
    records the gap files itself: no `Recorder` writes here. None: none."""
    global _HOST
    _HOST = gaps_of


def _stood_down() -> bool:
    """A Probe service from before this gate (CLI 0.307.0) wrapped this tap's
    drains and gates them itself (`_probe_capture_gate`): in its process
    this gate stands down, exactly as that service was reviewed."""
    try:
        from . import session_journal

        return bool(getattr(session_journal, "_probe_capture_gate", False))
    except Exception:  # noqa: BLE001
        return False


def host_records() -> bool:
    """Does a host of this process record the sessions' gap files (so the
    tap's own `Recorder` must not)?"""
    return _HOST is not None or _stood_down()


def _recorded(sid: str) -> list[list]:
    with _RECORDED_LOCK:
        return [list(g) for g in _RECORDED.get(sid) or []]


def _host_gaps(sid: str) -> list[list]:
    held = _recorded(sid)
    gaps_of = _HOST
    if gaps_of is None:
        return held
    try:
        gaps = _valid(gaps_of(sid) or [])
    except Exception:  # noqa: BLE001 -- cannot tell: the whole session
        gaps = None
    return held + ([list(g) for g in WHOLE] if gaps is None else gaps)


@contextlib.contextmanager
def settling(sid: str, gaps=None):
    """On this thread, `sid` is settled: never blocked, its lines in `gaps`
    (a list, or a callable giving one, read at each stage) and the host's
    skipped. The host decides its gaps here, not the gap file (it merged it)."""
    previous = getattr(_LOCAL, "settling", None)
    _LOCAL.settling = (sid, gaps)
    try:
        yield
    finally:
        _LOCAL.settling = previous


@contextlib.contextmanager
def switch_decided():
    """On this thread the caller decided about the switch itself (`probe
    import` skips a session whose own `.state` says `off`, never one that
    only takes the machine's default: history from before it changed): the
    gate goes by the gap files alone."""
    previous = getattr(_LOCAL, "switch_decided", False)
    _LOCAL.switch_decided = True
    try:
        yield
    finally:
        _LOCAL.switch_decided = previous


def verdict(sid: object) -> Verdict:
    """May a drain stage `sid` now, and which of its lines does it skip?"""
    if not isinstance(sid, str):
        return Verdict(False, [])
    context = getattr(_LOCAL, "settling", None)
    if context is not None and context[0] == sid:
        given = context[1]
        try:
            given = given() if callable(given) else given
            extra = _valid(given or [])
        except Exception:  # noqa: BLE001
            extra = None
        if extra is None:
            extra = [list(g) for g in WHOLE]
        return Verdict(False, normalized(extra + _host_gaps(sid)))
    if _stood_down():
        return Verdict(False, [])
    # A session id no file can be named for has no gap file (and no `.state`).
    gaps = normalized((read_gaps(sid) if valid_session_id(sid) else []) + _host_gaps(sid))
    off = False if getattr(_LOCAL, "switch_decided", False) else is_off(sid)
    return Verdict(off or is_open(gaps), gaps)


# -- the session's own recorder ------------------------------------------------------


def mark_now(path: object) -> int | None:
    """Where a gap starts or ends now: the transcript's size up to its last
    complete line (a line still being written belongs to after the move). 0
    with no transcript yet; None when it is there and cannot be read."""
    if not isinstance(path, str | Path):
        return 0
    from . import session_journal

    try:
        size = os.stat(path).st_size
        return int(session_journal.complete_line_end(Path(path), size))
    except FileNotFoundError:
        return 0
    except (OSError, ValueError):
        return None


class Recorder:
    """The session's own recorder's half (a `tap watch`, or a host that does
    not gate): looks at its switch (`look`, each pass and each second between
    passes) and records its off stretches in its gap file. A stretch it could
    not write yet stays in `held`, counts in this process's verdicts, and is
    written at a later look. Never raises."""

    def __init__(self, sid: str, transcript: object, log: Callable[[str], None] = lambda _m: None) -> None:
        self.sid, self.transcript, self.log = sid, transcript, log
        #: The complete-line end when the switch was last seen on (None: not
        #: seen on yet in this process).
        self.on_mark: int | None = None
        self._size: int | None = None
        self.off = False
        self._failing = False
        #: Stretches seen and not written to the gap file yet (open while off).
        self.held: list[list] = []
        #: Seen on at this mark while the gap file still holds an open stretch
        #: (another writer's, or one this recorder wrote): closed there once written.
        self.close_at: int | None = None

    def _mark(self) -> int | None:
        try:
            size = os.stat(self.transcript).st_size if isinstance(self.transcript, str | Path) else 0
        except FileNotFoundError:
            size = 0
        except OSError:
            return None
        if size == self._size and self.on_mark is not None:
            return self.on_mark
        mark = mark_now(self.transcript)
        if mark is not None:
            self._size = size
        return mark

    def look(self) -> bool:
        """True while the session reads `off` (or a stretch of it is still
        open). Seen off: a stretch opens where it was last seen on (0 at a
        first look), so what was written since, on or off, is never sent (fail
        closed: a line written on in that moment is lost, never one written
        off sent). Seen on: the open stretch closes at the transcript's mark now."""
        try:
            if is_off(self.sid):
                self.off = True
                self.close_at = None  # seen off again: nothing written while it was on closes now
                on_file = read_gaps_raw(self.sid)
                if not is_open(self.held) and not (on_file is not None and is_open(on_file)):
                    self.held = opened(self.held, self.on_mark or 0)
                self._write("off")
                return True
            mark = self._mark()
            self.off = False
            if mark is None:
                return self.blocked()  # cannot tell where: an open stretch stays open
            if is_open(self.held):
                self.held = closed(self.held, mark)
            on_file = read_gaps_raw(self.sid)
            if on_file is None or is_open(on_file):
                self.close_at = mark
            self._write("on")
            self.on_mark = mark
            return self.blocked()
        except Exception as exc:  # noqa: BLE001 -- never into capture; the drains still read the switch
            self.log(f"capture gate: session {self.sid}'s switch not looked at ({type(exc).__name__}: {exc})")
            return True

    def blocked(self) -> bool:
        return is_open(self.gaps())

    def _write(self, what: str) -> None:
        """Merge what it holds (and the close it owes) into the gap file."""
        if not self.held and self.close_at is None:
            self._publish()
            return
        held, close_at = [list(g) for g in self.held], self.close_at

        def change(gaps: list) -> list:
            out = normalized(list(gaps) + held)
            return closed(out, close_at) if close_at is not None else out

        result = _update(self.sid, change, self.transcript)
        if result is None:
            self._publish()
            if not self._failing:
                self._failing = True
                self.log(f"capture gate: session {self.sid} reads {what}; its gap file could not be written "
                         f"(its stretches {held} are kept here and skipped by this process; tried again at each look)")
            return
        self.held, self.close_at = [], None
        self._publish()
        self._failing = False
        self.log(f"capture gate: session {self.sid} reads {what}; its gaps (transcript bytes) are {result}")

    def _publish(self) -> None:
        with _RECORDED_LOCK:
            if self.held:
                _RECORDED[self.sid] = [list(g) for g in self.held]
            else:
                _RECORDED.pop(self.sid, None)

    def settled(self, cursor: int) -> None:
        """After a delivery: the server's cursor is at `cursor`, past every
        closed stretch that ends at or below it, which no process can send
        again from here: dropped from the gap file (under its lock) and from
        `held`, so the file does not grow for the session's whole life."""
        try:
            if any(g[1] is not None and g[1] <= cursor for g in self.held):
                self.held = [g for g in self.held if g[1] is None or g[1] > cursor]
                self._publish()
            on_file = read_gaps_raw(self.sid)
            if on_file and any(g[1] is not None and g[1] <= cursor for g in on_file):
                _update(self.sid, lambda gaps: [g for g in gaps if g[1] is None or g[1] > cursor], self.transcript)
        except Exception:  # noqa: BLE001 -- kept: tried again after the next delivery
            pass

    def recorded(self) -> bool:
        """Is its off stretch recorded: an open stretch in its gap file, or
        held here (seen, not written yet)?"""
        gaps = read_gaps_raw(self.sid)
        return is_open(self.held) or (gaps is not None and is_open(gaps))

    def gaps(self) -> list[list]:
        """Its stretches: the gap file's and the ones held here."""
        return normalized(read_gaps(self.sid) + self.held)
