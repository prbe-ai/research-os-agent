"""Durable-file primitives shared by the SDK spool and the stack connectors.

Every "capture bytes locally, upload out-of-band" path in this SDK needs the same
four low-level moves: an atomic file replacement that survives a crash, a directory
fsync, an advisory cross-process lock, and one UTC timestamp spelling. They were
copy-pasted into spool, capture, harbor, harbor_export, and miles. This is the one
home.

Deliberately stdlib-only and dependency-free: importing it must never pull in
``httpx`` (see the lazy package init in ``probe/__init__.py``), so a distributed
Miles actor can write metric batches to disk without importing the network stack.

The atomic writer is TEXT-based on purpose. Callers serialize their own way -- a
pretty ``indent=2`` ledger, a compact metric record, a JSONL spool page -- and hand
the finished text here. That keeps the serialization divergence in the callers where
it belongs and leaves this primitive with exactly one meaningful knob: the file
``mode`` (e.g. ``0o600`` for a queue on a shared PVC that carries scrubbed config).
"""

from __future__ import annotations

import ctypes
import fcntl
import json
import os
import shutil
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def now_iso() -> str:
    """The single UTC, ISO 8601 timestamp spelling used across durable records."""
    return datetime.now(timezone.utc).isoformat()


def fsync_directory(path: str | Path) -> None:
    """fsync a directory entry so a create/rename survives a crash.

    OSError is swallowed: some network filesystems reject a directory fsync, and a
    file fsync plus an atomic replace is still the strongest guarantee they expose.
    """
    try:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


def write_text_atomic(path: str | Path, text: str, *, mode: int | None = None) -> None:
    """Atomically replace ``path`` with ``text``.

    Write a temp sibling, fsync it, ``os.replace`` it onto ``path`` (atomic on POSIX),
    then fsync the parent directory. A crash leaves either the old file or the new one,
    never a torn write. ``mode`` (e.g. ``0o600``) is applied to the new file when given;
    ``None`` leaves it to the umask, matching a plain ``open("x")``. The parent
    directory must already exist.
    """
    path = Path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    open_mode = 0o666 if mode is None else mode
    try:
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, open_mode
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def file_lock(path: str | Path) -> Iterator[None]:
    """Serialize a narrow cross-process critical section via an advisory ``flock``.

    The lock is a sidecar file; its parent is created if missing (at the umask
    default, NOT 0o700 -- a caller that needs a private directory must create it
    itself first). NEVER hold it across network I/O -- ``flock`` is released only when
    the holder exits, so a hung holder blocks every other writer indefinitely.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def read_json(path: str | Path, *, error: type[Exception] = ValueError) -> dict[str, Any]:
    """Read and parse a JSON object from ``path``.

    Raises ``error`` -- the caller's own exception type, e.g. ``HarborExportError`` --
    on missing/unreadable/invalid/non-object content, so folding several readers into
    one never silently changes a caller's ``except`` surface.
    """
    path = Path(path)
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise error(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise error(f"{path} must contain a JSON object")
    return value


def try_clone(src: str, dst: str) -> bool:
    """Filesystem-level clone of ``src`` at ``dst``. True when the platform and
    filesystem support one (APFS clonefile, Linux FICLONE reflink); False means
    the caller must fall back to a byte copy. ``dst`` must not exist."""
    if sys.platform == "darwin":
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.clonefile(os.fsencode(src), os.fsencode(dst), 0) == 0:
                return True
        except (OSError, AttributeError):
            pass
        return False
    if sys.platform.startswith("linux"):
        _FICLONE = 0x40049409
        try:
            with open(src, "rb") as source, open(dst, "wb") as target:
                fcntl.ioctl(target.fileno(), _FICLONE, source.fileno())
            return True
        except OSError:
            Path(dst).unlink(missing_ok=True)
            return False
    return False


def snapshot_file(src: str | Path, dst: str | Path, *, mode: int = 0o600) -> None:
    """Immutable point-in-time snapshot of ``src`` at ``dst``.

    The mechanism is an internal detail: a copy-on-write filesystem clone where
    the filesystem offers one (instant, no extra space until the original
    changes), else a streamed byte copy. Either way ``dst`` appears atomically
    (temp sibling + ``os.replace``) and never shares mutable state with ``src``
    through the original path -- overwriting or deleting the original later
    cannot alter the snapshot. Named for what it is FOR, not how it works.
    """
    src = Path(src)
    dst = Path(dst)
    temporary = dst.with_name(f".{dst.name}.{uuid.uuid4().hex}.tmp")
    try:
        if not try_clone(str(src), str(temporary)):
            shutil.copyfile(src, temporary)
        os.chmod(temporary, mode)
        # A clone is a metadata operation and a copy already streamed the bytes;
        # fsync the result so the rename below lands on a durable file.
        descriptor = os.open(temporary, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(temporary, dst)
        fsync_directory(dst.parent)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = [
    "now_iso",
    "fsync_directory",
    "write_text_atomic",
    "file_lock",
    "read_json",
    "snapshot_file",
    "try_clone",
]
