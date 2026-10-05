"""Find a Kimi Code session's main wire from its session id.

Kimi Code's hook payloads carry a `session_id` (`session_<uuid>`) and no
transcript path. Kimi records every session it creates in
`$KIMI_CODE_HOME/session_index.jsonl`, one JSON line per session
(`{"sessionId", "sessionDir", "workDir"}`), appended when the session is
created, so the newest sessions are at the END: the file is read backwards,
a bounded amount, and the last line naming the session wins. The wire is then
`<sessionDir>/agents/main/wire.jsonl` (the part of the registry row's
`transcripts.pattern` below the session folder).

When the index has no line for the session (deleted, rotated, a Kimi that
stops writing it), the row's pattern is globbed under the transcript root for
that one session folder. Either way the answer must match the pattern, so a
subagent's `agents/agent-N/wire.jsonl` is never returned as the session.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path, PurePath

#: Kimi Code's session index, in its home folder.
INDEX_FILE = "session_index.jsonl"
#: How much of the index is read, from the end (about 200 bytes a session).
MAX_INDEX_BYTES = 8 * 1024 * 1024
_BLOCK = 64 * 1024


def _pattern_parts(harness) -> tuple[list[str], int] | None:
    """The row's pattern split at the session folder: (parts, its index)."""
    prefix = harness.session_id_prefix or ""
    parts = harness.transcript_pattern().split("/")
    for index, part in enumerate(parts):
        if prefix and part.startswith(prefix):
            return parts, index
    return None


def _indexed_session_dir(index: Path, native_id: str) -> Path | None:
    """The `sessionDir` of the LAST index line for `native_id`, or None."""
    needle = native_id.encode()
    try:
        handle = index.open("rb")
    except OSError:
        return None
    with handle:
        end = handle.seek(0, 2)
        floor = max(0, end - MAX_INDEX_BYTES)
        carry = b""
        position = end
        while position > floor:
            start = max(floor, position - _BLOCK)
            handle.seek(start)
            block = handle.read(position - start) + carry
            position = start
            lines = block.split(b"\n")
            # The first piece may be the tail of a line that starts earlier:
            # it is completed by the next (earlier) block.
            carry = lines.pop(0) if position > floor else b""
            for line in reversed(lines):
                found = _session_dir_of(line, needle, native_id)
                if found is not None:
                    return found
    return None


def _session_dir_of(line: bytes, needle: bytes, native_id: str) -> Path | None:
    if needle not in line:
        return None
    try:
        record = json.loads(line)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(record, dict) or record.get("sessionId") != native_id:
        return None
    session_dir = record.get("sessionDir")
    if not isinstance(session_dir, str) or not session_dir.strip():
        return None
    path = Path(session_dir)
    return path if path.is_absolute() else None


def find_transcript(harness, session_id: str, env: Mapping[str, str] | None = None) -> Path | None:
    """The main wire for one session (canonical or native id), or None."""
    split = _pattern_parts(harness)
    if split is None:
        return None
    parts, at = split
    pattern = "/".join(parts)
    native = harness.native_session_id(harness.canonical_session_id(session_id))
    home = harness.home_dir(env)
    if home is not None:
        session_dir = _indexed_session_dir(home / INDEX_FILE, native)
        if session_dir is not None and session_dir.name == native:
            candidate = session_dir.joinpath(*parts[at + 1 :])
            if PurePath(candidate).match(pattern):
                # Named by Kimi's own index; returned even before the file is
                # there, since the daemon waits for it.
                return candidate
    root = harness.transcript_root(env)
    if root is None or not root.is_dir():
        return None
    one_session = "/".join([*parts[:at], native, *parts[at + 1 :]])
    for match in sorted(root.glob(one_session)):
        if match.is_file() and PurePath(match).match(pattern):
            return match
    return None
