"""A transcript pinned to the file the Probe service accepted: every read opens that file or none.

A fenced (sandboxed) caller's session is recorded outside its fence; the service accepts its
transcript only as a regular file of the machine's session folder (`fenced_transcript`) and names
that file's identity (`dev:ino`). Whoever reads it pins the path to it (`hold_pin`: the recorder, and
the service for its recording's whole life, its first gate read included): every open here is
`O_NOFOLLOW | O_NONBLOCK` and checks the opened file is a regular file with that identity, so a file
swapped for a link, a FIFO (which would block the reader) or another file after the check raises
`TranscriptSwapped`, and the recorder stops (`tap.main`). Each holder releases only its own pin
(`release_pin` with the token `hold_pin` gave): two recorders of one path (a resumed session beside
its finishing recorder) never unpin each other; the newest pin held decides. A path no one pinned
opens as before. Stdlib only, vendored into the tap (`make sync-tap-core`).
"""

from __future__ import annotations

import errno
import os
import re
import stat
import threading
from pathlib import Path
from typing import BinaryIO

try:
    import fcntl
except ImportError:  # not POSIX: no O_NONBLOCK to clear
    fcntl = None

#: `dev:ino`, as the service writes an accepted file's identity.
_IDENTITY = re.compile(r"^(\d{1,20}):(\d{1,20})$")
#: A pinned open never follows a link and never waits for a FIFO's writer (`O_NONBLOCK`, cleared once
#: the descriptor is known to be a regular file).
_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
          | getattr(os, "O_BINARY", 0))

#: path -> {token: identity}, oldest first: each holder's own pin (`hold_pin`).
_pins: dict[str, dict[object, tuple[int, int]]] = {}
_lock = threading.Lock()


class TranscriptSwapped(Exception):  # noqa: N818 -- the event, as the recorder logs it
    """The pinned transcript is no longer the file the service accepted (a link, or another file)."""


def parse_identity(value: object) -> tuple[int, int] | None:
    """`(dev, ino)` from the service's `dev:ino`, else None."""
    found = _IDENTITY.match(value) if isinstance(value, str) else None
    return (int(found.group(1)), int(found.group(2))) if found else None


def get_identity(path: Path | str) -> str:
    """The `dev:ino` of the file at `path`, opened with no link followed (and no wait for a FIFO)."""
    fd = os.open(str(path), _FLAGS)
    try:
        found = os.fstat(fd)
    finally:
        os.close(fd)
    return f"{found.st_dev}:{found.st_ino}"


def hold_pin(path: Path | str, identity: tuple[int, int]) -> object:
    """Pin `path` to `identity` for its holder: the token `release_pin` takes, which releases this
    pin only (another holder's of the same path stays)."""
    token = object()
    with _lock:
        _pins.setdefault(str(path), {})[token] = identity
    return token


def release_pin(path: Path | str, token: object) -> None:
    with _lock:
        held = _pins.get(str(path))
        if held is None:
            return
        held.pop(token, None)
        if not held:
            del _pins[str(path)]


def get_pin(path: Path | str) -> tuple[int, int] | None:
    """The identity `path` is pinned to (the newest pin held), or None."""
    with _lock:
        held = _pins.get(str(path))
        return list(held.values())[-1] if held else None


def open_transcript(path: Path | str) -> BinaryIO:
    """`path` opened for reading: a pinned one with `O_NOFOLLOW | O_NONBLOCK`, as a regular file
    of its identity (else `TranscriptSwapped`), any other as `open(path, "rb")` does."""
    identity = get_pin(path)
    if identity is None:
        return open(path, "rb")  # noqa: SIM115 -- the caller closes it
    try:
        fd = os.open(str(path), _FLAGS)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise TranscriptSwapped(f"{path} is now a symbolic link") from exc
        raise
    try:
        found = os.fstat(fd)
        if not stat.S_ISREG(found.st_mode):
            raise TranscriptSwapped(f"{path} is no longer a regular file")
        if (found.st_dev, found.st_ino) != identity:
            raise TranscriptSwapped(f"{path} is now another file than the one the service accepted")
        _set_blocking(fd)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _set_blocking(fd: int) -> None:
    """Clear `O_NONBLOCK` on a regular file's descriptor (reads as any open file's)."""
    if fcntl is None:
        return
    flags = fcntl.fcntl(fd, fcntl.F_GETFL)
    if flags & os.O_NONBLOCK:
        fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)


def check_pinned(path: Path | str) -> None:
    """Raise `TranscriptSwapped` when a pinned `path` is no longer its file; a missing one is the
    recorder's to wait for, as an unpinned one. Nothing for a path no one pinned."""
    if get_pin(path) is None:
        return
    try:
        open_transcript(path).close()
    except FileNotFoundError:
        pass
