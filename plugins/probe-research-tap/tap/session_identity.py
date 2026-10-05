"""Bounded producer-native identity checks; filenames alone are not evidence.

Claude resumed/forked logs can contain ancestor sessionIds. The final primary
conversation record identifies the current leg; sidechain/progress IDs do not.
Codex and pi have an explicit session header. Kimi Code's wire has a header
(`metadata`) that names no session: Kimi keeps the session's identity in the
session folder's `state.json`, which it writes itself. Uploader identity is
separate.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

from .session_journal import ReconciliationRequired, _harness, prefix_hash

#: The transcript formats this check knows (a registry row's
#: `transcripts.format`). Anything else is refused, never checked as if it
#: were one of these.
FORMAT_CLAUDE = "claude_jsonl"
FORMAT_CODEX = "codex_rollout"
FORMAT_PI = "pi_jsonl"
FORMAT_KIMI = "kimi_wire"
#: Where each format's identity record lives, and its record type.
_HEADER_TYPE = {FORMAT_CODEX: "session_meta", FORMAT_PI: "session"}
#: Kimi Code: the wire's first record, and the identity file in the session folder.
_KIMI_HEADER_TYPE = "metadata"
_KIMI_STATE_FILE = "state.json"
_KIMI_STATE_MAX_BYTES = 1024 * 1024

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
    try:
        fmt = _harness(source).transcripts["format"]
    except (KeyError, TypeError) as exc:
        raise ReconciliationRequired(f"no identity check for capture source {source!r}") from exc
    if fmt == FORMAT_KIMI:
        return _kimi_identity(path, head_lines, _harness(source), expected)
    if fmt != FORMAT_CLAUDE and fmt not in _HEADER_TYPE:
        raise ReconciliationRequired(f"no identity check for transcript format {fmt!r}")
    records = []
    # Claude's current leg is its LAST primary record; header formats open with theirs.
    for line in head_lines if fmt != FORMAT_CLAUDE else tail_lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                records.append(value)
        except (ValueError, UnicodeDecodeError):
            continue
    native = None
    lineage = []
    source_cwd = None
    if fmt == FORMAT_CLAUDE:
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
        kind = _HEADER_TYPE[fmt]
        nested = fmt == FORMAT_CODEX  # Codex wraps the header fields in `payload`
        headers = [r for r in records if r.get("type") == kind]
        if headers:
            payload = headers[0].get("payload", {}) if nested else headers[0]
            native = payload.get("id")
            source_cwd = payload.get("cwd")
            for key in ("forked_from_id", "parentSession", "parent_session_id"):
                if payload.get(key):
                    lineage.append(str(payload[key]))
            if any(
                (r.get("payload", {}) if nested else r).get("id") != native
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


def _same_uuid(value: object, expected: str) -> bool:
    try:
        return str(UUID(str(value))) == expected
    except (ValueError, TypeError, AttributeError):
        return False


def _kimi_identity(path: Path, head_lines: list[bytes], harness, expected: str) -> dict:
    """A Kimi Code wire: `<session_<uuid>>/agents/main/wire.jsonl`.

    Two producer-written facts must agree with the session being uploaded:
    the session folder's own name and the `id` Kimi wrote into its
    `state.json`. The wire itself must open with Kimi's `metadata` header, so
    a stray file in a session folder is never taken for its transcript.
    """
    try:
        first = json.loads(head_lines[0]) if head_lines else None
    except (ValueError, UnicodeDecodeError):
        first = None
    if not isinstance(first, dict) or first.get("type") != _KIMI_HEADER_TYPE:
        raise ReconciliationRequired("transcript does not open with a Kimi Code header")
    folder = next(
        (
            parent
            for parent in path.parents
            if _same_uuid(harness.canonical_session_id(parent.name), expected)
        ),
        None,
    )
    if folder is None:
        raise ReconciliationRequired(
            "native transcript identity is absent or differs from the filename/session"
        )
    try:
        with (folder / _KIMI_STATE_FILE).open("rb") as handle:
            state = json.loads(handle.read(_KIMI_STATE_MAX_BYTES))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise ReconciliationRequired("Kimi Code session state is unreadable") from exc
    if not isinstance(state, dict) or not _same_uuid(
        harness.canonical_session_id(state.get("id") if isinstance(state.get("id"), str) else ""),
        expected,
    ):
        raise ReconciliationRequired(
            "native transcript identity is absent or differs from the filename/session"
        )
    proof = {
        "native_session_id": expected,
        "identity_method": "producer-record-v1",
        "observed_lineage": [],
        "original_author": "unverified",
    }
    source_cwd = state.get("cwd")
    if isinstance(source_cwd, str) and Path(source_cwd).is_absolute():
        proof["source_cwd"] = source_cwd
    return proof


def compatible_copy(first: Path, second: Path) -> bool:
    """Identical bytes or a proven append extension; size alone proves nothing."""
    end = min(first.stat().st_size, second.stat().st_size)
    return prefix_hash(first, end) == prefix_hash(second, end)
