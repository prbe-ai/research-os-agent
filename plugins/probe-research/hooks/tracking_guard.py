#!/usr/bin/env python3
"""The deterministic side of the tracking toggle: flip on skill activation,
warn -- never gate -- on a probe write the flip should have prevented.

UserPromptSubmit plus PostToolUse on Skill/SlashCommand and Bash, two jobs
on one file of state:

FLIP. When the tracking switch (track-work, or its legacy toggle names) is
invoked with an explicit direction, THIS HOOK writes
the session's tracking signal -- the same write `probe session untrack`
makes. Bare invocation flips to the opposite of the current state (that is
what a toggle is) -- but on track-work, which is ALSO the how-to manual,
only in a shape that is PROOF OF A PERSON. Typing `/track-work` is someone
reaching for the switch; the model activating the skill with no argument is
an agent opening its own manual mid-task, and that must never move the
switch. So the bare flip is granted to the raw typed line and to Claude
Code's <command-name> expansion of one, and withheld from the tool call and
from Codex's <skill> activation block -- both of which the MODEL can send.
A Codex researcher loses nothing: their typed `$slug` line arrives as the
raw shape first. The legacy toggle names, whose whole identity was the
switch and which have no manual to read, flip bare in every shape.
"Current" for a flip is the explicit signal if one exists and otherwise
the machine's default posture (`is_tracking` resolves that, exactly as the
statusline does -- the two surfaces must not disagree about what "current"
means). An explicit `off`/`on` (or the skill's synonyms) sets that state,
idempotently. `status`, or an argument that reads as neither, writes
nothing. The flip fires on BOTH invocation paths: PostToolUse catches the
model calling the skill as a tool, and UserPromptSubmit catches the
researcher TYPING the command -- which may produce no tool use at all, so
without it the most common path would depend on the model running the CLI
from prose. ONE invocation reaches those paths in up to three shapes,
though (a raw typed line, the harness's expansion of it, the model's tool
call), and a relative flip on each sighting cancels itself out -- so a
sighting whose shape the current invocation has not been seen in yet
CONVERGES on the target the first sighting resolved, and only a repeat of
a shape flips again. Both spellings are parsed: Claude Code's "/command"
plus <command-name>, and Codex's "$command" plus <skill><name>. The skill still has the model read the result back with
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

The deny is escapable and says how in its own reason: `/track-work on`.
That matters because this heuristic cannot tell a violation from the
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
import math
import shlex
import sys
import time

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
        # `probe notes team` PRINTS the team note. It reached the guard as an
        # unknown verb in a write group and was refused by a message that says
        # "Reading Probe is still fine" -- which is exactly the shape of bug the
        # deny list is supposed to avoid.
        "team",
    }
)

DENY_REASON = (
    "Research tracking is OFF for this conversation -- this machine starts "
    "sessions untracked, or the researcher turned it off with "
    "/track-work off -- so `{matched}` was refused before it ran. "
    "Create no Probe projects, experiments or runs, write no notes or Project "
    "Summary Markdown. Reading Probe is still fine and the actual work "
    "continues as normal. If the researcher wants this recorded, they turn "
    "tracking on (`/track-work on`); do not ask them to approve "
    "the write itself, and do not route around this."
)

MESSAGE = (
    "Research tracking is OFF for this conversation -- this machine starts "
    "sessions untracked, or the researcher turned it off with "
    "/track-work off -- but `{matched}` "
    "just wrote to Probe. Honor the declaration: record nothing further. If the "
    "researcher explicitly asked for this write, ask whether tracking should "
    "resume (`/track-work on`); otherwise consider undoing it, and "
    "continue the actual work without recording."
)

# The switch, by trailing slug -- in TWO classes, which differ ONLY in whether
# a bare invocation flips on the TOOL surface. The legacy names were a
# dedicated toggle and nothing else, so invoking one bare flips wherever it is
# seen; a resumed transcript from before the consolidation must keep meaning
# what it meant. `track-work` is ALSO the how-to manual, and an agent loads a
# manual bare dozens of times a session -- so there the bare flip is granted to
# the TYPED surface only (see `_bare_flips`). Old slugs stay matched forever:
# matching a name that no longer resolves costs nothing; missing one that did
# costs the flip.
BARE_FLIP_SLUGS = frozenset({"toggle-research-tracking", "research-tracking"})
GUIDANCE_SLUGS = frozenset({"track-work"})
TOGGLE_SKILL_SLUGS = BARE_FLIP_SLUGS | GUIDANCE_SLUGS

# Direction words, mirroring the skill's own reading of its argument ("off",
# "stop", "disable" / "on", "start", "resume"). "toggle"/"flip" is a relative
# flip and rides the same permission as bare invocation -- it is the same
# request, spelled out -- so it is granted wherever bare is. `status` and
# unrecognised prose write nothing: asking a question must never flip the
# switch.
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
# Codex runs this same hooks.json and spells both shapes differently: the line
# the researcher types is "$probe-research:toggle-research-tracking", and the
# expansion is a <skill><name> block rather than <command-name>. A parser that
# knew only the slash spelling left Codex with NO flip at all -- the researcher
# types the command, nothing writes the signal, and `probe session status`
# truthfully reports the state nobody changed.
_SKILL_NAME_TAG = re.compile(r"<skill>\s*<name>\s*/?([^<\s]+)\s*</name>", re.DOTALL)
_COMMAND_PREFIXES = ("/", "$")
_COMMAND_ARGS_TAG = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_ARGUMENTS_LINE = re.compile(r"^ARGUMENTS:[ \t]*(.+)$", re.MULTILINE)

#: The three SHAPES one invocation can be seen in. A harness delivers more than
#: one of them for a single command -- Claude Code expands the typed command
#: into a prompt AND has the model call the skill as a tool; Codex sends the
#: raw "$..." line AND a <skill> block -- and a toggle that flipped on every
#: sighting landed exactly where it started. That is the bug this names: two
#: flips per invocation read as a switch that does nothing.
SHAPE_RAW = "raw"
SHAPE_EXPANDED = "expanded"
SHAPE_TOOL = "tool"
#: Codex's `<skill><name>` block. A FOURTH shape rather than a spelling of
#: SHAPE_EXPANDED, because the two differ in the one property the bare flip
#: rests on: WHO can produce them. Claude Code builds a <command-name> block
#: only by expanding a command the researcher typed, and this plugin ships no
#: `commands/track-work.md` for the model to invoke into one. Codex emits this
#: block for a skill ACTIVATION, and the model activates skills too -- so on
#: its own it is not evidence of a person. A typed Codex invocation also sends
#: the raw `$slug` line, and that is the sighting the bare flip rides there.
SHAPE_SKILL_BLOCK = "skill-block"

#: How long one invocation's resolved target stays claimed, so a LATER sighting
#: of that same invocation converges on it instead of flipping it back. Only a
#: shape NOT yet seen converges; a repeat of a shape already seen is a second
#: invocation -- the researcher typing the command again -- and must flip. That
#: distinction is what lets this bound be generous instead of a race with how
#: fast someone types: measured, a typed command and the model's tool call for
#: it land ~2s apart while a person re-invoking takes ~12s, and neither number
#: has to be trusted here.
FLIP_CLAIM_TTL_SECONDS = 300.0


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


#: The shapes only a RESEARCHER can produce. An ALLOWLIST, and deliberately so:
#: everywhere else in this file ambiguity resolves toward silence, and a
#: denylist would resolve the next shape somebody adds -- a subagent shape, a
#: resumed-transcript shape, a typo at a call site -- toward WRITING the
#: signal. Unknown must mean "not proof of a person".
RESEARCHER_SHAPES = frozenset({SHAPE_RAW, SHAPE_EXPANDED})


def _bare_flips(slug: str, shape: str) -> bool:
    """May a direction-less invocation of `slug`, seen in `shape`, flip?

    THE ONE RULE THIS FILE TURNS ON. A bare invocation is a toggle -- the
    researcher reached for the switch and its name says what it does -- with a
    single carve-out: on `track-work`, which is the how-to manual as well as
    the switch, a shape the MODEL can produce is not a researcher at all. It is
    the model loading its own guidance, unprompted, mid-task, dozens of times a
    session, exactly when the switch must not move; a flip there would silently
    stop the recording this whole plugin exists to make automatic.

    So the SHAPE decides, not the intent we would have to guess at. Two shapes
    are proof of a person and flip on every slug (`RESEARCHER_SHAPES`); every
    other shape -- the tool call, Codex's activation block, anything added
    later -- flips only on the legacy slugs, which were a dedicated switch with
    no manual to read. A researcher on Codex still gets the flip: their typed
    `$slug` line arrives as SHAPE_RAW, which the model has no way to send.
    """
    if shape in RESEARCHER_SHAPES:
        return slug in TOGGLE_SKILL_SLUGS
    return slug in BARE_FLIP_SLUGS


def _direction_from_words(words: "list[str]", *, bare_flips: bool) -> "str | None":
    """Map an argument word list to a direction; None means do not touch the
    signal. `bare_flips` is the caller's answer to "may a direction-less
    invocation flip here?" -- see `_bare_flips`, which is where the slug class
    and the surface are weighed. `status` and unrecognised prose write nothing
    -- asking a question must never flip a switch."""
    if not words:
        return "toggle" if bare_flips else None
    if words[0] in OFF_WORDS:
        return "off"
    if words[0] in ON_WORDS:
        return "on"
    if words[0] in TOGGLE_WORDS:
        # A relative flip is only meaningful where the bare form is one:
        # `/track-work toggle` and `/track-work` are the same request, and a
        # surface that honours one while ignoring the other reads as a switch
        # that works only if you guess its vocabulary.
        return "toggle" if bare_flips else None
    if words[0] in STATUS_WORDS:
        return None  # a question, and questions never flip switches
    return None


def prompt_direction(prompt: object) -> "tuple[str | None, str, str]":
    """(direction, shape, slug) for a TYPED toggle command in a
    UserPromptSubmit prompt; direction is None when the prompt is not an
    invocation at all.

    A typed command may produce no tool use, so this is the only deterministic
    surface that path is guaranteed. The SHAPE comes back with the direction
    because both harnesses send the same invocation more than once, and
    telling the sightings apart is what keeps the flip to one -- see
    `_apply_direction`.

    THREE shapes reach this one function, and they do NOT agree about who
    could have sent them (`_bare_flips`): a raw typed line and Claude Code's
    <command-name> expansion are proof of a person, while Codex's <skill>
    block is an ACTIVATION -- which the model does too -- and is reported as
    SHAPE_SKILL_BLOCK so a bare one cannot flip the guidance slug.

    Lean silent, same as everywhere else: the raw shape must START with the
    command (a mid-sentence mention like "should I run
    /toggle-research-tracking?" is a question, not an invocation), and an
    expanded shape must name the slug in its own tag exactly. Those two
    guards are what keep the bare flip from being reachable by PASTED
    expansion-shaped text, now that bare flips here at all.
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return None, SHAPE_EXPANDED, ""
    text = prompt.strip()
    # The expanded shape is only credited when the prompt IS an expansion --
    # harness-built messages open with the command/skill block. Matching a tag
    # anywhere would let PASTED text (an issue body, documentation of this very
    # hook) flip tracking with no invocation at all, and an ARGUMENTS line
    # elsewhere in the paste would defeat the bare-never-flips guarantee.
    tag = None
    if text.startswith(("<command-message>", "<command-name>", "<skill>")):
        command_tag = _COMMAND_NAME_TAG.search(text)
        skill_tag = _SKILL_NAME_TAG.search(text)
        candidates = [m for m in (command_tag, skill_tag) if m]
        # Earliest tag wins: the leading block is the invocation; a later tag is
        # quoted content inside the expanded body.
        tag = min(candidates, key=lambda m: m.start(), default=None)
        # WHICH tag matched is what says who could have sent it -- see
        # SHAPE_SKILL_BLOCK. Same block, same parse, different provenance.
        shape = SHAPE_SKILL_BLOCK if tag is skill_tag else SHAPE_EXPANDED
    if tag:
        slug = tag.group(1).split(":")[-1]
        if slug not in TOGGLE_SKILL_SLUGS:
            return None, shape, slug
        args = _COMMAND_ARGS_TAG.search(text)
        if args is None:
            args = _ARGUMENTS_LINE.search(text)
        words = args.group(1).strip().lower().split() if args else []
        return (
            _direction_from_words(words, bare_flips=_bare_flips(slug, shape)),
            shape,
            slug,
        )
    if not text.startswith(_COMMAND_PREFIXES):
        return None, SHAPE_RAW, ""
    parts = text.splitlines()[0].lstrip("/$").split()
    slug = parts[0].split(":")[-1] if parts else ""
    if slug not in TOGGLE_SKILL_SLUGS:
        return None, SHAPE_RAW, slug
    return (
        _direction_from_words(
            [w.lower() for w in parts[1:]], bare_flips=_bare_flips(slug, SHAPE_RAW)
        ),
        SHAPE_RAW,
        slug,
    )


def toggle_direction(tool_name: str, tool_input: object) -> "tuple[str | None, str]":
    """(direction, slug); direction is `"on"`, `"off"`, `"toggle"`, or None
    (do not touch the signal).

    THE TOOL SHAPE, so `track-work` bare is a manual read and writes nothing
    (`_bare_flips`); only an explicit direction word flips it here. The legacy
    slugs keep their bare flip, having never been a manual. `status` and
    unrecognised prose never touch the signal -- guessing would flip on a
    sentence like "what is going on?".

    Field names mirror telemetry.match_skill: a Skill call carries the slug in
    `skill` with the argument in `args`; a SlashCommand carries one `command`
    string with the argument inline. Namespacing (`probe-research:`) and a
    leading slash are stripped the same way there too.
    """
    if tool_name not in ("Skill", "SlashCommand") or not isinstance(tool_input, dict):
        return None, ""
    inline_args = ""
    matched_slug = None
    for field in ("skill", "command", "skill_name", "name"):
        value = tool_input.get(field)
        if not (isinstance(value, str) and value.strip()):
            continue
        parts = value.strip().lstrip("/").split(None, 1)
        if parts[0].split(":")[-1] in TOGGLE_SKILL_SLUGS:
            matched_slug = parts[0].split(":")[-1]
            if len(parts) > 1:
                inline_args = parts[1]
            break
    if matched_slug is None:
        return None, ""
    separate = tool_input.get("args")
    if isinstance(separate, str) and separate.strip():
        inline_args = (inline_args + " " + separate).strip()
    return (
        _direction_from_words(
            inline_args.lower().split(), bare_flips=_bare_flips(matched_slug, SHAPE_TOOL)
        ),
        matched_slug,
    )


def _claim_path(session_id: str):
    """Beside the signal it explains, named for what it holds."""
    return _session_marker.sessions_dir() / (session_id + ".flip-claim")


def _read_claim(session_id: str) -> "dict | None":
    """This session's still-fresh claim, or None.

    Unreadable, malformed and stale all read as None. A claim is bookkeeping
    on top of the signal, never the state itself: losing one costs a flip that
    lands the same way the researcher asked for anyway, while trusting a
    broken one would cost the flip they DID ask for.
    """
    try:
        with open(_claim_path(session_id), encoding="utf-8") as handle:
            claim = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(claim, dict):
        return None
    target = claim.get("target")
    at = claim.get("at")
    seen = claim.get("seen")
    # ABSENT is not a mismatch. A claim written by a plugin version that did
    # not record the slug can only be compared the old way -- converging it is
    # what that version meant -- and treating absence as "a different slug"
    # would flip twice across an upgrade that lands mid-window.
    slug = claim.get("slug")
    if slug is not None and not isinstance(slug, str):
        return None
    if target not in ("on", "off"):
        return None
    # `bool` is an `int`, and NaN compares False against every bound -- a claim
    # carrying either would never expire. A truncated or hand-edited file is
    # the only way to reach them, and every one of them costs a flip, so each
    # reads as "no claim" rather than "a claim I cannot evaluate".
    if isinstance(at, bool) or not isinstance(at, (int, float)) or not math.isfinite(at):
        return None
    if not isinstance(seen, list) or not all(isinstance(s, str) for s in seen):
        return None
    if not seen:
        # No shape recorded means every shape reads as unseen, so the next
        # sighting -- including a repeat, which is a NEW invocation -- would
        # converge. _write_claim cannot produce this; only a damaged file can.
        return None
    if time.time() - at > FLIP_CLAIM_TTL_SECONDS:
        return None
    return {"target": target, "seen": seen, "at": at, "slug": slug}


def _write_claim(
    session_id: str, on: bool, seen: "list[str]", slug: str, at=None
) -> None:
    """Fail-soft, like everything else here: a claim that cannot be written
    costs a duplicate flip, and a hook that raised would cost the session.

    `at` carries the ORIGINAL claim time through a converging rewrite. Stamping
    a fresh one there would let a chain of sightings renew the window
    indefinitely, so the bound would describe the last sighting rather than the
    invocation it belongs to.
    """
    record = {
        "target": "on" if on else "off",
        "at": time.time() if at is None else at,
        "seen": seen,
        "slug": slug,
    }
    try:
        path = _claim_path(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        pass


def _apply_direction(direction: str, session_id: str, shape: str, slug: str) -> None:
    """Write the signal a direction asks for, ONCE PER INVOCATION.

    Silent on success and on failure alike: the skill has the model read the
    result back with `probe session status` and narrate it, and set_tracking
    already validates the session id.

    An explicit `on`/`off` needs none of the claim below -- setting the same
    state twice IS setting it once. Only the bare toggle is relative, and only
    a relative write can cancel itself.

    ONE INVOCATION is (slug, shape-not-yet-seen). The SLUG half matters
    because two different switch names in one window are two invocations
    however their shapes line up: a typed bare `/track-work` followed by a
    bare legacy toggle converged on the first one's target and the second
    silently did nothing -- a switch that does nothing being the exact
    symptom the claim was added to fix.
    """
    if not _session_marker.valid_session_id(session_id):
        # The same refusal set_tracking makes, one step earlier, because the
        # claim is a FILENAME too: validating in only one of the two places is
        # how a guarded write grows an unguarded sibling, and this one escaped
        # the sessions directory entirely on a traversal id.
        return
    if direction != "toggle":
        # Clear the claim rather than ignoring it. An explicit setter is
        # absolute and needs no claim of its own, but LEAVING one behind
        # outlives the invocation that wrote it: the next bare toggle,
        # arriving in a shape that claim has not seen, converges on a target
        # nobody asked for and the switch silently does nothing -- the shipped
        # bug, back through the door this branch just closed.
        try:
            _claim_path(session_id).unlink()
        except OSError:
            pass
        _session_marker.set_tracking(session_id, direction == "on")
        return

    claim = _read_claim(session_id)
    same_invocation = claim is not None and claim["slug"] in (slug, None)
    if same_invocation and shape not in claim["seen"]:
        # Another shape of the invocation that wrote this claim: converge on
        # the target it already resolved, and record the shape so a third
        # sighting cannot flip either.
        on = claim["target"] == "on"
        _write_claim(session_id, on, claim["seen"] + [shape], slug, at=claim["at"])
    else:
        # A shape already seen, a DIFFERENT slug, or nothing claimed -- each of
        # them a new invocation, and a new invocation always flips. Flip
        # to the opposite of the CURRENT state: the explicit signal when one
        # exists, else the machine's default posture -- resolved by is_tracking
        # so this and the statusline cannot disagree about what "current"
        # means.
        on = not _session_marker.is_tracking(
            _session_marker.tracking_signal(session_id)
        )
        _write_claim(session_id, on, [shape], slug)
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
        direction, shape, slug = prompt_direction(payload.get("prompt"))
        if direction is not None:
            _apply_direction(direction, session_id, shape, slug)
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
        direction, slug = toggle_direction(tool_name, payload.get("tool_input"))
        if direction is not None:
            _apply_direction(direction, session_id, SHAPE_TOOL, slug)
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
