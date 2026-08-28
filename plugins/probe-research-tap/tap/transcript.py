"""JSONL transcript tail with byte-offset cursor, plus batch chunking.

Tracks bytes (not line counts) so a partial trailing line — written
mid-flush by Claude Code — does not advance the cursor and gets re-read
on the next tick once the writer flushes the newline.

Detects truncation/rotation by comparing the current file size against
the persisted cursor: if the file has shrunk we reset to offset 0.

CANONICAL: src/probe/tap_core/transcript.py — the copy under
plugins/probe-research-tap/tap/ is vendored by `make sync-tap-core` and must
stay byte-identical (tests/test_tap_core_sync.py guards it). Edit the
canonical file, never the plugin copy. The duplication exists because the tap
is a separate distribution living in the agent's plugin cache — the CLI
cannot import it, and the plugin cannot import probe.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any


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


def read_new(path: Path, byte_offset: int, limit: int | None = None) -> TailResult:
    """Read bytes from byte_offset to EOF; return complete lines.

    If the file has shrunk below byte_offset we reset to 0 (truncation).

    `limit` caps how many bytes come back in one call. The live tail leaves it
    unset -- a tick's growth is small and reading to EOF is one syscall. An
    IMPORTER walking historical transcripts cannot: a single session log runs to
    tens of MB, and reading it whole to ship it in 1MB batches would hold the
    entire file in memory to no purpose. A capped read still ends on a line
    boundary, because the trailing partial line is withheld exactly as it is at
    a real EOF, so the caller's cursor arithmetic is unchanged.
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
        buf = f.read() if limit is None else f.read(limit)

    lines, partial = split_lines(buf)
    new_offset = start + (len(buf) - partial)
    return TailResult(lines=lines, new_byte_offset=new_offset, file_size=cur_size, inode=inode)


# Target size for one serialized batch body. The ingest gateway caps request
# bodies at 2MB and the client maps the resulting 413 to POISON — a silent
# drop — so batches are built to half that. Budgeted against RAW line bytes,
# which over-estimates: sanitization only ever removes bytes.
MAX_BATCH_BYTES = 1024 * 1024


def chunk_lines(lines: list[bytes], max_bytes: int) -> list[list[bytes]]:
    """Split lines into groups that will serialise under the gateway's body cap.

    A single line larger than the budget becomes its own group — it cannot be
    split without destroying the event, and shipping one oversized body that
    may be 413'd beats dropping the event outright.

    `max_bytes` is REQUIRED here, no default: the tap resolves its budget at
    call time in reconcile.py so tests patching reconcile.MAX_BATCH_BYTES keep
    their grip, and a default bound here would freeze the constant at import.
    """
    groups: list[list[bytes]] = []
    current: list[bytes] = []
    size = 0
    for line in lines:
        n = len(line)
        if current and size + n > max_bytes:
            groups.append(current)
            current = []
            size = 0
        current.append(line)
        size += n
    if current:
        groups.append(current)
    return groups


def build_batch_body(
    *,
    device_id: str,
    session_id: str,
    batch_seq: int,
    cwd: str,
    base_line_no: int,
    lines: list[bytes],
    sanitize: Callable[[Any], Any],
) -> bytes | None:
    """Construct the JSON body for /ingest/v1/sessions/{claude-code,codex}.

    Identity is injected server-side from the ingest token — no employee fields
    here, and the gateway stamps device_id from the token row rather than
    trusting this one.

    Each line is parsed JSON, then run through `sanitize` to strip API metadata
    and drop agent-internal bookkeeping. Lines whose JSON fails to parse are
    kept as raw strings — a lenient fallback so a malformed line is visible in
    the transcript rather than silently vanishing.

    Returns None if every event was dropped by the sanitizer (e.g. a tick that
    only saw stop_hook_summary + turn_duration). Callers treat None as "nothing
    to ship, but advance the cursor."

    `sanitize` IS A PARAMETER, not a lookup. It used to be chosen inside the
    loop by reading the PROBE_TAP_SOURCE environment variable, which is fine
    for a daemon pinned to one agent for its whole life and wrong for anything
    processing both kinds in one pass — an importer walking a machine's Claude
    Code AND Codex history would translate one of them with the other's
    sanitizer, and the failure is silent because both return plausible dicts.

    A sanitizer may return a LIST instead of a single event — pi's
    bashExecution entries do, translating one JSONL line into a synthetic
    tool_use/tool_result pair, because pi records a `!command` escape's call
    and output on one entry while CC's shape puts the call on the assistant
    and the result on the user. Each item in the list becomes its own event,
    all sharing that line's `line_no`. Two events legitimately sharing a
    line_no is safe: the server keys batches on (session_id, batch_seq) and
    treats line_no as ordering, not identity.
    """
    events = []
    for i, line in enumerate(lines):
        try:
            raw = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            raw = line.decode("utf-8", errors="replace")
        sanitized = sanitize(raw)
        if sanitized is None:
            continue
        for one in (sanitized if isinstance(sanitized, list) else [sanitized]):
            events.append({"line_no": base_line_no + i, "raw": one})
    if not events:
        return None
    body = {
        "device_id": device_id,
        "session_id": session_id,
        "batch_seq": batch_seq,
        "cwd": cwd,
        "events": events,
    }
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def build_finalize_body(*, session_id: str) -> bytes:
    """Construct the body for the gateway's SessionFinalizeRequest.

    Says "this session is over, mine it now". Until one of these arrives the
    engine keeps coalescing the session's batches into a live document and
    never runs the completion path — which is the only thing that produces the
    extracted knowledge units (qa / code_change / decision / file_ref). A
    session nobody finalizes is captured but never mined.

    Deliberately minimal. The gateway validates against a model with exactly
    two fields and rebuilds the forwarded body from them, so anything else here
    (device_id, cwd, a stray `events` array) is dropped server-side; sending it
    would just imply a contract that does not exist.
    """
    body = {"finalize": True, "session_id": session_id}
    return json.dumps(body, separators=(",", ":")).encode("utf-8")


def byte_offset_after(path: Path, start: int, line_count: int) -> int:
    """Byte position just past the Nth newline at or after `start`.

    Re-derives the exact cursor rather than trusting summed line lengths:
    split_lines strips \\r and skips blank lines, so line lengths are not a
    file position.
    """
    seen = 0
    pos = start
    with path.open("rb") as f:
        f.seek(start)
        while seen < line_count:
            chunk = f.readline()
            if not chunk:
                break
            pos += len(chunk)
            if chunk.endswith(b"\n"):
                if chunk.strip():
                    seen += 1
            else:
                pos -= len(chunk)  # partial trailing line: do not consume
                break
    return pos
