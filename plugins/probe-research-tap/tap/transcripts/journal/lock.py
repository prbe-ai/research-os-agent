"""the FileLock interface (non-blocking whole-file lock per handle, durable rename) and the per-OS pick."""

from __future__ import annotations
try:
    import fcntl
except ImportError:  # Windows: `_lock_now`
    fcntl = None  # type: ignore[assignment]
import os
from contextlib import suppress
from pathlib import Path


def state_root() -> Path:
    return Path(
        os.environ.get("PROBE_TRANSCRIPT_STATE_DIR", "~/.probe/transcripts-v2")
    ).expanduser()


#: Windows locks one byte this far in (byte-range locks are mandatory there):
#: the same offset as `probe._shared.oscompat.LOCK_OFFSET`.
_WIN_LOCK_OFFSET = 0x7FFFFFFF


def _sync_directory(directory) -> None:
    """Make a rename in `directory` durable: fsync the folder. POSIX only:
    Windows cannot open a folder as a file (NTFS journals the rename itself)."""
    if os.name == "nt":
        return
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _lock_now(handle) -> bool:
    """An exclusive, non-blocking whole-file lock on an open file, per handle
    (a second open in this process conflicts): `flock`, or one byte with
    `msvcrt.locking` on Windows. False when another holds it."""
    if fcntl is None:
        import msvcrt

        try:
            handle.seek(_WIN_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(handle) -> None:
    """Let go of `_lock_now`'s lock before the handle closes. Closing alone
    releases it too, but Windows may release a closed handle's byte lock
    late, and another process's `_lock_now` must find it free at once."""
    with suppress(OSError):
        if fcntl is None:
            import msvcrt

            handle.seek(_WIN_LOCK_OFFSET)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle, fcntl.LOCK_UN)
