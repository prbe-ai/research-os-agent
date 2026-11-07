"""Bounded producer-native identity checks; filenames alone are not evidence.

Claude resumed/forked logs can contain ancestor sessionIds. The final primary
conversation record identifies the current leg; sidechain/progress IDs do not.
A Claude sub-agent's own file holds sidechain records only: its id is made from
its parent's session and its agentId (`get_subagent_session_id`, a UUID v5,
which Claude Code never writes), and its last turn proves that id.
Codex and pi have an explicit session header. Kimi Code's wire has a header
(`metadata`) that names no session: Kimi keeps the session's identity in the
session folder's `state.json`, which it writes itself; a helper agent's wire
(`agents/agent-<n>/`) gets a made id the same way a Claude sub-agent does, and
`state.json` must list that helper. Uploader identity is separate.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID, uuid5

from .journal.errors import ReconciliationRequired
from .pinned import open_transcript
from .journal.harness import _harness
from .journal.integrity import prefix_hash
from .session_kind import SessionKind, read_head, session_kind

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
#: `check_identity`'s first read from a Claude transcript's end; without a
#: primary record in it, the whole SAMPLE_BYTES window is read.
VERDICT_FIRST_BYTES = 64 * 1024


class IdentityMismatch(ReconciliationRequired):
    """The transcript is proven to be another session's: it names a native
    session id that is not this one (or two that disagree), or a Kimi Code wire
    sits in another session's folder. Not merely unprovable yet, as a
    transcript with no record so far is (plain ReconciliationRequired): a
    recorder that claimed the session lets go of it (`tap/main.py`)."""


def validate_identity(path: Path, source: str, session_id: str) -> dict:
    return _identify(path, source, session_id, verdict_only=False)


def check_identity(path: Path, source: str, session_id: str) -> None:
    """`validate_identity`'s verdict without its proof. A Claude transcript is
    read back from its end only as far as its last primary record (usually its
    last line), where the proof parses every record of the last SAMPLE_BYTES
    for the lineage. Session discovery checks every transcript on the machine,
    and only the verdict decides there."""
    _identify(path, source, session_id, verdict_only=True)


def _is_primary(record: dict) -> bool:
    return bool(
        record.get("type") in ("user", "assistant")
        and not record.get("isSidechain")
        and record.get("sessionId")
    )


def _is_subagent_turn(record: dict) -> bool:
    """A Claude sub-agent's turn: a sidechain record naming its parent session and its agent."""
    return bool(
        record.get("type") in ("user", "assistant")
        and record.get("isSidechain") is True
        and isinstance(record.get("sessionId"), str)
        and isinstance(record.get("agentId"), str)
        and record.get("agentId")
    )


def _is_claude_turn(record: dict) -> bool:
    """What a sub-agent's id is proved by: its own turn, or a session's turn after it."""
    return _is_primary(record) or _is_subagent_turn(record)


def get_subagent_session_id(parent_session_id: str, agent_id: str) -> str | None:
    """The session id of a Claude sub-agent's chat (`<parent>/subagents/agent-<agentId>.jsonl`):
    a UUID v5 made from its parent's and its own id, the same on every machine; None when the
    parent id is not a UUID. Claude Code gives a sub-agent no session id of its own."""
    try:
        return str(uuid5(UUID(parent_session_id), agent_id))
    except (ValueError, TypeError, AttributeError):
        return None


def _get_turn_session(record: dict) -> object:
    """The session a Claude turn names: a session's own, or the one made for its sub-agent."""
    if _is_primary(record):
        return record["sessionId"]
    return get_subagent_session_id(record["sessionId"], record["agentId"])


#: What `json.loads` raises on a line it cannot use. RecursionError: a line
#: nested past the interpreter's limit (about 1,000 levels before Python 3.12).
_UNPARSABLE = (ValueError, UnicodeDecodeError, RecursionError)


def _records(lines, marker: bytes | None = None):
    """The JSON objects among `lines`, in order. With `marker`, only lines that
    could hold it: its literal bytes, a \\u escape that could spell it, or a NUL
    (a UTF-16/32 line `json.loads` would still decode)."""
    for line in lines:
        if marker is not None and marker not in line and b"\\u" not in line and b"\x00" not in line:
            continue
        try:
            value = json.loads(line)
        except _UNPARSABLE:
            continue
        if isinstance(value, dict):
            yield value


def _window(handle, size: int, span: int, *, tail: bool) -> list[bytes]:
    """The complete lines of the first (`tail=False`) or last `span` bytes."""
    if not tail:
        handle.seek(0)
        lines = handle.read(span).splitlines()
        return lines[:-1] if size > span else lines
    start = max(0, size - span)
    handle.seek(start)
    lines = handle.read().splitlines()
    return lines[1:] if start > 0 else lines


