"""JSONL transcript tail with byte-offset cursor.

Tracks bytes (not line counts) so a partial trailing line — written
mid-flush by Claude Code — does not advance the cursor and gets re-read
on the next tick once the writer flushes the newline.

Detects truncation/rotation by comparing the current file size against
the persisted cursor: if the file has shrunk we reset to offset 0.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class TailResult:
    lines: list[bytes]
    new_byte_offset: int
    file_size: int
    inode: int


def split_lines(buf: bytes) -> tuple[list[bytes], int]:
    """Split buf into newline-terminated lines.

    Returns (lines, partial_byte_count). Trailing partial bytes are NOT
    included in `lines` and the caller must subtract `partial_byte_count`
    from the new cursor position so they're re-read next tick.
    Blank lines are skipped (matching the Go SplitLines).
    """
    out: list[bytes] = []
    start = 0
    for i, b in enumerate(buf):
        if b == 0x0A:  # '\n'
            line = buf[start:i].rstrip(b"\r")
            if line:
                out.append(bytes(line))
            start = i + 1
    return out, len(buf) - start


def validate_json(line: bytes) -> bool:
    try:
        json.loads(line)
        return True
    except (ValueError, UnicodeDecodeError):
        return False


def read_new(path: Path, byte_offset: int) -> TailResult:
    """Read bytes from byte_offset to EOF; return complete lines.

    If the file has shrunk below byte_offset we reset to 0 (truncation).
    """
    st = path.stat()
    cur_size = st.st_size
    inode = st.st_ino

    start = byte_offset
    if cur_size < byte_offset:
        start = 0

    if cur_size <= start:
        return TailResult(lines=[], new_byte_offset=start, file_size=cur_size, inode=inode)

    with path.open("rb") as f:
        f.seek(start)
        buf = f.read()

    lines, partial = split_lines(buf)
    new_offset = start + (len(buf) - partial)
    return TailResult(lines=lines, new_byte_offset=new_offset, file_size=cur_size, inode=inode)
