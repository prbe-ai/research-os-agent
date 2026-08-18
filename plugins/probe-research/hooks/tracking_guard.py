#!/usr/bin/env python3
"""The deterministic side of the tracking toggle: flip on skill activation,
warn -- never gate -- on a probe write the flip should have prevented.

UserPromptSubmit plus PostToolUse on Skill/SlashCommand and Bash, two jobs
on one file of state:

FLIP. When the toggle-research-tracking skill is invoked, THIS HOOK writes
the session's tracking signal -- the same write `probe session untrack`
makes. Bare invocation is a TOGGLE: it flips to the opposite of the current
state, where "current" is the explicit signal if one exists and otherwise
the machine's default posture (`is_tracking` resolves that, exactly as the
statusline does -- the two surfaces must not disagree about what "current"
means). An explicit `off`/`on` (or the skill's synonyms) sets that state,
idempotently. `status`, or an argument that reads as neither, writes
nothing. The flip fires on BOTH invocation paths: PostToolUse catches the
model calling the skill as a tool, and UserPromptSubmit catches the
researcher TYPING the slash command -- which produces no tool use at all,
so without it the most common path would depend on the model running the
CLI from prose. The skill still has the model read the result back with
`probe session status` and narrate it, but the flip never DEPENDS on the
model obeying prose: the model's only job is to report, and a state that
contradicts what the researcher asked for is a hook failure to surface,
not to quietly repair. The declaration is the researcher's; recording it
is not a job to delegate to compliance.

WARN. When `probe session untrack` has marked this session off and a Bash
command still wrote research content through the probe CLI, this hook hands
the model one line of additionalContext restating the contract it is
violating. That is the whole mechanism: the write has already happened, the
hook never denies anything, and exit is 0 on every path.

DENY, THEN WARN. PreToolUse refuses a probe write the flip should have
prevented; PostToolUse still warns on anything that got through. This file
used to warn only, reasoning that "stop recording" spans surfaces no hook
can reach -- the SDK inside a training script, the hosted MCP, a job on
another machine -- so a deny would cover one path of several and teach the
agent the gate is advisory. What that argument missed is which path is the
common one: every project this has actually leaked was created by a coding
agent typing `probe ...` into Bash, the one path a hook DOES cover. A gate
that stops the observed failure is worth more than the consistency of
refusing to stop any of it, and the remaining surfaces keep the warn.

The deny is escapable and says how in its own reason: `/toggle-research-tracking
on`. That matters because this heuristic cannot tell a violation from the
team's day job (debugging Probe itself) -- so the escape is one command, and
REMOVAL_VERBS are never gated at all, because "record nothing" is not
"prevent cleanup".

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

DENY_REASON = (
    "Research tracking is OFF for this conversation -- this machine starts "
    "sessions untracked, or the researcher turned it off with "
    "/toggle-research-tracking -- so `{matched}` was refused before it ran. "
    "Create no Probe projects, experiments or runs, write no notes or Project "
    "Summary Markdown. Reading Probe is still fine and the actual work "
    "continues as normal. If the researcher wants this recorded, they turn "
    "tracking on (`/toggle-research-tracking on`); do not ask them to approve "
    "the write itself, and do not route around this."
)

MESSAGE = (
    "Research tracking is OFF for this conversation -- this machine starts "
    "sessions untracked, or the researcher turned it off with "
    "/toggle-research-tracking -- but `{matched}` "
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

# A typed slash command reaches UserPromptSubmit in one of two shapes,
# depending on whether the harness expands it before or after the hook: the
# raw string the researcher typed ("/probe-research:toggle-research-tracking
# off"), or the expanded command message carrying <command-name> (and the
# argument in <command-args> or a trailing "ARGUMENTS: ..." line). Parse both;
# NEVER scan free prose for direction words -- the expanded body is the skill
# text itself, which says "off" and "on" in every paragraph.
_COMMAND_NAME_TAG = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
_COMMAND_ARGS_TAG = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_ARGUMENTS_LINE = re.compile(r"^ARGUMENTS:[ \t]*(.+)$", re.MULTILINE)


#: Verbs inside a write group that REMOVE research rather than record it.
#: "Record nothing" is not "prevent cleanup" -- the first thing a researcher
#: does after finding an untracked session's writes on the dashboard is delete
#: them, and a layer that fought that would make the mess it exists to prevent
#: permanent. It matters most for the DENY: a blocked cleanup is a wall, not a
#: warning. The warn skips them for the same reason plus one of its own -- its
#: line reads "record nothing further ... consider undoing it", which is
#: nonsense aimed at an undo.
REMOVAL_VERBS = frozenset({"delete", "remove", "rm", "prune", "purge"})


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
            if verb and verb not in READ_VERBS and verb not in REMOVAL_VERBS:
                return "probe " + head + " " + verb
    return None


def _direction_from_words(words: "list[str]") -> "str | None":
    """Map an argument word list to a direction; None means do not touch the
    signal. Bare IS the toggle; `status` and unrecognised prose write nothing
    -- asking a question must never flip a switch."""
    if not words:
        return "toggle"
    if words[0] in OFF_WORDS:
        return "off"
    if words[0] in ON_WORDS:
        return "on"
    if words[0] in TOGGLE_WORDS:
        return "toggle"
    return None


def prompt_direction(prompt: object) -> "str | None":
    """Direction for a TYPED toggle command in a UserPromptSubmit prompt, or
    None. A typed slash command produces no tool use, so this is the only
    deterministic surface that path has.

    Lean silent, same as everywhere else: the raw shape must START with the
    command (a mid-sentence mention like "should I run
    /toggle-research-tracking?" is a question, not an invocation), and the
    expanded shape must name the slug in <command-name> exactly.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return None
    text = prompt.strip()
    tag = _COMMAND_NAME_TAG.search(text)
    if tag:
        if tag.group(1).split(":")[-1] not in TOGGLE_SKILL_SLUGS:
            return None
        args = _COMMAND_ARGS_TAG.search(text)
        if args is None:
            args = _ARGUMENTS_LINE.search(text)
        words = args.group(1).strip().lower().split() if args else []
        return _direction_from_words(words)
    if not text.startswith("/"):
        return None
    parts = text.splitlines()[0].lstrip("/").split()
    if not parts or parts[0].split(":")[-1] not in TOGGLE_SKILL_SLUGS:
        return None
    return _direction_from_words([w.lower() for w in parts[1:]])


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
    return _direction_from_words(inline_args.lower().split())


def _apply_direction(direction: str, session_id: str) -> None:
    """Write the signal a direction asks for. Silent on success and on
    failure alike: the skill has the model read the result back with `probe
    session status` and narrate it, and set_tracking already validates the
    session id."""
    if direction == "toggle":
        # The opposite of the CURRENT state: the explicit signal when one
        # exists, else the machine's default posture -- resolved by
        # is_tracking so this and the statusline cannot disagree about what
        # "current" means.
        current = _session_marker.is_tracking(
            _session_marker.tracking_signal(session_id)
        )
        on = not current
    else:
        on = direction == "on"
    _session_marker.set_tracking(session_id, on)


def _offending_write(payload: dict, session_id: str) -> "str | None":
    """The probe write this untracked session should not be making, or None.

    ONE gate for both events, so the deny and the warn can never disagree about
    what counts -- a refusal at PreToolUse followed by silence at PostToolUse
    (or the reverse) would read as the layer being unsure.

    Signal first: tracking on is the overwhelmingly common case and must cost
    one read, not a parse. Resolved through `is_tracking`, so a machine whose
    DEFAULT is off is covered -- that default is the researcher choosing what a
    new session starts at, and a layer that fired only when someone re-typed it
    per session would be silent exactly where the writes are.
    """
    if _session_marker.is_tracking(_session_marker.tracking_signal(session_id)):
        return None
    if payload.get("tool_name") != "Bash":
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return None
    return probe_write(command)


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
    hook_event = payload.get("hook_event_name")
    if hook_event == "UserPromptSubmit":
        direction = prompt_direction(payload.get("prompt"))
        if direction is not None:
            _apply_direction(direction, session_id)
        return
    if hook_event == "PreToolUse":
        matched = _offending_write(payload, session_id)
        if matched:
            sys.stdout.write(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny",
                            "permissionDecisionReason": DENY_REASON.format(
                                matched=matched
                            ),
                        }
                    }
                )
            )
        return
    tool_name = payload.get("tool_name")
    if tool_name in ("Skill", "SlashCommand"):
        direction = toggle_direction(tool_name, payload.get("tool_input"))
        if direction is not None:
            _apply_direction(direction, session_id)
        return
    if tool_name != "Bash":
        return
    matched = _offending_write(payload, session_id)
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
