#!/usr/bin/env python3
"""Warn -- never gate -- when a probe WRITE lands in a session the researcher untracked.

PostToolUse on Bash. When `probe session untrack` has marked this session off
and a Bash command still wrote research content through the probe CLI, this
hook hands the model one line of additionalContext restating the contract it
is violating. That is the whole mechanism: the write has already happened, the
hook never denies anything, and exit is 0 on every path.

WHY WARN AND NOT DENY. The off marker is a fact a hook can read, but "stop
recording" is a contract that spans surfaces no hook can reach -- the SDK
inside a training script, the hosted MCP, a job on another machine. A deny
here would cover one path of several and teach the agent the gate is
advisory, and it would also block legitimate invocations this heuristic
cannot distinguish (debugging Probe itself is this team's day job). So the
deterministic layer DETECTS, and the behavioural layer -- the model reading
the contract -- RESOLVES. This mirrors the lab's standing rule for capture
completeness: warn at the moment of violation, never gate the work.

WHY THE PARSE IS A HEURISTIC, AND WHICH WAY IT LEANS. The command string is
shell, and this is not a shell parser: segments are split on `&&`/`||`/`;`/
`|`, env-var prefixes are skipped, and the first probe invocation whose
subcommand records research content is reported. False negatives are
acceptable -- a missed warning costs one unwarned write in a state the
researcher will notice on the dashboard anyway. False positives are NOT: a
warning on `probe run show` teaches the model this layer cries wolf, which is
how warning layers die. Every ambiguity below resolves toward silence.

STDLIB ONLY, PYTHON 3.9, FAIL-SOFT -- same vendoring contract as the sibling
hooks: this runs under the system python3 with no probe package importable,
and nothing here may block or break a session.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

# Sibling import, resolved because sys.path[0] is this script's directory when
# hooks.json runs `python3 <plugin_root>/hooks/tracking_guard.py`.
import _session_marker

# Top-level probe commands that record research content whatever follows them.
# `flush` is deliberately absent: draining an outbox delivers writes recorded
# BEFORE the researcher flipped the switch, and honouring the pre-off record
# is what "off deletes nothing" means.
TOP_LEVEL_WRITES = frozenset(
    {"log", "link", "snapshot", "exec", "backfill", "import", "wandb"}
)

# Command groups whose verbs write research content unless the verb is a read.
# `session` is deliberately absent (it is the switch itself -- warning on
# `probe session track` would block the un-mute), and so are the read groups
# (metrics, series, get, bundle, events, coordinates, statusline, ...) and the
# machine-plumbing groups (context, token, mcp, workspace, shared, outbox).
WRITE_GROUPS = frozenset(
    {
        "project",
        "experiment",
        "run",
        "artifact",
        "notes",
        "wiki",
        "span",
        "group",
        "edge",
        "trial",
        "views",
    }
)

# Verbs inside a write group that only read. Unknown verbs count as writes --
# the group already said "research content" -- EXCEPT that the leaning above
# still applies: anything added to probe later that reads should be added here.
READ_VERBS = frozenset(
    {
        "show",
        "list",
        "get",
        "status",
        "versions",
        "cat",
        "diff",
        "search",
        "export",
        "events",
        "coordinates",
        "download",
        "help",
    }
)

MESSAGE = (
    "Research tracking is OFF for this conversation -- the researcher declared "
    "it via /research-tracking (probe session untrack) -- but `{matched}` just "
    "wrote to Probe. Honor the declaration: record nothing further. If the "
    "researcher explicitly asked for this write, ask whether tracking should "
    "resume (`/research-tracking on`); otherwise consider undoing it, and "
    "continue the actual work without recording."
)

_ENV_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SEGMENT_SPLIT = re.compile(r"&&|\|\||;|\|")


def probe_write(command: str) -> "str | None":
    """The probe invocation that records research content, or None.

    Returns the matched `probe <group> <verb>` (or `probe <command>`) so the
    warning can name what it saw rather than gesturing at the whole command
    line.
    """
    for segment in _SEGMENT_SPLIT.split(command):
        try:
            tokens = shlex.split(segment, posix=True)
        except ValueError:
            continue  # unbalanced quotes: not parseable, lean silent
        index = 0
        while index < len(tokens) and _ENV_ASSIGNMENT.match(tokens[index]):
            index += 1
        if index >= len(tokens):
            continue
        if os.path.basename(tokens[index]) != "probe":
            continue
        args = [t for t in tokens[index + 1 :] if not t.startswith("-")]
        if not args:
            continue
        head = args[0]
        if head in TOP_LEVEL_WRITES:
            return "probe " + head
        if head in WRITE_GROUPS:
            verb = args[1] if len(args) > 1 else ""
            if verb and verb not in READ_VERBS:
                return "probe " + head + " " + verb
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict) or payload.get("tool_name") != "Bash":
        return
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    # Signal first: tracking on (or undecided) is the overwhelmingly common
    # case, and it must cost one read, not a parse. The EXPLICIT "off" only --
    # a session that merely recorded nothing has made no declaration to honour.
    if _session_marker.tracking_signal(session_id) != "off":
        return
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return
    matched = probe_write(command)
    if not matched:
        return
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": MESSAGE.format(matched=matched),
                }
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a broken warning layer must never become a broken session
    sys.exit(0)