def _find_last_record(handle, size: int, is_wanted=_is_primary) -> dict | None:
    """The last `is_wanted` record in the last SAMPLE_BYTES, reading as little as
    it can: a record whole inside a shorter span is whole inside the full one, and
    nothing after it is wanted."""
    for span in (VERDICT_FIRST_BYTES, SAMPLE_BYTES):
        lines = _window(handle, size, span, tail=True)
        found = next((r for r in _records(reversed(lines)) if is_wanted(r)), None)
        if found is not None or span >= size:
            return found
    return None



def _spawn_parent(payload: dict) -> str | None:
    """A Codex sub-agent's parent thread, from its header's
    `source.subagent.thread_spawn.parent_thread_id` (codex-cli 0.166), or None."""
    node = payload.get("source")
    for key in ("subagent", "thread_spawn"):
        node = node.get(key) if isinstance(node, dict) else None
    parent = node.get("parent_thread_id") if isinstance(node, dict) else None
    return str(parent) if parent else None

def _identify(path: Path, source: str, session_id: str, *, verdict_only: bool) -> dict:
    try:
        expected = str(UUID(session_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ReconciliationRequired("malformed source session identity") from exc
    with open_transcript(path) as handle:
        size = handle.seek(0, 2)
        try:
            fmt = _harness(source).transcripts["format"]
        except (KeyError, TypeError) as exc:
            raise ReconciliationRequired(f"no identity check for capture source {source!r}") from exc
        # A program or a person: the FIRST lines say (`session_kind`), wherever
        # the identity is read from. The proof only; the verdict reads no more.
        automated = not verdict_only and session_kind(read_head(handle), expected) is SessionKind.AUTOMATED
        if fmt == FORMAT_KIMI:
            head_lines = _window(handle, size, SAMPLE_BYTES, tail=False)
            return _kimi_identity(path, head_lines, _harness(source), expected) | {"automated": automated}
        if fmt != FORMAT_CLAUDE and fmt not in _HEADER_TYPE:
            raise ReconciliationRequired(f"no identity check for transcript format {fmt!r}")
        # Claude's current leg is its LAST primary record, a sub-agent's (a made id: UUID v5,
        # never Claude Code's) its last turn; header formats open with theirs.
        is_made = UUID(expected).version == 5
        if fmt == FORMAT_CLAUDE and verdict_only:
            last = _find_last_record(handle, size, _is_claude_turn if is_made else _is_primary)
            records = [last] if last is not None else []
        elif fmt == FORMAT_CLAUDE:
            records = list(_records(_window(handle, size, SAMPLE_BYTES, tail=True)))
        else:
            head_lines = _window(handle, size, SAMPLE_BYTES, tail=False)
            records = list(_records(head_lines, _HEADER_TYPE[fmt].encode()))
    native = None
    lineage = []
    source_cwd = None
    if fmt == FORMAT_CLAUDE and is_made:
        turns = [r for r in records if _is_claude_turn(r)]
        if turns:
            native = _get_turn_session(turns[-1])
            # The parent session: the link to the chat that started it.
            lineage = sorted({r["sessionId"] for r in turns if _is_subagent_turn(r)})
            source_cwd = next((r["cwd"] for r in reversed(turns) if r.get("cwd")), None)
    elif fmt == FORMAT_CLAUDE:
        primary = [r for r in records if _is_primary(r)]
        if primary:
            native = primary[-1]["sessionId"]
            # Only string ids: a malformed one would make the proof crash where the verdict passes.
            lineage = sorted({
                r["sessionId"] for r in primary
                if isinstance(r["sessionId"], str) and r["sessionId"] != native
            })
            source_cwd = next(
                (r["cwd"] for r in primary if r["sessionId"] == native and r.get("cwd")), None
            )
    else:
        kind = _HEADER_TYPE[fmt]
        nested = fmt == FORMAT_CODEX  # Codex wraps the header fields in `payload`
        headers = [r for r in records if r.get("type") == kind]
        if headers:
            payload = _header_fields(headers[0], nested)
            native = payload.get("id")
            source_cwd = payload.get("cwd")
            lineage = _get_lineage(payload)
            _check_headers(headers, nested)
    try:
        matches = str(UUID(native)) == expected
    except (ValueError, TypeError, AttributeError):
        matches = False
    if not matches:
        raise (ReconciliationRequired if native is None else IdentityMismatch)(
            "native transcript identity is absent or differs from the filename/session"
        )
    proof = {
        "native_session_id": expected,
        "identity_method": "producer-record-v1",
        "observed_lineage": lineage[:32],
        "original_author": "unverified",
        "automated": automated,
    }
    if isinstance(source_cwd, str) and Path(source_cwd).is_absolute():
        proof["source_cwd"] = source_cwd
    return proof


def _header_fields(record: dict, nested: bool) -> dict:
    """A header's fields; one that is not an object proves nothing."""
    fields = record.get("payload") if nested else record
    return fields if isinstance(fields, dict) else {}


#: Header fields naming the session this one came from (a fork's source, a sub-agent's parent).
_LINEAGE_KEYS = ("forked_from_id", "parentSession", "parent_session_id", "parent_thread_id")


def _get_lineage(payload: dict) -> list[str]:
    """The sessions a header says this one came from, each once."""
    lineage: list[str] = []
    for value in (*(payload.get(key) for key in _LINEAGE_KEYS), _spawn_parent(payload)):
        if value and str(value) not in lineage:
            lineage.append(str(value))
    return lineage


def _check_headers(headers: list[dict], is_nested: bool) -> None:
    """Every header names the first one's session, or one the first says it came from: Codex
    (seen 0.147-0.166) writes a sub-agent spawned with its parent's context as its own header, then
    a copy of the parent's rollout, the parent's header first. Any other id is another chat's."""
    first = _header_fields(headers[0], is_nested)
    known = (first.get("id"), *_get_lineage(first))
    if any(_header_fields(r, is_nested).get("id") not in known for r in headers):
        raise IdentityMismatch("conflicting native session headers")


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
    a stray file in a session folder is never taken for its transcript. A
    helper's wire (`agents/agent-<n>/wire.jsonl`, a made id: UUID v5 of the
    session and `agent-<n>`) proves its session the same way, and `state.json`
    must list `agent-<n>` as a helper (`agents`, `type: sub`; Kimi 2.1-2.5).
    """
    try:
        first = json.loads(head_lines[0]) if head_lines else None
    except _UNPARSABLE:
        first = None
    if not isinstance(first, dict) or first.get("type") != _KIMI_HEADER_TYPE:
        raise ReconciliationRequired("transcript does not open with a Kimi Code header")
    helper = _get_kimi_helper(path, harness) if UUID(expected).version == 5 else None
    if helper is not None:
        folder, session = helper
        if get_subagent_session_id(session, path.parent.name) != expected:
            folder = None
    else:
        session = expected
        folder = next(
            (
                parent
                for parent in path.parents
                if _same_uuid(harness.canonical_session_id(parent.name), expected)
            ),
            None,
        )
    if folder is None:
        raise IdentityMismatch(
            "native transcript identity is absent or differs from the filename/session"
        )
    try:
        with (folder / _KIMI_STATE_FILE).open("rb") as handle:
            state = json.loads(handle.read(_KIMI_STATE_MAX_BYTES))
    except (OSError, *_UNPARSABLE) as exc:
        raise ReconciliationRequired("Kimi Code session state is unreadable") from exc
    if not isinstance(state, dict) or not _same_uuid(
        harness.canonical_session_id(state.get("id") if isinstance(state.get("id"), str) else ""),
        session,
    ):
        raise (IdentityMismatch if isinstance(state, dict) and state.get("id") else ReconciliationRequired)(
            "native transcript identity is absent or differs from the filename/session"
        )
    if helper is not None:
        agents = state.get("agents")
        listed = agents.get(path.parent.name) if isinstance(agents, dict) else None
        if not isinstance(listed, dict) or listed.get("type") != "sub":
            raise ReconciliationRequired("Kimi Code's session state does not list this helper")
    proof = {
        "native_session_id": expected,
        "identity_method": "producer-record-v1",
        # A helper's link to the chat that started it.
        "observed_lineage": [session] if helper is not None else [],
        "original_author": "unverified",
    }
    source_cwd = state.get("cwd")
    if isinstance(source_cwd, str) and Path(source_cwd).is_absolute():
        proof["source_cwd"] = source_cwd
    return proof


def _get_kimi_helper(path: Path, harness) -> tuple[Path, str] | None:
    """(session folder, session id) of a helper's wire at `<session>/agents/agent-<n>/wire.jsonl`."""
    folder = path.parent.parent.parent
    if not path.parent.name.startswith("agent-") or path.parent.parent.name != "agents":
        return None
    try:
        return folder, str(UUID(harness.canonical_session_id(folder.name)))
    except ValueError:
        return None


def compatible_copy(first: Path, second: Path) -> bool:
    """Identical bytes or a proven append extension; size alone proves nothing."""
    end = min(first.stat().st_size, second.stat().st_size)
    return prefix_hash(first, end) == prefix_hash(second, end)
