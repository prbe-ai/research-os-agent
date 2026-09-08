"""Bounded producer-native identity checks; filenames alone are not evidence.

Claude resumed/forked logs can contain ancestor sessionIds. The final primary
conversation record identifies the current leg; sidechain/progress IDs do not.
Codex and pi have an explicit session header. Uploader identity is separate.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from .session_journal import ReconciliationRequired, prefix_hash

SAMPLE_BYTES = 2 * 1024 * 1024


def validate_identity(path: Path, source: str, session_id: str) -> dict:
    try:
        expected = str(UUID(session_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReconciliationRequired("malformed source session identity") from exc
    with path.open("rb") as handle:
        head = handle.read(SAMPLE_BYTES)
        size = handle.seek(0, 2)
        handle.seek(max(0, size - SAMPLE_BYTES))
        tail = handle.read()
    head_lines = head.splitlines()
    tail_lines = tail.splitlines()
    if size > SAMPLE_BYTES:
        head_lines = head_lines[:-1]
        tail_lines = tail_lines[1:]
    records = []
    for line in head_lines if source != "claude_code" else tail_lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
        except (ValueError, UnicodeDecodeError):
            continue
    native = None
    lineage = []
    source_cwd = None
    if source == "claude_code":
        primary = [
            r
            for r in records
            if r.get("type") in ("user", "assistant")
            and not r.get("isSidechain")
            and r.get("sessionId")
        ]
        if primary:
            native = primary[-1]["sessionId"]
            lineage = sorted({r["sessionId"] for r in primary if r["sessionId"] != native})
            source_cwd = next(
                (r["cwd"] for r in primary if r["sessionId"] == native and r.get("cwd")), None
            )
    else:
        kind = "session_meta" if source == "codex" else "session"
        headers = [r for r in records if r.get("type") == kind]
        if headers:
            payload = headers[0].get("payload", {}) if source == "codex" else headers[0]
            native = payload.get("id")
            source_cwd = payload.get("cwd")
            for key in ("forked_from_id", "parentSession", "parent_session_id"):
                if payload.get(key):
                    lineage.append(str(payload[key]))
            if any(
                (r.get("payload", {}) if source == "codex" else r).get("id") != native
                for r in headers
            ):
                raise ReconciliationRequired("conflicting native session headers")
    try:
        matches = str(UUID(native)) == expected
    except (ValueError, TypeError, AttributeError):
        matches = False
    if not matches:
        raise ReconciliationRequired(
            "native transcript identity is absent or differs from the filename/session"
        )
    proof = {
        "native_session_id": expected,
        "identity_method": "producer-record-v1",
        "observed_lineage": lineage[:32],
        "original_author": "unverified",
    }
    if isinstance(source_cwd, str) and Path(source_cwd).is_absolute():
        proof["source_cwd"] = source_cwd
    return proof


def compatible_copy(first: Path, second: Path) -> bool:
    """Identical bytes or a proven append extension; size alone proves nothing."""
    end = min(first.stat().st_size, second.stat().st_size)
    return prefix_hash(first, end) == prefix_hash(second, end)
