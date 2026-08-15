#!/usr/bin/env python3
"""The deterministic side of the tracking toggle: flip on skill activation,
warn -- never gate -- on a probe write the flip should have prevented.

PostToolUse on Skill/SlashCommand and Bash, two jobs on one file of state:

FLIP. When the toggle-research-tracking skill is invoked, THIS HOOK writes
the session's tracking signal -- the same write `probe session untrack`
makes. Bare invocation is a TOGGLE: it flips to the opposite of the current
state, where "current" is the explicit signal if one exists and otherwise
the machine's default posture (`is_tracking` resolves that, exactly as the
statusline does -- the two surfaces must not disagree about what "current"
means). An explicit `off`/`on` (or the skill's synonyms) sets that state,
idempotently. `status`, or an argument that reads as neither, writes
nothing. The skill still has the model read the result back with `probe
session status` and narrate it, but the flip no longer DEPENDS on the model
obeying prose: a skill activation the model then fumbles would otherwise
leave the flag unflipped while every reader of it -- the statusline, the
compact-contract injection, the warn below -- confidently reported the
wrong state. The declaration is the researcher's; recording it is not a job
to delegate to compliance.

WARN. When `probe session untrack` has marked this session off and a Bash
command still wrote research content through the probe CLI, this hook hands
the model one line of additionalContext restating the contract it is
violating. That is the whole mechanism: the write has already happened, the
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
    "it via /toggle-research-tracking (probe session untrack) -- but `{matched}` "
    "just wrote to Probe. Honor the declaration: record nothing further. If the "
    "researcher explicitly asked for this write, ask whether tracking should "
    "resume (`/toggle-research-tracking on`); otherwise consider undoing it, and "
    "continue the actual work without recording."
)

# The toggle skill, by trailing slug -- both spellings, because a machine mid-
# upgrade can carry a transcript that invoked the old name while this newer
# hook file handles the event. Matching a name that no longer resolves costs
# nothing; missing one that did costs the flip.
TOGGLE_SKILL_SLUGS = frozenset({"toggle-research-tracking", "research-tracking"})

# Direction words, mirroring the skill's own reading of its argument ("off",
# "stop", "disable" / "on", "start", "resume"). A BARE invocation (or a
# literal "toggle") flips to the opposite of the current state -- that is
# what a toggle is. `status` and unrecognised prose write nothing: asking a
# question must never flip the switch.
OFF_WORDS = frozenset({"off", "stop", "disable", "end"})
ON_WORDS = frozenset({"on", "start", "resume"})
TOGGLE_WORDS = frozenset({"toggle", "flip"})
STATUS_WORDS = frozenset({"status"})

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


def toggle_direction(tool_name: str, tool_input: object) -> "str | None":
    """`"on"`, `"off"`, `"toggle"`, or None (do not touch the signal).

    Bare invocation IS the toggle -- the researcher typed the command and the
    command's name says what it does. Only two things read as "not a
    direction": `status` (a question, and questions must not flip switches)
    and prose the word lists do not recognise, where guessing would flip on a
    sentence like "what is going on?".

    Field names mirror telemetry.match_skill: a Skill call carries the slug in
    `skill` with the argument in `args`; a SlashCommand carries one `command`
    string with the argument inline. Namespacing (`probe-research:`) and a
    leading slash are stripped the same way there too.
    """
    if tool_name not in ("Skill", "SlashCommand") or not isinstance(tool_input, dict):
        return None
    inline_args = ""
    matched = False
    for field in ("skill", "command", "skill_name", "name"):
        value = tool_input.get(field)
        if not (isinstance(value, str) and value.strip()):
            continue
        parts = value.strip().lstrip("/").split(None, 1)
        if parts[0].split(":")[-1] in TOGGLE_SKILL_SLUGS:
            matched = True
            if len(parts) > 1:
                inline_args = parts[1]
            break
    if not matched:
        return None
    separate = tool_input.get("args")
    if isinstance(separate, str) and separate.strip():
        inline_args = (inline_args + " " + separate).strip()
    words = inline_args.lower().split()
    if not words:
        return "toggle"
    if words[0] in OFF_WORDS:
        return "off"
    if words[0] in ON_WORDS:
        return "on"
    if words[0] in TOGGLE_WORDS:
        return "toggle"
    return None


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    if not isinstance(payload, dict):
        return
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return
    tool_name = payload.get("tool_name")
    if tool_name in ("Skill", "SlashCommand"):
        direction = toggle_direction(tool_name, payload.get("tool_input"))
        if direction is not None:
            if direction == "toggle":
                # The opposite of the CURRENT state: the explicit signal when
                # one exists, else the machine's default posture -- resolved
                # by is_tracking so this and the statusline cannot disagree
                # about what "current" means.
                current = _session_marker.is_tracking(
                    _session_marker.tracking_signal(session_id)
                )
                on = not current
            else:
                on = direction == "on"
            # Silent on success and on failure alike: the skill has the model
            # read the result back with `probe session status` and narrate it,
            # and set_tracking already validates the session id.
            _session_marker.set_tracking(session_id, on)
        return
    if tool_name != "Bash":
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
