"""Who drives a captured session, a program or a person, read off its transcript's first lines.

Search hides a session only when this says AUTOMATED, so it is a DENY-list of
the markers a harness writes for a headless run: Claude Code's `entrypoint`
`sdk-cli` (`claude -p`) and Codex's `session_meta` (`originator: codex_exec`,
`source: exec`, a sub-agent's `source.subagent`). The Agent SDK's `sdk-py` /
`sdk-ts` read UNKNOWN, as does a head with no marker at all (empty, unparsable,
pi, Kimi Code) or only another session's (a resumed or forked transcript's
history); any other entrypoint (`cli`, `claude-vscode`, `claude-desktop`,
one Claude Code adds next year) reads INTERACTIVE. A bot this misses stays
searchable; a person it mislabelled would vanish.

`probe.daemon.audit.interactive` reads the same markers with the opposite
default (anything it cannot tell runs the team-note audit, as it always has),
so the two are not one function; its T3 Code test is copied here, because this
file is also the tap's copy (`make -C agent sync-tap-core`): stdlib only,
nothing from `probe`.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from enum import Enum
from typing import BinaryIO

#: How much of the transcript's start is read: lines, the longest line read
#: whole and the most read in all. `audit.py`'s bounds: a T3 Code session's tool
#: list came at line 13 behind six hook lines (2026-10-26), and Codex's first
#: line carries its instructions (52 KB on the devbox).
HEAD_LINES = 40
LINE_CAP_BYTES = 4 * 1024 * 1024
HEAD_CAP_BYTES = 8 * 1024 * 1024
#: Claude Code's `entrypoint` when a program started it.
AUTOMATED_ENTRYPOINTS = frozenset({"sdk-cli"})
#: The Agent SDK's: never AUTOMATED, because editors built on it (Zed) report these while a
#: person types; a missed bot costs noise, a hidden person costs lost work.
AGENT_SDK_ENTRYPOINTS = frozenset({"sdk-py", "sdk-ts"})
#: Codex: `codex exec` names itself as the originator, and says `source: exec`
#: even when an app's originator override reached it.
AUTOMATED_ORIGINATORS = frozenset({"codex_exec"})
AUTOMATED_SOURCES = frozenset({"exec"})
#: T3 Code drives Claude Code through the Agent SDK, so its sessions say
#: `sdk-cli` too; Claude Code's own list of T3's MCP tools (a
#: `deferred_tools_delta` line) is what says a person is in T3.
T3_CODE_TOOL_PREFIX = "mcp__t3-code__"


class SessionKind(str, Enum):  # noqa: UP042 -- StrEnum is 3.11; the CLI runs on 3.10
    """StrEnum's behaviour (`str(kind)` is its value), spelled for Python 3.10,
    the CLI's floor, without `probe.foundation.compat.compat`, which the tap's copy cannot import."""

    AUTOMATED = "automated"
    INTERACTIVE = "interactive"
    UNKNOWN = "unknown"

    __str__ = str.__str__
    __format__ = str.__format__


def read_head(handle: BinaryIO) -> list[bytes]:
    """The first lines of an open binary transcript, read from its start."""
    handle.seek(0)
    head: list[bytes] = []
    read = 0
    while len(head) < HEAD_LINES and read < HEAD_CAP_BYTES:
        raw = handle.readline(LINE_CAP_BYTES)
        if not raw:
            break
        read += len(raw)
        head.append(raw)
    return head


def transcript_kind(path) -> SessionKind:
    """`session_kind` of the transcript at `path`; UNKNOWN when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            return session_kind(read_head(handle))
    except OSError:
        return SessionKind.UNKNOWN


def session_kind(head_lines: Iterable[bytes | str], session_id: str | None = None) -> SessionKind:
    """What the head says about who drives the session. Claude Code: AUTOMATED
    only when every `entrypoint` on the session's own lines says `sdk-cli`; a
    line naming another `sessionId` is history a resume or fork carried in and
    says nothing about this session, and T3 Code's tool list anywhere in the
    head makes it a person's. Codex: its `session_meta`."""
    program = False
    for raw in head_lines:
        try:
            obj = json.loads(raw)
        except (ValueError, RecursionError):  # a line nested past the parser's depth
            continue
        if not isinstance(obj, dict):
            continue
        if _lists_t3_code_tools(obj):
            return SessionKind.INTERACTIVE
        entrypoint = obj.get("entrypoint")
        if isinstance(entrypoint, str) and entrypoint.strip():
            if not _own_line(obj, session_id):
                continue
            value = entrypoint.strip().lower()
            if value in AGENT_SDK_ENTRYPOINTS:
                return SessionKind.UNKNOWN
            if value not in AUTOMATED_ENTRYPOINTS:
                return SessionKind.INTERACTIVE
            program = True
            continue
        payload = obj.get("payload") if obj.get("type") == "session_meta" else None
        if isinstance(payload, dict) and not program:
            return _codex_kind(payload)
    return SessionKind.AUTOMATED if program else SessionKind.UNKNOWN


def _own_line(obj: dict, session_id: str | None) -> bool:
    """Did the session itself write this line? Without an id to check, every line counts."""
    owner = obj.get("sessionId")
    if session_id is None or not isinstance(owner, str):
        return True
    return owner.strip().lower() == session_id.strip().lower()


def _codex_kind(payload: dict) -> SessionKind:
    source, originator = payload.get("source"), payload.get("originator")
    if isinstance(source, dict) and source.get("subagent"):
        return SessionKind.AUTOMATED
    if isinstance(source, str) and source.strip().lower() in AUTOMATED_SOURCES:
        return SessionKind.AUTOMATED
    if isinstance(originator, str) and originator.strip().lower() in AUTOMATED_ORIGINATORS:
        return SessionKind.AUTOMATED
    return SessionKind.INTERACTIVE if source or originator else SessionKind.UNKNOWN


def _lists_t3_code_tools(obj: dict) -> bool:
    """Does this Claude Code line list T3 Code's `t3-code` MCP tools? Only the
    list Claude Code writes itself counts: a prompt naming the tools is not T3.
    (`audit._lists_t3_code_tools`, copied.)"""
    attachment = obj.get("attachment") if obj.get("type") == "attachment" else None
    if not isinstance(attachment, dict) or attachment.get("type") != "deferred_tools_delta":
        return False
    names = attachment.get("addedNames")
    return isinstance(names, list) and any(
        isinstance(name, str) and name.startswith(T3_CODE_TOOL_PREFIX) for name in names
    )
