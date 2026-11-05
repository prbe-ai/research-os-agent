"""A transcript pinned to the file the Probe service accepted: every read opens that file or none.

A fenced (sandboxed) caller's session is recorded outside its fence; the service accepts its
transcript only as a regular file of the machine's session folder (`fenced_transcript`) and names
that file's identity (`dev:ino`). The recorder pins the path to it (`set_pin`): every open here is
`O_NOFOLLOW` and checks the opened file's identity, so a file swapped for a link or for another file
after the check raises `TranscriptSwapped`, and the recorder stops (`tap.main`). A path no one pinned
opens as before. Stdlib only, vendored into the tap (`make sync-tap-core`).
"""

from __future__ import annotations

import errno
import os
import re
import threading
from pathlib import Path
from typing import BinaryIO

#: `dev:ino`, as the service writes an accepted file's identity.
_IDENTITY = re.compile(r"^(\d{1,20}):(\d{1,20})$")
_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)

_pins: dict[str, tuple[int, int]] = {}
_lock = threading.Lock()


class TranscriptSwapped(Exception):  # noqa: N818 -- the event, as the recorder logs it
    """The pinned transcript is no longer the file the service accepted (a link, or another file)."""


def parse_identity(value: object) -> tuple[int, int] | None:
    """`(dev, ino)` from the service's `dev:ino`, else None."""
    found = _IDENTITY.match(value) if isinstance(value, str) else None
    return (int(found.group(1)), int(found.group(2))) if found else None


def get_identity(path: Path | str) -> str:
    """The `dev:ino` of the regular file at `path`, opened with no link followed."""
    fd = os.open(str(path), _FLAGS)
    try:
        found = os.fstat(fd)
    finally:
        os.close(fd)
    return f"{found.st_dev}:{found.st_ino}"


def set_pin(path: Path | str, identity: tuple[int, int]) -> None:
    with _lock:
        _pins[str(path)] = identity


def clear_pin(path: Path | str) -> None:
    with _lock:
        _pins.pop(str(path), None)


def open_transcript(path: Path | str) -> BinaryIO:
    """`path` opened for reading: a pinned one with `O_NOFOLLOW` and its identity checked (raises
    `TranscriptSwapped`), any other as `open(path, "rb")` does."""
    with _lock:
        identity = _pins.get(str(path))
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
        if (found.st_dev, found.st_ino) != identity:
            raise TranscriptSwapped(f"{path} is now another file than the one the service accepted")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def check_pinned(path: Path | str) -> None:
    """Raise `TranscriptSwapped` when a pinned `path` is no longer its file; a missing one is the
    recorder's to wait for, as an unpinned one. Nothing for a path no one pinned."""
    with _lock:
        if str(path) not in _pins:
            return
    try:
        open_transcript(path).close()
    except FileNotFoundError:
        pass
