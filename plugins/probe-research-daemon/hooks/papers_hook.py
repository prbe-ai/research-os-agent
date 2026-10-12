#!/usr/bin/env python3
"""PreToolUse on `WebSearch`: hold a paper-type search until the agent has
tried Probe's paper search (`probe paper find`).

Where the Probe daemon records (the lean plugin, `PROBE_PLUGIN_PROFILE=daemon`)
the agent's only Probe read used to be `probe ask`, which answers minutes later,
so a question about papers went to the web out of habit. `probe paper find`
answers in seconds; this hook makes the agent try it first.

ONE HOLD, BOUNDED TWICE. A search is held (`PAPER_SEARCH_HOLD`, which names the
command) only when nothing in the last `PAPER_FIND_WINDOW_S` relaxed it:
- `probe paper find` ran in this session (it touches `paper_find_stamp_path`
  before its network call, so a failed search counts as trying), or
- this hook already held one (`paper_hold_stamp_path`).
The hold stamp is written BEFORE the refusal and a failed write passes the
search, so no state the hook cannot record can turn into a hold loop.

Never held: the agent profile (it has the Probe MCP tools); a session the
researcher turned off (the guard would refuse `probe paper find` too); a session
id that is not a safe file name; a query with none of `PAPER_QUERY`'s words; a
CLI without the command, or one that could not be asked
(`version_check.paper_find_ready`: `probe paper find --help`, ~0.9 s; a definite
answer is kept for the session, an unknown one is asked again). Fetching a
paper's page is never held: the paper search returns passages, never a whole
paper. Several web searches in one turn can all be held together: one batch is
one nudge, and the window bounds holds across turns, where a loop could form.

Codex runs its web search on the server and pi has no PreToolUse, so neither
reaches this hook; the managed block's PAPERS line is their only nudge.

stdlib only (system python3, no probe package): the shared constants come
from `_session_marker`, the sibling copy of `probe.sdk.session_marker`.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

import _session_marker

#: The tool this hook answers. Claude Code and Kimi both name it so.
WEB_SEARCH = "WebSearch"

#: Words that make a web search a paper search. Whole words, any case. Not
#: `acl` or `survey`: "S3 bucket ACL" and "survey table schema" are infra.
PAPER_QUERY = re.compile(
    r"\b(?:arxiv|preprints?|papers?|openreview|et al|literature|"
    r"neurips|icml|iclr|emnlp|cvpr)\b",
    re.IGNORECASE,
)


def _fresh(path, now: float) -> bool:
    """Was `path` touched within the window? Missing or unreadable is no."""
    try:
        return now - path.stat().st_mtime < _session_marker.PAPER_FIND_WINDOW_S
    except OSError:
        return False


def _state(session_id: str, cwd: "str | None") -> str:
    """The session's state, as the guard reads it (`tracking_guard._state`)."""
    explicit = _session_marker.session_state(session_id)
    if explicit is not None:
        return explicit
    state, _source = _session_marker.resolve_state_default(cwd)
    return state


def _cli_ready(session_id: str) -> bool:
    """`version_check.paper_find_ready`, cached for the session."""
    cache = _session_marker.paper_cli_cache_path(session_id)
    try:
        return cache.read_text(encoding="utf-8").strip() == "1"
    except OSError:
        pass
    import version_check

    ready = version_check.paper_find_ready()
    if ready is None:
        return False
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text("1" if ready else "0", encoding="utf-8")
    except OSError:
        pass
    return ready


def hold_reason(payload: dict, now: float) -> "str | None":
    """The refusal for this tool call, or None to let it run."""
    if os.environ.get("PROBE_PLUGIN_PROFILE") != "daemon":
        return None
    if payload.get("tool_name") != WEB_SEARCH:
        return None
    session_id = payload.get("session_id")
    if not _session_marker.valid_session_id(session_id):
        return None
    tool_input = payload.get("tool_input")
    query = tool_input.get("query") if isinstance(tool_input, dict) else None
    if not isinstance(query, str) or not PAPER_QUERY.search(query):
        return None
    cwd = payload.get("cwd")
    if _state(session_id, cwd if isinstance(cwd, str) and cwd else None) == _session_marker.STATE_OFF:
        return None
    if _fresh(_session_marker.paper_find_stamp_path(session_id), now):
        return None
    held = _session_marker.paper_hold_stamp_path(session_id)
    if _fresh(held, now):
        return None
    if not _cli_ready(session_id):
        return None
    try:
        held.parent.mkdir(parents=True, exist_ok=True)
        held.touch()
    except OSError:
        return None
    return _session_marker.PAPER_SEARCH_HOLD


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    try:
        reason = hold_reason(payload, time.time())
    except Exception:
        return
    if reason:
        sys.stdout.write(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                }
            )
        )


if __name__ == "__main__":
    main()
