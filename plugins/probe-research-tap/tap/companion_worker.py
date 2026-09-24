"""The Probe daemon: records a coding session in Probe so the agent does not have to.

`python -m tap companion --session-id ... --transcript ... --cwd ...`, started by
the session's own `tap watch` (see `companion_supervisor.py`) whenever the
session's `probe` switch is in the `daemon` state, and stopped when it leaves it
or the session ends. One per session. Stdlib only.

A CYCLE, every ~minute while the transcript grows:

    read new transcript lines ─> observe (parse, extract ids, REDACT)
        │
        ▼
    one gateway call: "what should be recorded?"  ──fail──> lease lapses; agent records
        │
        ▼
    for each proposal:  allowed kind? ─ grounded (ids seen this session, evidence
                        after the handover boundary)? ─ read the target: already there?
        │                        any "no" ─> HELD, with the reason
        ▼
    persist every decision + the new watermark in ONE transaction   (persist ...)
        │
        ▼
    publish pending proposals with Idempotency-Key                   (... then publish)
        │
        ▼
    renew the write lease -- ONLY here, when a cycle finished

THE LEASE IS THE SAFETY. While it is live the `probe` CLI refuses the agent's
ambient writes; when the worker is down, hung, unauthorized or out of budget the
lease lapses or is released with a reason, and the agent records again -- fall
back to the agent, never to silence.

WHAT IT MAY WRITE (`KINDS`): titled sub-notes, a run's or project's empty name /
description / notes, tags (added, never removed), papers, run-to-run lineage,
the end of a run this session opened with `probe run start`, and files the
session produced. Never a delete, never a run start, never `invalid`, never the
team note, never an entity this session did not touch.

SHADOW (`PROBE_COMPANION_SHADOW=1`, or `companion_shadow: true` in the probe
config): runs while the switch is `on`, takes no lease, and records every
decision as HELD (`shadow`) -- the comparison against what the agent wrote.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import mimetypes
import os
import re
import signal
import time
from pathlib import Path
from typing import Any, NamedTuple

from tap import companion_api as api_mod
from tap import companion_lease as lease
from tap import companion_ledger as ledger_mod
from tap import companion_observe as observe
from tap import config as cfg
from tap import secrets

log = logging.getLogger("tap.companion")

STATE_DAEMON = "daemon"
STATE_FULL = "full"

#: Cadence. A cycle runs when the transcript has grown and either it has been
#: quiet for QUIET_SECONDS or the oldest undecided byte is MAX_WAIT_SECONDS old,
#: so a burst of tool calls is decided together rather than line by line.
POLL_SECONDS = 5
QUIET_SECONDS = 45
MAX_WAIT_SECONDS = 180
#: Raw transcript bytes a cycle may read. The model sees events WHOLE, so what
#: bounds a cycle is RENDER_BUDGET_CHARS: a cycle whose rendered events would
#: exceed it ends at the last line that fits, and the next cycle continues from
#: there. Nothing is cut to make room (see `observe.EVENT_CEILING`).
CHUNK_BYTES = 1536 * 1024
#: Rendered transcript per cycle, in characters: with the rules (~17k), the
#: framing and the session facts it stays under the gateway's
#: MAX_TOTAL_CHARS = 400_000 per request.
RENDER_BUDGET_CHARS = 300_000
#: The conclusions pass's view of the whole session (its narration and the
#: outputs of the commands that did work), in characters.
CONCLUSIONS_BUDGET_CHARS = 240_000
#: `<sid>.turn`, written by the Stop hook with the transcript's size when the
#: agent's turn ended: the worker drains to that offset at once, then runs the
#: conclusions pass over the whole session.
TURN_SUFFIX = ".turn"
#: Prior context shown (read-only) on the first cycle after a handover.
CONTEXT_BYTES = 64 * 1024
CYCLE_WALL_SECONDS = 150
FINAL_WALL_SECONDS = 60
GATEWAY_ATTEMPTS = 2
#: One gateway call's ceiling; the server's own upstream timeout is 55s.
GATEWAY_TIMEOUT_SECONDS = 60
GATEWAY_COOLDOWN_SECONDS = 300
#: After a 403 from the gateway (tenant not enabled, wrong credential kind).
FORBIDDEN_COOLDOWN_SECONDS = 1800
RENEW_EVERY_SECONDS = 30
#: No network call is started with less lease than this left: a call that would
#: outlive the lease is exactly the "both sides write" window.
LEASE_MARGIN_SECONDS = 35
#: Prompts the gateway refused (too large, rejected by the provider) in a row
#: before the worker hands writing back instead of skipping another range.
MAX_SKIPPED_CYCLES = 3
#: Unusable answers (empty, truncated, not the JSON asked for) for the SAME
#: range before that range is skipped rather than retried forever.
MAX_UNUSABLE_ANSWERS = 3
#: How much of a range the agent owned is scanned (no model call) for the ids
#: and run starts in it, so the daemon can still decorate what the agent made.
ABSORB_BYTES = 2 * 1024 * 1024
#: `main()` exit code meaning "respawning me cannot help" (another worker holds
#: the session, or the ledger is newer than this code).
EXIT_DO_NOT_RESPAWN = 3
#: A thinking model spends output tokens on reasoning before the answer; too low
#: a ceiling comes back with empty content. The server clamps to its own.
MAX_OUTPUT_TOKENS = 16384
KNOWN_IDS_SHOWN = 60  # ids listed to the model (KNOWN IDS and ENTITIES use the same window)
MAIN_NOTE_BODY_CHARS = 20_000
NOTE_BODY_CHARS = 4_000
MAX_FILES_SHOWN = 150  # result files the conclusions pass lists for notes, per pass
MAX_ADMITTED_RUNS = 60  # runs named in work output, admitted per conclusions pass
# Retrieval (the model asks for more instead of guessing): per cycle, at most
# RETRIEVAL_ROUNDS answers that ask, RETRIEVAL_REQUESTS requests in all and
# RETRIEVAL_EXPAND_CHARS of expanded text; every request stays under the
# gateway's 400_000-character limit (`app/companion/router.py` MAX_TOTAL_CHARS).
RETRIEVAL_ROUNDS = 3
NO_RETRIEVAL_NOTE = ("\n\n(Retrieval is not available for this request: answer now, with proposals, from what "
                     "is shown above.)")
#: The conclusions pass runs at most once per this many seconds per session (a
#: burst of short turns is concluded together; `finish` always concludes).
CONCLUSIONS_MIN_GAP_SECONDS = 600
#: Of the session-end wall, the seconds kept for publishing after the final pass.
FINAL_PUBLISH_RESERVE_SECONDS = 15
#: The least of the session the conclusions view keeps when the facts are large.
MIN_CONCLUSIONS_VIEW_CHARS = 20_000
RETRIEVAL_REQUESTS = 6
RETRIEVAL_EXPAND_CHARS = 48 * 1024
REQUEST_CHARS_LIMIT = 390_000
PUBLISH_ATTEMPTS = 8
#: Per-session ceilings, enforced here and never raised by a server reply.
MAX_WRITES_PER_SESSION = 400
MAX_FILE_NOTES_PER_SESSION = 200  # a cap of their own: a big sweep's files never crowd out a note
#: A file or run the session produced is at most this much older than the
#: session's first line (clock skew between the transcript and the filesystem).
SESSION_START_SLACK_SECONDS = 120
MAX_PROPOSALS_PER_CYCLE = 25
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
#: A text file is scanned WHOLE for credentials before it may upload; one larger
#: than this is not uploaded at all.
MAX_TEXT_ARTIFACT_BYTES = 10 * 1024 * 1024
#: Runs checked for the same bytes before an upload (the session's most recent).
MAX_DEDUPE_RUNS = 40
#: Text or not is decided by CONTENT, never by the name: the first TEXT_SNIFF_BYTES
#: decode as UTF-8 and hold no NUL byte. A credential file with an odd suffix is
#: still text, so it is still scanned.
TEXT_SNIFF_BYTES = 8192

ENV_SHADOW = "PROBE_COMPANION_SHADOW"
SKIPPED_OUTCOME = "skipped: no words, run event, directed command or produced file in this chunk"
# The Jev judge (`POST /v1/companion/judge`, plan F12), on unless this says off.
# Per finished turn it (a) records, per kind, whether the turn holds material for
# it, beside what the model then decided (SHADOW: it never skips a model call
# unless SKIP is switched on and proven, below), and (b) names the agent's
# paragraphs no existing note records -- the conclusions pass checks each, and
# the session-end audit checks the whole session. A workspace the server has not
# enabled answers 403 once; the session then stops asking.
ENV_JUDGE = "PROBE_COMPANION_JUDGE"
JUDGE_SLICE_CHARS = 80_000  # a gate slice; `_judge` trims further to the route's budget
#: The route's own budget (`app/companion/router.py:judge_budget_chars`): text,
#: notes and question text together within (TOKEN_BUDGET - 35 per question) x 3.77.
JUDGE_TOKEN_BUDGET = 24_000
JUDGE_TOKENS_PER_QUESTION = 35
JUDGE_CHARS_PER_TOKEN = 3.77
JUDGE_CALLS_PER_TURN = 2
JUDGE_QUESTIONS_PER_CALL = 12
JUDGE_NOTES = 16
JUDGE_NOTE_CHARS = 1000
JUDGE_RESERVE_SECONDS = 90  # of the cycle's wall time kept for the model call after judging
JUDGE_TARGET_P = 0.5
#: SKIP (plan T16b): a turn's model calls are skipped only when EVERY kind has
#: been proven on shadow data (`scripts/companion_judge_report.py`: recall >= 0.95
#: over >= 200 turns from >= 10 sessions, listed in ENV_JUDGE_SKIP_KINDS) and the
#: judge says "nothing" >= JUDGE_SKIP_NOTHING_P with every kind < JUDGE_SKIP_KIND_P.
#: A run start or end, a directed command, a written or produced file always call
#: the model; one skip-verdict turn in JUDGE_HOLDOUT_EVERY calls it anyway, to keep
#: measuring. Until then the judge is SHADOW: it records and never skips.
ENV_JUDGE_SKIP_KINDS = "PROBE_COMPANION_JUDGE_SKIP_KINDS"
#: The session-end audit is opt-in (`PROBE_COMPANION_JUDGE_AUDIT=on`): on the two
#: replay trials it found nothing the conclusions passes had missed and cost one
#: more model pass per session (+40% input tokens on the inline trial).
ENV_JUDGE_AUDIT = "PROBE_COMPANION_JUDGE_AUDIT"
#: The kinds SKIP must have proven. Not `file_note`: a file note needs a file the
#: session produced, and a produced file is a deterministic trigger SKIP never skips.
JUDGE_SKIP_KINDS = ("note", "run_end", "artifact", "edge", "describe", "tag", "paper")
JUDGE_SKIP_NOTHING_P = 0.8
JUDGE_SKIP_KIND_P = 0.5
JUDGE_HOLDOUT_EVERY = 5
#: The session-end audit judges every paragraph of the session, in batches.
JUDGE_AUDIT_CALLS = 8
JUDGE_SKIPPED_OUTCOME = "skipped: the judge found nothing to record in this turn"
JUDGE_MIN_PARAGRAPH = 80
JUDGE_KIND_QUESTIONS = {
    "note": "This part of a coding session states a result, a decision, a chosen configuration or a caveat.",
    "run_end": "This part shows a tracked run's process finishing or failing.",
    "artifact": "This part produces a result file worth keeping: a table, plot, report or data file.",
    "edge": "This part starts a run from another run's result: a follow-up, an ablation, a variant or a retry.",
    "describe": "This part states the goal or question of a project or experiment.",
    "tag": "This part tests a named concept or technique a run could be tagged with.",
    "paper": "This part reads a research paper.",
    "file_note": "This part shows what a result file holds.",
    "nothing": "Nothing in this part needs recording in the team's experiment tracker.",
}

KINDS = ("note", "describe", "tag", "paper", "edge", "run_end", "artifact", "file_note")
TARGET_TYPES = ("project", "run", "artifact")
RUN_RELATIONS = ("forked_from", "resumed_from", "retried_from", "branched_from", "derived_from")
NOTE_TITLE_PREFIX = "companion: "

#: Never uploaded, whatever the model says: the names credentials live under.
_SECRET_NAME_RE = re.compile(
    r"(^|/)(\.env.*|\.netrc|\.pgpass|id_[a-z0-9]+|.*\.pem|.*\.key|.*\.p12|.*\.pfx|.*\.jks|"
    r".*\.keystore|.*\.kdbx|.*\.keychain(-db)?|.*credential.*|.*secret.*|.*token.*|config\.json|\.npmrc|\.pypirc|kubeconfig|"
    r".*\.tfstate(\..*)?|\.htpasswd|\.git-credentials|\.mcp\.json)$",
    re.IGNORECASE,
)

_stop = False


def _on_term(_signum, _frame) -> None:
    global _stop
    _stop = True


# ---------------------------------------------------------------------------
# The prompt.
# ---------------------------------------------------------------------------

RULES_FILE = Path(__file__).with_name("companion_rules.md")
# track-work's detailed reference (`make sync-tap-core`): the conclusions pass
# carries it whole; a cycle can ask for one section (`expand "reference#<heading>"`).
REFERENCE_FILE = Path(__file__).with_name("companion_reference.md")

FRAMING = """You are the Probe daemon. You read a coding agent's session transcript and record the work in Probe, the team's system of record for ML work, so the agent does not have to. Your record must be as complete as a careful researcher's own: a teammate who reads only Probe, never this chat, must learn what was tried, what came out, what was decided and why, and what the results do not show.

The agent does its own launching: it creates the project, experiment and sweep group, starts runs (`probe exec`, `probe run start`, the SDK), and logs each run's metrics and the files the run itself attaches. Anything it wrote with `--directed` was asked for by the researcher: never repeat it. Everything else is yours.

WHAT YOU MUST RECORD, each time the transcript shows it:
1. Conclusions. Every result, decision or caveat the agent STATES (in its messages, or in a results table a command printed) becomes a note on the experiment it concerns: which option won and by how much, which configuration was chosen and why, what was dropped and why, what the results do not show. Put the numbers in with their names, exactly as the transcript gives them, and state BOTH sides of a comparison, never only the difference: "val F1 0.812 vs 0.774 for the frozen encoder", not "+0.038 over the frozen encoder". A run's metrics are NOT a record of the decision: the decision needs its own note.
2. The experiment's document. When an experiment's main note is empty, write it: kind "note" with "main": true, a markdown body with the sections "## Result" (the winner and what it beat, each with its number), "## How it was chosen", "## Setup choices worth knowing", "## What these results do not show", and "## Next" when the agent named one. Later findings go in titled sub-notes (kind "note" without "main").
3. The project's document. When the parent project's main note is empty and the work reached a conclusion, write a short one ("main": true): what the project is, the decision with its numbers, what the results do not show, what comes next.
4. Empty descriptions. The server writes a project's description itself once a run finishes under it, so a PARENT project with no runs of its own stays empty: give it one sentence from the researcher's stated goal (kind "describe"). An experiment's description is its question, set when it was created: never write one. Never describe runs: the server names and describes runs itself.
5. Lineage. A run that starts from what another run chose (a follow-up on the winner, an ablation of the chosen configuration) is linked to that run: kind "edge", "relation": "derived_from". A retry of a failed or wrong run: "retried_from".
6. Superseded runs. A run the session itself replaced (a first attempt with a bug, a run opened by mistake) is tagged "abandoned" (kind "tag"), and gets a one-line sub-note naming the run that replaced it and why. You never delete.
7. File notes. Each result file listed under FILES (their notes are empty) gets a one-line note saying what it holds and what it is for. Its name, the run it belongs to and the command that wrote it are usually enough: "out/lr-3e-4/metrics.json" on run "lr-3e-4" holds that configuration's scores. Put them in "file_notes" (below), one entry for EVERY file under FILES; leave a file out only when nothing in the session says what it is.
8. Papers the session actually read; tags for the concepts a run tests.

Write only what the transcript states: never guess a number, a result, a cause or an id. A comparison in ANY note, main documents included, names both values ("val F1 0.812 vs 0.774 for the frozen encoder"), even where the agent's own summary gave only the difference ("+0.038"): the other value is in the session (a results table, a run's output); `grep` for it if it is not shown.

Answer with ONE JSON object: {"proposals": [...], "file_notes": {"<file id from FILES>": "<one line>"}} and nothing else ("file_notes" only when FILES is given). An empty list is right only when nothing above applies. Each proposal:
  {"kind": one of "note" | "describe" | "tag" | "paper" | "edge" | "run_end" | "artifact" | "file_note",
   "target": {"type": "project" | "run" | "artifact", "id": "<uuid seen in this session>"},
   "evidence": {"from": <offset>, "to": <offset>},  -- the [.. @offset] markers of the events that justify it
   "why": "<one sentence>",
   ...kind fields}
Kind fields:
  note       "title": short, "body": markdown; "main": true writes the entity's main document (only when it is empty). Target: the experiment (a project id) or the parent project, or a run for a run-level finding.
  describe   any of "name" (real English, not a re-cased slug), "description", "notes" (what a later reader should distrust). Only fills EMPTY fields.
  tag        "tags": [lowercase-kebab concepts, or "abandoned"].
  paper      target is the project; "title", "source_url", optional "summary_md", "tags". Only a paper the session actually read.
  edge       target is the NEW run; "source_run_id": the run it came from; "relation": forked_from | resumed_from | retried_from | branched_from | derived_from; "reason".
  run_end    target is a run the session opened with `probe run start` whose process the transcript shows finished; "status": completed | failed.
  artifact   target is the run or project; "path": a file the session produced (as written in the transcript); optional "kind", "notes".
  file_note  target is a file listed under FILES ("type": "artifact"); "notes": one line.
The ids you may use are listed under KNOWN IDS, ENTITIES and FILES. The set is closed: an id that is not listed cannot be used, so leave the proposal out rather than inventing one. Do not re-propose anything under ALREADY DECIDED. Never delete, never start a run, never mark anything invalid."""


def _rules() -> str:
    try:
        text = RULES_FILE.read_text(encoding="utf-8")
    except OSError:
        return ""
    # The skill's frontmatter is for the harness's skill loader, not the model.
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end >= 0:
            text = text[end + 4 :]
    return text.strip()


def _reference() -> str:
    try:
        return REFERENCE_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _reference_sections() -> dict[str, str]:
    """The reference's `## ` sections by heading (lowercased, number dropped)."""
    sections: dict[str, str] = {}
    heading, lines = None, []
    for line in _reference().splitlines():
        if line.startswith("## "):
            if heading:
                sections[heading] = "\n".join(lines).strip()
            heading, lines = re.sub(r"^\d+\.\s*", "", line[3:].strip()).lower(), [line]
        elif heading:
            lines.append(line)
    if heading:
        sections[heading] = "\n".join(lines).strip()
    return sections


def _retrieval_help() -> str:
    headings = ", ".join(f'"reference#{h}"' for h in _reference_sections())
    return (
        "\n\nIF WHAT YOU NEED IS NOT SHOWN (an event marked as cut, an earlier part of the session, the "
        "detailed recording reference), answer instead with ONE JSON object {\"need\": [...]} and no "
        "proposals. Requests: {\"expand\": \"<event id from a cut marker>\", \"page\": <n from 0>} "
        f"({observe.EXPAND_PAGE_CHARS // 1024} KB pages of one event); {{\"grep\": \"<plain text>\"}} (up to "
        f"{observe.GREP_MAX_HITS} matching places in the whole session, with their event ids); "
        f"{{\"expand\": \"reference#<heading>\"}} (one section of the detailed reference: {headings}). "
        f"At most {RETRIEVAL_ROUNDS} such answers and {RETRIEVAL_REQUESTS} requests per turn; what comes back "
        "is context: evidence must still cite the [.. @offset] markers of events you were shown above."
    )


def build_messages(
    *,
    chunk_text: str,
    prior_context: str,
    known_ids: dict[str, str | None],
    decided: list[dict],
    feedback: list[dict],
    run_starts: list[str],
    cwd: str,
    entities: list[dict] | None = None,
    files: list[dict] | None = None,
    conclusions: bool = False,
    targets: list[dict] | None = None,
    existing_notes: list[str] | None = None,
) -> list[dict]:
    known = [{"id": eid, "type": etype} for eid, etype in list(known_ids.items())[-KNOWN_IDS_SHOWN:]]
    facts: dict[str, Any] = {
        "working_directory": cwd,
        "KNOWN IDS": known,
        "runs_this_session_opened_with_probe_run_start": run_starts[-20:],
        "ALREADY DECIDED": decided,
    }
    if entities:
        facts["ENTITIES"] = entities
    if files:
        facts["FILES"] = files
    if existing_notes:
        facts["NOTES ALREADY ON THESE ENTITIES"] = existing_notes[:60]
    if feedback:
        facts["researcher_feedback_on_your_earlier_writes"] = feedback
    user = [f"SESSION FACTS\n{json.dumps(facts, indent=1)}"]
    if prior_context:
        user.append(
            "EARLIER IN THE SESSION (the agent recorded this part itself; context only, "
            f"propose nothing about it)\n{prior_context}"
        )
    if conclusions:
        user.append(
            "THE WHOLE SESSION SO FAR (the agent's own words, and the output of the commands that did "
            "work). The agent just finished a turn. Check the record against WHAT YOU MUST RECORD, "
            "item by item, and propose what is missing:\n"
            "- every result, decision, chosen configuration and caveat stated here that neither ALREADY "
            "DECIDED nor NOTES ALREADY ON THESE ENTITIES covers (never restate a note in new words: a "
            "sub-note is for what the existing ones miss);\n"
            "- every main_document and description under ENTITIES that reads \"(empty)\";\n"
            "- lineage: for EACH run under ENTITIES, did it start from another run's chosen "
            "configuration (a follow-up on the winner, an ablation or variant of it)? Then an edge "
            "\"derived_from\" to that run. A rerun of a broken or superseded run: \"retried_from\";\n"
            "- runs the session replaced or opened by mistake: tag \"abandoned\" plus a one-line "
            "sub-note;\n"
            f"- \"file_notes\": one line for each of the {len(files or [])} files under FILES.\n\n"
            f"{chunk_text}"
        )
        if targets:
            flagged = "\n\n".join(f"[assistant @{t['offset']}] (judge p={t['p']})\n{t['paragraph']}" for t in targets)
            user.append(
                "PARAGRAPHS A JUDGE FLAGGED (each seems to state a result, decision, setting or caveat that no "
                "existing note records; check each against the record and ALREADY DECIDED, and propose what "
                f"is really missing, citing its offset)\n{flagged}"
            )
    else:
        user.append(f"NEW TRANSCRIPT\n{chunk_text}")
    rules = _rules()
    system = FRAMING + _retrieval_help() + ("\n\n# Recording rules (track-work)\n\n" + rules if rules else "")
    reference = _reference() if conclusions else ""
    if reference:
        system += "\n\n# Recording reference (track-work, detailed)\n\n" + reference
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user)}]


# ---------------------------------------------------------------------------
# Deciding: validate, ground, read before write.
# ---------------------------------------------------------------------------


class SessionEntities(NamedTuple):
    """What the conclusions pass knows about the session's entities."""

    entities: list[dict]  # ENTITIES: projects/experiments and runs, with what is still empty
    files: dict[str, dict]  # file id -> its listing and row, for the file-note gate
    shown: list[dict]  # FILES as the model sees them
    admitted: dict[str, str]  # runs admitted from work-command output
    note_lines: list[str]  # what the entities' notes already say (the judge's state)


class SessionView(NamedTuple):
    """The session as the conclusions pass reads it, before it is fitted to a budget."""

    prompts: list[tuple[int, str]]  # the researcher's prompts, every range
    words: list[tuple[int, str]]  # the agent's words, daemon-owned ranges
    outputs: list[tuple[int, str]]  # work commands with their output, daemon-owned ranges
    produced: str
    touched: set[str]
    mentioned: set[str]
    workdirs: list[str]


class Held(Exception):
    """A proposal that will not be written, and why."""


def idem_key(session_id: str, kind: str, target: str, payload: dict) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{session_id}|{kind}|{target}|{canonical}".encode()).hexdigest()
    return f"pc1-{digest[:48]}"


def _str(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value[:limit] if value else None


def _uuid(value: Any) -> str | None:
    if isinstance(value, str) and observe.UUID_RE.fullmatch(value.strip().lower()):
        return value.strip().lower()
    return None


class Decider:
    """Turns one raw model proposal into a write op, or raises Held."""

    def __init__(self, api: api_mod.Api, *, known_ids: dict[str, str | None], run_starts: set[str],
                 run_ends: set[str], cwd: Path, touched: set[str], chunk_text: str,
                 range_start: int, range_end: int, agent_ranges: list | tuple = (),
                 produced_text: str = "", workdirs: list[str] | tuple = (),
                 files: dict[str, dict] | None = None, started_at: float | None = None) -> None:
        self.api = api
        self.known_ids = known_ids
        self.run_starts = run_starts
        self.run_ends = run_ends
        self.cwd = cwd
        self.touched = touched
        self.chunk_text = chunk_text
        self.range_start = range_start
        self.range_end = range_end
        self.agent_ranges = [tuple(r) for r in agent_ranges]
        self.started_at = started_at
        self.produced_text = produced_text
        self.workdirs = list(workdirs)
        #: FILES shown to the model: artifact id -> {"listing": path, "row": row}.
        #: A file note may target only one of these (the daemon read them itself).
        self.files = dict(files or {})
        self._entity_cache: dict[tuple[str, str], dict] = {}
        self._artifact_cache: dict[tuple[str, str], list] = {}

    # -- helpers --
    def _get(self, path: str) -> Any:
        """A read for deciding. Refused or failed reads HOLD the proposal; only a
        401 (the key itself is dead) propagates and stops the worker."""
        try:
            return self.api.get(path)
        except (api_mod.Rejected, api_mod.Forbidden) as exc:
            raise Held(f"read refused ({exc.status}): {path}") from None
        except api_mod.Retryable:
            raise Held(f"read failed, not written: {path}") from None

    def _resolve(self, path_text: str) -> Path:
        """The file a path names. A relative path is relative to where the
        command that printed it ran: the folders the session `cd`'d into, most
        recent first, then the folder the session started in."""
        path = Path(os.path.expanduser(path_text))
        if path.is_absolute():
            candidates = [path]
        else:
            bases = [Path(os.path.expanduser(d)) for d in reversed(self.workdirs)]
            bases = [b if b.is_absolute() else self.cwd / b for b in bases] + [self.cwd]
            candidates = [base / path for base in bases]
        for candidate in candidates:
            try:
                resolved = candidate.resolve(strict=True)
            except (OSError, RuntimeError):
                continue
            if resolved.is_file():
                return resolved
        raise Held("file does not exist")

    def _artifact_rows(self, etype: str, eid: str, *, strict: bool) -> list:
        """An entity's artifacts, once per cycle. `strict` holds the proposal on a
        failed read (the target); otherwise an unreadable entity is skipped."""
        key = (etype, eid)
        if key not in self._artifact_cache:
            try:
                listing = self._get(f"/v1/{etype}s/{eid}/artifacts")
            except Held:
                if strict:
                    raise
                listing = []
            rows = listing.get("items", listing.get("artifacts", [])) if isinstance(listing, dict) else listing
            self._artifact_cache[key] = [r for r in rows or [] if isinstance(r, dict)]
        return self._artifact_cache[key]

    def _already_recorded(self, etype: str, eid: str, name: str, digest: str) -> None:
        """Never re-upload what is already in Probe: the same bytes on the target
        or on any run this session worked on (where the agent's own SDK uploads
        land), or the same name on the target."""
        runs = [rid for rid, kind in self.known_ids.items() if kind in ("run", None) and rid != eid]
        runs += [rid for rid in sorted(self.run_starts) if rid not in runs and rid != eid]
        places = [(etype, eid, True)] + [("run", rid, False) for rid in runs[-MAX_DEDUPE_RUNS:]]
        for place_type, place_id, strict in places:
            for row in self._artifact_rows(place_type, place_id, strict=strict):
                if row.get("content_hash") == digest:
                    raise Held(f"already recorded: these bytes are on {place_type} {place_id}")
                if strict and row.get("name") == name:
                    raise Held("already recorded: an artifact with this name is on the target")

    def _entity(self, etype: str, eid: str) -> dict:
        key = (etype, eid)
        if key not in self._entity_cache:
            row = self._get(f"/v1/{etype}s/{eid}")
            if not isinstance(row, dict):
                raise Held(f"{etype} {eid} not readable")
            self._entity_cache[key] = row
        return self._entity_cache[key]

    def _target(self, raw: dict) -> tuple[str, str]:
        target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
        etype = target.get("type")
        eid = _uuid(target.get("id"))
        if etype not in TARGET_TYPES or eid is None:
            raise Held("no valid target")
        if (etype == "artifact") != (raw.get("kind") == "file_note"):
            raise Held("only a file note targets a file, and a file note only a file")
        if etype == "artifact":
            if eid not in self.files:
                raise Held(f"file {eid} is not one listed under FILES")
            return etype, eid
        if eid not in self.known_ids:
            raise Held(f"target {eid} is not one this session acted on")
        self._entity(etype, eid)  # exists, and this token may read it
        return etype, eid

    def _evidence(self, raw: dict) -> dict:
        ev = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
        try:
            start, end = int(ev.get("from")), int(ev.get("to"))
        except (TypeError, ValueError):
            raise Held("no evidence range") from None
        if end < start or end <= self.range_start or end > self.range_end:
            raise Held("evidence outside the new transcript")
        if any(a < end <= b for a, b in self.agent_ranges):
            raise Held("evidence is in a range the agent owned")
        return {"from": start, "to": end, "why": _str(raw.get("why"), 300)}

    # -- kinds --
    def decide(self, raw: Any) -> dict:
        """`{"kind", "target_type", "target_id", "payload", "evidence", "op"}` or Held."""
        if not isinstance(raw, dict) or raw.get("kind") not in KINDS:
            raise Held("kind not allowed")
        kind = raw["kind"]
        evidence = self._evidence(raw)
        etype, eid = self._target(raw)
        op = getattr(self, f"_{kind}")(raw, etype, eid)
        return {"kind": kind, "target_type": etype, "target_id": eid, "evidence": evidence, **op}

    def _note(self, raw: dict, etype: str, eid: str) -> dict:
        title = _str(raw.get("title"), 120)
        main = raw.get("main") is True and etype == "project"  # a run's notes hold 4,000 chars: a sub-note
        body = _str(raw.get("body"), MAIN_NOTE_BODY_CHARS if main else NOTE_BODY_CHARS)
        if not title or not body:
            raise Held("note needs a title and a body")
        if raw.get("main") is True and etype == "project":
            # The entity's main document, only while it is EMPTY: never replace what
            # someone wrote. Publish re-reads it and falls back to a sub-note if it
            # was filled meanwhile.
            row = self._entity(etype, eid)
            if (isinstance(row.get("notes"), str) and row["notes"].strip()) or int(row.get("notes_version") or 0):
                raise Held("the main document is not empty; write a titled sub-note instead")
            row["notes"], row["notes_version"] = body, 1  # a second main proposal sees it filled
            payload = {"title": title, "body": body}
            return {"payload": payload, "op": ("NOTES", f"/v1/{etype}s/{eid}", payload)}
        full_title = NOTE_TITLE_PREFIX + title
        listing = self._get(f"/v1/{etype}s/{eid}/sub-notes") or {}
        existing = {item.get("title") for item in listing.get("sub_notes", []) if isinstance(item, dict)}
        if full_title in existing:
            raise Held("a sub-note with this title already exists")
        if len(existing) >= int(listing.get("limit_count") or 20):
            raise Held("sub-note limit reached")
        payload = {"title": full_title, "body": body}
        return {"payload": payload, "op": ("POST", f"/v1/{etype}s/{eid}/sub-notes", payload)}

    def _describe(self, raw: dict, etype: str, eid: str) -> dict:
        row = self._entity(etype, eid)
        wanted = {
            "name": _str(raw.get("name"), 200),
            "description": _str(raw.get("description"), 2000),
            "notes": _str(raw.get("notes"), 2000) if etype == "run" else None,
        }
        body: dict[str, Any] = {}
        for key, value in wanted.items():
            if not value:
                continue
            current = row.get(key)
            if key == "name" and isinstance(current, str) and not _name_is_placeholder(current, row):
                continue
            if key != "name" and isinstance(current, str) and current.strip():
                continue  # never overwrite what someone already wrote
            body[key] = value
        if not body:
            raise Held("every proposed field is already filled")
        body["authored_by"] = "agent"
        row.update({k: v for k, v in body.items() if k != "authored_by"})  # later proposals see it
        return {"payload": body, "op": ("DESCRIBE", f"/v1/{etype}s/{eid}", body)}

    def _tag(self, raw: dict, etype: str, eid: str) -> dict:
        tags = raw.get("tags")
        if not isinstance(tags, list):
            raise Held("tags must be a list")
        wanted = [t.strip().lower() for t in tags if isinstance(t, str) and t.strip()][:10]
        current = [t for t in (self._entity(etype, eid).get("tags") or []) if isinstance(t, str)]
        new = [t for t in wanted if t not in current]
        if not new:
            raise Held("tags already present")
        self._entity(etype, eid)["tags"] = current + new  # a second tag proposal adds to this one
        return {"payload": {"add": new}, "op": ("TAG", f"/v1/{etype}s/{eid}", {"add": new})}

    def _paper(self, raw: dict, etype: str, eid: str) -> dict:
        if etype != "project":
            raise Held("a paper is recorded against a project")
        title = _str(raw.get("title"), 300)
        source_url = _str(raw.get("source_url"), 1000)
        if not title or not source_url:
            raise Held("paper needs a title and a source_url")
        if source_url not in self.chunk_text:
            raise Held("the paper's source_url does not appear in the transcript")
        listing = self._get(f"/v1/projects/{eid}/papers") or {}
        items = listing.get("items", listing.get("papers", [])) if isinstance(listing, dict) else listing
        if any(isinstance(p, dict) and p.get("source_url") == source_url for p in items or []):
            raise Held("paper already recorded")
        body: dict[str, Any] = {"title": title, "source_url": source_url}
        if _str(raw.get("summary_md"), 4000):
            body["summary_md"] = _str(raw.get("summary_md"), 4000)
        if isinstance(raw.get("tags"), list):
            body["tags"] = [t for t in raw["tags"] if isinstance(t, str)][:10]
        return {"payload": body, "op": ("POST", f"/v1/projects/{eid}/papers", body)}

    def _edge(self, raw: dict, etype: str, eid: str) -> dict:
        if etype != "run":
            raise Held("lineage here is run to run")
        source = _uuid(raw.get("source_run_id"))
        relation = raw.get("relation")
        if source is None or source not in self.known_ids or source == eid:
            raise Held("edge source is not a run this session acted on")
        if relation not in RUN_RELATIONS:
            raise Held("relation not allowed")
        self._entity("run", source)
        edges = self._get(f"/v1/runs/{eid}/edges") or {}
        rows = edges.get("items", edges.get("edges", [])) if isinstance(edges, dict) else edges
        for edge in rows or []:
            if isinstance(edge, dict) and {edge.get("source_id"), edge.get("target_id")} == {source, eid}:
                raise Held("these runs are already linked")
        # Direction: the NEW run derives from the source, so the edge is new -> source.
        body = {
            "source_type": "run",
            "source_id": eid,
            "target_type": "run",
            "target_id": source,
            "relation": relation,
            "reason": _str(raw.get("reason"), 300) or "inferred from the session transcript",
        }
        return {"payload": body, "op": ("POST", "/v1/edges", body)}

    def _file_note(self, raw: dict, etype: str, eid: str) -> dict:
        notes = _str(raw.get("notes"), 500)
        if not notes:
            raise Held("a file note needs one line of notes")
        entry = self.files[eid]
        current = entry["row"].get("notes")
        if isinstance(current, str) and current.strip():
            raise Held("this file already has notes")
        entry["row"]["notes"] = notes  # a second proposal for the same file sees it
        body = {"notes": notes, "listing": entry["listing"]}
        return {"payload": {"notes": notes}, "op": ("FILE_NOTE", f"/v1/artifacts/{eid}", body)}

    def _run_end(self, raw: dict, etype: str, eid: str) -> dict:
        if etype != "run" or eid not in self.run_starts:
            raise Held("only a run this session opened with `probe run start` is ended here")
        if eid in self.run_ends:
            raise Held("the agent already ended this run")
        status = raw.get("status")
        if status not in ("completed", "failed"):
            raise Held("status must be completed or failed")
        if self._entity("run", eid).get("status") != "running":
            raise Held("run is not running")
        body = {"status": status}
        return {"payload": body, "op": ("PATCH", f"/v1/runs/{eid}", body)}

    def _artifact(self, raw: dict, etype: str, eid: str) -> dict:
        """A file the session PRODUCED, and nothing that could carry a secret.

        Produced means: written by the agent's own edit tools, or named in the
        output of a shell command that did work (not `cat`, `ls`, `grep`...). A
        path that only appears in a file the agent read, or in the model's own
        words, is not evidence. WHERE the file lives does not matter: a session
        started in the home folder that works in `~/trials/x` uploads from there.
        Then: no hidden directory on the way, no credential-shaped name, a text
        file (by content, `_looks_text`) scanned whole by the same scanner capture
        uses, a size limit, and never bytes the session already recorded
        (`_already_recorded`). Any kind of file: the extension decides nothing.
        """
        path_text = _str(raw.get("path"), 1000)
        if not path_text:
            raise Held("artifact needs a path")
        if path_text not in self.touched and not _names_path(self.produced_text, path_text):
            raise Held("the session did not produce this file")
        resolved = self._resolve(path_text)
        if any(part.startswith(".") for part in resolved.parts):
            raise Held("file is in a hidden directory or is a hidden file")
        if _SECRET_NAME_RE.search(_display_path(resolved)):
            raise Held("file is not uploadable")
        stat = resolved.stat()
        if self.started_at is not None and stat.st_mtime < self.started_at - SESSION_START_SLACK_SECONDS:
            # Produced means produced BY THIS SESSION: a path some output printed
            # (a README, a fetched page) does not reach a file from before it began.
            raise Held("the file predates this session")
        size = stat.st_size
        if size == 0:
            raise Held("the file is empty")
        text = _looks_text(resolved)
        if size > (MAX_TEXT_ARTIFACT_BYTES if text else MAX_ARTIFACT_BYTES):
            raise Held("file size outside the daemon's limit")
        if _is_compressed(resolved):
            raise Held("a compressed or archived file: the credential scan cannot read inside it "
                       "(the researcher can upload it with --directed)")
        if _scan_file(resolved, text=text):
            raise Held("file content matched the secret scanner")
        digest = _sha256_file(resolved)
        name = _str(raw.get("name"), 200) or resolved.name
        self._already_recorded(etype, eid, name, digest)
        content_type = mimetypes.guess_type(resolved.name)[0] or "application/octet-stream"
        presign = {"name": name, "content_hash": digest, "size_bytes": size, "content_type": content_type}
        if etype == "run" and _str(raw.get("kind"), 40):
            presign["kind"] = _str(raw.get("kind"), 40)
        if _str(raw.get("notes"), 1000):
            presign["notes"] = _str(raw.get("notes"), 1000)
        payload = {**presign, "path": str(resolved)}
        return {"payload": payload, "op": ("UPLOAD", f"/v1/{etype}s/{eid}/artifacts/uploads", payload)}


#: Leading bytes of containers the credential scan cannot read into.
_COMPRESSED_MAGIC = (
    b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b", b"BZh", b"\xfd7zXZ\x00", b"7z\xbc\xaf\x27\x1c",
    b"\x28\xb5\x2f\xfd", b"Rar!\x1a\x07", b"\x04\x22\x4d\x18",
)


def _is_compressed(path: Path) -> bool:
    """zip (and every zip-based format), gzip, bzip2, xz, 7z, zstd, rar, lz4, tar."""
    with path.open("rb") as handle:
        head = handle.read(512)
    return head.startswith(_COMPRESSED_MAGIC) or head[257:262] == b"ustar"


_PRINTABLE_RUN = re.compile(rb"[\t\x20-\x7e]{8,}")


def _scan_file(path: Path, *, text: bool) -> bool:
    """Every file is scanned, whatever its kind: text as UTF-8, UTF-16 by its byte
    order mark, anything else by its readable runs (a database, a pickle or a
    Latin-1 text file keeps its ASCII in the clear, where the scanner finds it)."""
    data = path.read_bytes()
    if text:
        decoded = data.decode("utf-8", "replace")
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        decoded = data.decode("utf-16", "replace")
    else:
        # A binary: its readable runs, as `strings` finds them (fast on 50 MB of
        # noise, and a key stored in a database or a pickle is one such run), and
        # the runs again with NUL bytes dropped, for UTF-16 text with no BOM.
        runs = _PRINTABLE_RUN.findall(data) + _PRINTABLE_RUN.findall(data.replace(b"\x00", b""))
        decoded = b"\n".join(runs).decode("ascii")
    return bool(secrets.scan(decoded))


def _looks_text(path: Path) -> bool:
    """Text by content: the first TEXT_SNIFF_BYTES hold no NUL and decode as UTF-8
    (a multi-byte character cut at the edge of the sample still counts)."""
    with path.open("rb") as handle:
        head = handle.read(TEXT_SNIFF_BYTES)
    if b"\0" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as exc:
        return len(head) == TEXT_SNIFF_BYTES and exc.start >= len(head) - 3
    return True


def _display_path(path: Path) -> str:
    """`path` relative to the home folder when it is under it, for the name check:
    the home folder's own path is not the file's name."""
    try:
        return str(path.relative_to(Path.home().resolve()))
    except ValueError:
        return str(path)


def _names_path(text: str, path_text: str) -> bool:
    """Is `path_text` named in `text` as a path, not merely as a substring?"""
    if not text:
        return False
    return re.search(r"(?<![\w./-])" + re.escape(path_text) + r"(?![\w/-])", text) is not None


def _name_is_placeholder(name: str, row: dict) -> bool:
    """A name nobody chose: empty, or the slug re-cased."""
    if not name.strip():
        return True
    slug = row.get("slug")
    if not isinstance(slug, str):
        return False
    squash = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())  # noqa: E731
    return squash(name) == squash(slug)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Publishing.
# ---------------------------------------------------------------------------


class AlreadyDone(Exception):
    """Publishing found the fact already recorded; the proposal counts as published."""


def publish(api: api_mod.Api, proposal: ledger_mod.Proposal) -> Any:
    """Write one decided proposal.

    DESCRIBE and TAG are built HERE from a fresh read, not at decide time: the
    server replaces the whole tag list and the whole field, so a body built
    minutes earlier would drop tags or overwrite text someone added since. A
    note or paper being RETRIED is checked for first -- the server may have
    committed it before a 5xx, and the key's replay window does not cover that.
    """
    op = proposal.evidence.get("_op") if isinstance(proposal.evidence, dict) else None
    if not isinstance(op, list) or len(op) != 3:
        raise api_mod.Rejected(0, None, "proposal carries no op")
    method, path, body = op
    if method == "TAG":
        row = api.get(path) or {}
        current = [t for t in (row.get("tags") or []) if isinstance(t, str)]
        new = [t for t in body.get("add", []) if t not in current]
        if not new:
            raise AlreadyDone("tags already present")
        return api.request("PATCH", path, {"tags": current + new}, idem_key=proposal.idem_key)
    if method == "DESCRIBE":
        row = api.get(path) or {}
        patch = {
            key: value
            for key, value in body.items()
            if key == "authored_by"
            or not (isinstance(row.get(key), str) and row.get(key).strip())
            or (key == "name" and _name_is_placeholder(row.get(key) or "", row))
        }
        if set(patch) <= {"authored_by"}:
            raise AlreadyDone("every field was filled in meanwhile")
        return api.request("PATCH", path, patch, idem_key=proposal.idem_key)
    if method == "NOTES":
        # The main document, still only while it is empty: a fresh read decides.
        row = api.get(path) or {}
        filled = (isinstance(row.get("notes"), str) and row["notes"].strip()) or int(row.get("notes_version") or 0)
        if filled and isinstance(row.get("notes"), str) and row["notes"].strip() == body["body"].strip():
            raise AlreadyDone("the main document is this proposal's own (a lost response, retried)")
        if not filled:
            return api.request("PATCH", path, {"notes": body["body"], "base_version": 0,
                                               "op_key": proposal.idem_key}, idem_key=proposal.idem_key)
        listing = api.get(path + "/sub-notes") or {}
        title = NOTE_TITLE_PREFIX + body["title"]
        if title in {i.get("title") for i in listing.get("sub_notes", []) if isinstance(i, dict)}:
            raise AlreadyDone("the document was filled meanwhile and the sub-note exists")
        return api.request("POST", path + "/sub-notes", {"title": title, "body": body["body"]},
                           idem_key=proposal.idem_key + "-sub")
    if method == "FILE_NOTE":
        listing = api.get(body["listing"]) or []
        rows = listing.get("items", listing.get("artifacts", [])) if isinstance(listing, dict) else listing
        aid = path.rsplit("/", 1)[-1]
        row = next((r for r in rows or [] if isinstance(r, dict) and r.get("id") == aid), None)
        if row is None:
            raise api_mod.Rejected(404, None, "the file is gone")
        if isinstance(row.get("notes"), str) and row["notes"].strip():
            raise AlreadyDone("the file's notes were written meanwhile")
        patch = {"notes": body["notes"], "base_version": int(row.get("notes_version") or 0),
                 "op_key": proposal.idem_key}
        try:
            return api.request("PATCH", path, patch, idem_key=proposal.idem_key)
        except api_mod.Rejected as exc:
            if exc.status == 409:  # someone wrote the notes between the read and the write
                raise AlreadyDone("the file's notes were written meanwhile") from None
            raise
    if method == "POST" and proposal.attempts and proposal.kind in ("note", "paper"):
        listing = api.get(path) or {}
        if proposal.kind == "note":
            titles = {i.get("title") for i in listing.get("sub_notes", []) if isinstance(i, dict)}
            if body.get("title") in titles:
                raise AlreadyDone("the note landed on an earlier attempt")
        else:
            items = listing.get("items", listing.get("papers", [])) if isinstance(listing, dict) else listing
            if any(isinstance(i, dict) and i.get("source_url") == body.get("source_url") for i in items or []):
                raise AlreadyDone("the paper landed on an earlier attempt")
    if method != "UPLOAD":
        return api.request(method, path, body, idem_key=proposal.idem_key)
    return _upload(api, proposal, path, body)


def _upload(api: api_mod.Api, proposal: ledger_mod.Proposal, path: str, body: dict) -> Any:
    """presign -> PUT -> confirm, from a PRIVATE COPY of the file, so the bytes
    that upload are exactly the bytes that were hashed and scanned."""
    import shutil
    import tempfile

    local = Path(body["path"])
    with tempfile.TemporaryDirectory(prefix="probe-companion-") as tmp:
        copy = Path(tmp) / "artifact"
        try:
            shutil.copyfile(local, copy)
        except OSError:
            raise api_mod.Rejected(0, None, "file is gone") from None
        if _sha256_file(copy) != body["content_hash"]:
            raise api_mod.Rejected(0, None, "file changed since it was decided")
        request = {k: v for k, v in body.items() if k != "path"}
        presigned = api.request("POST", path, request, idem_key=proposal.idem_key)
        if not isinstance(presigned, dict) or not presigned.get("artifact_id"):
            raise api_mod.Retryable(0, presigned, "presign answered without an artifact id")
        if not presigned.get("have"):
            api.put_file(
                presigned["upload_url"],
                copy,
                content_type=request["content_type"],
                headers=presigned.get("upload_headers") or presigned.get("headers"),
            )
        return api.request(
            "POST",
            f"/v1/artifacts/{presigned['artifact_id']}/confirm",
            None,
            idem_key=proposal.idem_key + "-confirm",
        )


def publish_backoff(attempts: int) -> float:
    """Seconds before a failed publish is tried again: 30s doubling, 30 min cap."""
    return 0.0 if attempts <= 0 else min(30.0 * 2 ** (attempts - 1), 1800.0)


# ---------------------------------------------------------------------------
# The worker.
# ---------------------------------------------------------------------------


def shadow_enabled() -> bool:
    if os.environ.get(ENV_SHADOW, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return cfg._read_probe_config().get("companion_shadow") is True


class GatewayUnusable(api_mod.ApiError):
    """The gateway cannot serve this worker at all (route missing, request shape
    refused): hand writing back now, rather than skip ranges while holding it."""


class UnusableAnswer(api_mod.Retryable):
    """The gateway answered, but not with the proposals JSON asked for."""


#: publish_pending's three outcomes.
PUBLISH_OK = "ok"  # something landed, or nothing was waiting
PUBLISH_FAILING = "failing"  # every write tried this pass failed transiently
PUBLISH_WAITING = "waiting"  # writes are waiting out their backoff


class Worker:
    """One session's daemon. See the module docstring for a cycle.

    THE LEASE is held only while the worker can record, and is CHECKED against
    its expiry before every network call and every write, not assumed from
    `self.live`; every request is bounded by what is left of it. Two things are
    never allowed: both sides writing (a write after the lease lapsed) and
    neither side writing (holding the lease while not recording).

    WHO OWNED WHICH BYTES. The ledger keeps `agent_ranges`, the transcript
    ranges the agent owned: everything before the first take, and every span
    between a handback and the next take. The worker reads the transcript in
    order and never lets a chunk cross into an agent range; it jumps over each
    one (scanning it only for ids and run starts, without a model call). What it
    had not decided when it handed back for a FAILURE is still its own (the
    agent was refused there), so `owed_until` marks where that debt ends and the
    agent's range begins. A switch the researcher MOVES carries no debt.
    """

    def __init__(self, *, session_id: str, transcript: Path, cwd: Path, source: str) -> None:
        self.session_id = session_id
        self.transcript = transcript
        self.cwd = cwd
        self.source = source
        self.ledger = ledger_mod.Ledger(session_id)
        self.spend = ledger_mod.DeviceSpend()
        self.api: api_mod.Api | None = None
        self.live = False  # holds the lease right now
        self.lease_expires_at = 0.0  # wall clock: survives a laptop sleeping
        self.released_reason: str | None = None
        self.last_growth = time.monotonic()
        self.last_size = -1
        self.failed_cycles = 0
        self.skipped_cycles = 0
        self.unusable: tuple[int, int] = (-1, 0)  # (range start, answers in a row)
        self.blocked_token: str | None = None
        self.cooldown_until = 0.0
        self.last_renew = 0.0
        self.parent_pid = os.getppid()

    # -- lease and handover --
    def _transcript_size(self) -> int:
        try:
            return self.transcript.stat().st_size
        except OSError:
            return 0

    def _renew(self) -> bool:
        now = time.time()
        if not lease.renew(self.session_id, now=now):
            # The file did not land: nobody else sees a live lease, so this
            # worker must not act as if it held one.
            log.warning("could not write the lease; handing back")
            self.live = False
            self.lease_expires_at = 0.0
            return False
        self.lease_expires_at = now + lease.LEASE_TTL_SECONDS
        self.last_renew = time.monotonic()
        if self.api is not None:
            self.api.deadline = self.lease_expires_at - LEASE_MARGIN_SECONDS / 2
        return True

    def lease_left(self) -> float:
        """Seconds of lease left, 0 when not held (wall clock)."""
        return max(0.0, self.lease_expires_at - time.time()) if self.live else 0.0

    def lease_ok(self) -> bool:
        """May a network call or a write start now?"""
        if not self.live:
            return False
        if self.lease_left() <= LEASE_MARGIN_SECONDS:
            self._lapsed()
            return False
        return True

    def _lapsed(self) -> None:
        """The lease ran out under us (a stall, a sleep): the agent was told
        recording came back to it, so this is a handback, not a hiccup."""
        log.warning("the write lease lapsed; handing back")
        self.release(lease.REASON_GATEWAY, "lapsed", debt=True)

    def _agent_ranges(self) -> list[list[int]]:
        return [list(r) for r in self.ledger.get_json("agent_ranges", [])]

    def _absorb(self, start: int, end: int) -> None:
        """Remember the ids, run starts and files in a range the agent owned
        (no model call): the daemon may decorate what the agent made there."""
        start = max(start, end - ABSORB_BYTES)
        offset = start
        run_starts = set(self.ledger.get_json("run_starts", []))
        run_ends = set(self.ledger.get_json("run_ends", []))
        touched = list(self.ledger.get_json("touched", []))
        workdirs = list(self.ledger.get_json("workdirs", []))
        while offset < end:
            lines, new = observe.read_chunk(self.transcript, offset, max_bytes=min(CHUNK_BYTES, end - offset))
            if new <= offset:
                break
            seen = observe.observe(self.source, [(o, raw) for o, raw in lines if o <= end])
            with self.ledger.transaction():
                self.ledger.remember_ids(seen.ids)
            run_starts |= seen.run_starts
            run_ends |= seen.run_ends
            touched = _merge_touched(touched, seen.touched_files)
            workdirs = _merge_workdirs(workdirs, seen.workdirs)
            offset = new
        with self.ledger.transaction():
            self.ledger.set_json("run_starts", sorted(run_starts))
            self.ledger.set_json("run_ends", sorted(run_ends))
            self.ledger.set_json("touched", touched)
            self.ledger.set_json("workdirs", workdirs)

    def take_lease(self) -> None:
        """agent -> daemon. The daemon never authors from a range the agent owned."""
        if self.live:
            return
        offset = self._transcript_size()
        if self.ledger.writer != "daemon":
            ranges = self._agent_ranges()
            watermark = self.ledger.watermark
            owed = self.ledger.get_json("owed_until")
            if self.ledger.boundary is None:
                agent_from = 0  # first take: the agent owned everything so far
            elif owed is not None and watermark < int(owed):
                agent_from = int(owed)  # the debt [watermark, owed) stays the daemon's
            else:
                agent_from = watermark
            if offset > agent_from:
                ranges.append([agent_from, offset])
                self._absorb(agent_from, offset)
            with self.ledger.transaction():
                self.ledger.set_json("agent_ranges", ranges[-200:])
                self.ledger.set_json("owed_until", None)
                if self.ledger.boundary is None or watermark >= agent_from:
                    self.ledger.set_watermark(max(offset, watermark))
                self.ledger.set_json("undecided_since", None)
                self.ledger.set_json("context_shown", False)
            self.ledger.record_boundary(byte_offset=offset, from_writer="agent", to_writer="daemon",
                                        reason="daemon")
        self.live = True
        if not self._renew():
            return
        self.released_reason = None
        log.info("took the write lease at offset %s", self.ledger.watermark)

    def release(self, reason: str, boundary_reason: str, *, debt: bool = False) -> None:
        lease.release(self.session_id, reason)
        if self.live or self.ledger.writer == "daemon":
            size = self._transcript_size()
            if debt and size > self.ledger.watermark:
                self.ledger.set_json("owed_until", size)
            self.ledger.record_boundary(byte_offset=self.ledger.watermark, from_writer="daemon",
                                        to_writer="agent", reason=boundary_reason)
        self.live = False
        self.lease_expires_at = 0.0
        if self.api is not None:
            self.api.deadline = None
        self.released_reason = reason
        log.info("released the write lease: %s", reason)

    def ensure_api(self) -> bool:
        token = api_mod.companion_token()
        if not token:
            self.api = None
            return False
        try:
            base = cfg.api_base_url()
        except cfg.APIBaseURLUnset:
            self.api = None
            return False
        if self.api is None or self.api.token != token:
            self.api = api_mod.Api(base, token)
        return True

    # -- one cycle --
    def _next_agent_range(self, offset: int) -> list[int] | None:
        """The first agent range that ends after `offset`, if any."""
        for a, b in sorted(self._agent_ranges()):
            if b > offset:
                return [a, b]
        return None

    def _skip_agent_range(self) -> None:
        """If the watermark sits at (or in) an agent range, jump past it."""
        nxt = self._next_agent_range(self.ledger.watermark)
        if nxt is not None and nxt[0] <= self.ledger.watermark < nxt[1]:
            self.ledger.set_watermark(nxt[1])

    def due(self, final: bool = False) -> bool:
        size = self._transcript_size()
        if size != self.last_size:
            self.last_size = size
            self.last_growth = time.monotonic()
        # A transcript SHORTER than the watermark was rewritten: re-read it (the
        # idempotency keys stop it re-writing anything).
        pending = size != self.ledger.watermark
        if not pending:
            return False
        if final:
            return True
        turn = self._turn_offset()
        if turn is not None and turn > self.ledger.watermark:
            return True  # the agent finished a turn: read up to it now, not after the quiet
        quiet = time.monotonic() - self.last_growth >= QUIET_SECONDS
        waited = self.ledger.get_json("undecided_since")
        if waited is None:
            self.ledger.set_json("undecided_since", time.time())
            waited = time.time()
        return quiet or time.time() - float(waited) >= MAX_WAIT_SECONDS

    def cycle(self, *, shadow: bool, wall: float = CYCLE_WALL_SECONDS) -> str:
        assert self.api is not None
        deadline = time.monotonic() + wall
        self._skip_agent_range()
        start = self.ledger.watermark
        nxt = self._next_agent_range(start)
        budget = CHUNK_BYTES if nxt is None else max(1, min(CHUNK_BYTES, nxt[0] - start))
        lines, end = observe.read_chunk(self.transcript, start, max_bytes=budget)
        if end == start:
            return "empty"
        lines, end = _fit_to_budget(self.source, lines, end, RENDER_BUDGET_CHARS)
        seen = observe.observe(self.source, lines, open_calls=self.ledger.get_json("open_calls", {}))
        with self.ledger.transaction():
            self.ledger.remember_ids(seen.ids)
            self.ledger.record_directed(seen.directed)
        run_starts = set(self.ledger.get_json("run_starts", [])) | seen.run_starts
        run_ends = set(self.ledger.get_json("run_ends", [])) | seen.run_ends
        touched = _merge_touched(self.ledger.get_json("touched", []), seen.touched_files)
        workdirs = _merge_workdirs(self.ledger.get_json("workdirs", []), seen.workdirs)
        self.ledger.set_json("workdirs", workdirs)
        chunk_text = observe.render(seen.events)
        if not chunk_text.strip():
            with self.ledger.transaction():
                self._commit_watermark(end, run_starts, run_ends, touched, seen.open_calls)
            return "nothing to decide"
        if self._skipped(start, end) and not _deterministic(seen):
            with self.ledger.transaction():
                cycle_id = self.ledger.start_cycle(byte_start=start, byte_end=end, events=len(seen.events))
                self.ledger.finish_cycle(cycle_id, outcome=JUDGE_SKIPPED_OUTCOME)
                self._commit_watermark(end, run_starts, run_ends, touched, seen.open_calls)
            return "skipped by the judge"
        if not _worth_a_call(seen):
            # Reads and listings only: no words from the agent, no run started or
            # ended, nothing directed, written or produced. The ledger above kept the
            # ids; the conclusions pass still sees this range. Recorded, not hidden.
            with self.ledger.transaction():
                cycle_id = self.ledger.start_cycle(byte_start=start, byte_end=end, events=len(seen.events))
                self.ledger.finish_cycle(cycle_id, outcome=SKIPPED_OUTCOME)
                self._commit_watermark(end, run_starts, run_ends, touched, seen.open_calls)
            return "skipped"
        if self.spend.exhausted():
            raise api_mod.Budget(429, None, "device daily token ceiling reached")

        prior = ""
        if not self.ledger.get_json("context_shown", False) and start > 0:
            ctx_lines, _ = observe.read_chunk(self.transcript, max(0, start - CONTEXT_BYTES), max_bytes=CONTEXT_BYTES)
            ctx_lines = [(off, raw) for off, raw in ctx_lines if off <= start]
            prior = observe.render(observe.observe(self.source, ctx_lines).events, whole=False)[-CONTEXT_BYTES:]
        feedback = self.ledger.unconsumed_feedback()
        facts = dict(known_ids=self.ledger.seen_ids(), decided=self.ledger.recent_decisions(), feedback=feedback,
                     run_starts=sorted(run_starts), cwd=str(self.cwd))
        messages = build_messages(chunk_text=chunk_text, prior_context=prior, **facts)
        over = sum(len(m["content"]) for m in messages) - REQUEST_CHARS_LIMIT
        if over > 0 and prior:
            # The earlier context is context only: it gives way before the chunk.
            prior = prior[over + 200 :]
            messages = build_messages(chunk_text=chunk_text, prior_context=prior, **facts)
        cycle_id = self.ledger.start_cycle(byte_start=start, byte_end=end, events=len(seen.events))
        try:
            answer = self._ask(messages, deadline, shadow=shadow, cycle_id=cycle_id, upto=end)
            proposals = _parse_proposals(answer)
        except UnusableAnswer as exc:
            streak = self.unusable[1] + 1 if self.unusable[0] == start else 1
            self.unusable = (start, streak)
            if streak >= MAX_UNUSABLE_ANSWERS:
                # The model cannot answer this range: skip it, on the record,
                # rather than hand the lease back and forth over it forever.
                with self.ledger.transaction():
                    self._commit_watermark(end, run_starts, run_ends, touched, seen.open_calls)
                    self.ledger.finish_cycle(cycle_id, outcome=f"skipped: {exc}")
                self.skipped_cycles += 1
                return "skipped"
            self.ledger.finish_cycle(cycle_id, outcome=f"gateway: {exc}")
            raise
        except api_mod.Rejected as exc:
            if exc.status in (413, 422):
                # The gateway refused THIS prompt (too large, rejected by the
                # provider). Retrying the same range would wedge on it, so it is
                # skipped -- on the record -- and repeated skips hand back.
                with self.ledger.transaction():
                    self._commit_watermark(end, run_starts, run_ends, touched, seen.open_calls)
                    self.ledger.finish_cycle(cycle_id, outcome=f"skipped: gateway refused ({exc.status})")
                self.skipped_cycles += 1
                return "skipped"
            self.ledger.finish_cycle(cycle_id, outcome=f"gateway unusable ({exc.status})")
            raise GatewayUnusable(exc.status, exc.detail, f"gateway unusable: {exc}") from None
        except api_mod.ApiError as exc:
            # The watermark stays put, so the same range is retried next cycle.
            self.ledger.finish_cycle(cycle_id, outcome=f"gateway: {type(exc).__name__}")
            raise
        self.skipped_cycles = 0
        self.unusable = (-1, 0)
        usage = answer.get("usage") or {}
        tokens_in, tokens_out = int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)

        decider = Decider(
            self.api,
            known_ids=self.ledger.seen_ids(),
            run_starts=run_starts,
            run_ends=run_ends,
            cwd=self.cwd,
            touched=set(touched),
            chunk_text=chunk_text,
            range_start=start,
            range_end=end,
            agent_ranges=self._agent_ranges(),
            produced_text=seen.produced_text,
            workdirs=workdirs,
            started_at=self._session_started_at(),
        )
        # EVERY proposal gets a decision on the record: deciding is a few reads,
        # and a proposal dropped here would be dropped silently while the lease
        # tells the agent not to record it.
        decisions: list[dict] = []
        for index, raw in enumerate(proposals):
            if index >= MAX_PROPOSALS_PER_CYCLE:
                decisions.append({"raw": raw, "held": "over the per-cycle cap"})
                continue
            try:
                decisions.append(decider.decide(raw))
            except Held as held:
                decisions.append({"raw": raw, "held": str(held)})

        self._persist(cycle_id, decisions, shadow=shadow, tokens=(tokens_in, tokens_out),
                      feedback_ids=[f["feedback_id"] for f in feedback],
                      watermark=(end, run_starts, run_ends, touched, seen.open_calls))
        return f"{len(decisions)} decided"

    def _persist(self, cycle_id: int, decisions: list[dict], *, shadow: bool, tokens: tuple[int, int],
                 feedback_ids: list[int], watermark: tuple | None, conclusions: bool = False) -> None:
        """Every decision, the watermark (for a transcript cycle) and the cycle, in one transaction."""
        committed = self.ledger.committed_writes(exclude_kind="file_note")
        file_notes = self.ledger.committed_writes(kind="file_note")
        with self.ledger.transaction():
            for item in decisions:
                if "held" in item:
                    raw = item["raw"] if isinstance(item["raw"], dict) else {"raw": item["raw"]}
                    kind = raw.get("kind") if isinstance(raw.get("kind"), str) else "unknown"
                    target = raw.get("target") if isinstance(raw.get("target"), dict) else {}
                    self.ledger.add_proposal(
                        idem_key=idem_key(self.session_id, kind, json.dumps(target, sort_keys=True), raw),
                        cycle=cycle_id, kind=kind, target_type=target.get("type"),
                        target_id=target.get("id"), payload=raw, evidence={}, status=ledger_mod.STATUS_HELD,
                        reason=item["held"],
                    )
                    continue
                target = f"{item['target_type']}:{item['target_id']}"
                status, reason = ledger_mod.STATUS_PENDING, None
                if shadow:
                    status, reason = ledger_mod.STATUS_HELD, "shadow"
                elif item["kind"] == "file_note" and file_notes >= MAX_FILE_NOTES_PER_SESSION:
                    status, reason = ledger_mod.STATUS_HELD, "session file-note cap reached"
                elif item["kind"] != "file_note" and committed >= MAX_WRITES_PER_SESSION:
                    status, reason = ledger_mod.STATUS_HELD, "session write cap reached"
                added = self.ledger.add_proposal(
                    idem_key=idem_key(self.session_id, item["kind"], target, item["payload"]),
                    cycle=cycle_id, kind=item["kind"], target_type=item["target_type"],
                    target_id=item["target_id"], payload=item["payload"],
                    evidence={**item["evidence"], "_op": list(item["op"])},
                    status=status, reason=reason,
                )
                if added and status == ledger_mod.STATUS_PENDING:
                    if item["kind"] == "file_note":
                        file_notes += 1
                    else:
                        committed += 1
            if watermark is not None:
                self._commit_watermark(*watermark)
                self.ledger.set_json("context_shown", True)
            self.ledger.consume_feedback(feedback_ids)
            # A conclusions cycle spans the whole session (byte 0 to the end) by design;
            # the label keeps it apart from the transcript cycles in the ledger.
            outcome = ("conclusions: " if conclusions else "") + f"{len(decisions)} decided"
            self.ledger.finish_cycle(cycle_id, outcome=outcome, input_tokens=tokens[0], output_tokens=tokens[1])

    # -- the conclusions pass --
    def _turn_offset(self) -> int | None:
        """Where the agent's last finished turn ended (the Stop hook's `<sid>.turn`)."""
        try:
            data = json.loads((lease.sessions_dir() / (self.session_id + TURN_SUFFIX)).read_text("utf-8"))
        except (OSError, ValueError):
            return None
        value = data.get("offset") if isinstance(data, dict) else None
        return int(value) if isinstance(value, (int, float)) and value > 0 else None

    def conclusions_due(self) -> bool:
        """A finished turn is fully read and has not been concluded yet."""
        turn = self._turn_offset()
        if turn is None:
            return False
        done = int(self.ledger.get_json("conclusions_through", 0) or 0)
        if not (done < turn and self.ledger.watermark >= min(turn, self._transcript_size())):
            return False
        last = float(self.ledger.get_json("conclusions_at", 0) or 0)
        return time.time() - last >= CONCLUSIONS_MIN_GAP_SECONDS

    def decide_due(self, *, shadow: bool) -> str | None:
        """One poll's deciding: a due cycle (at once when a turn just finished),
        then the conclusions pass once that turn is fully read."""
        outcome = None
        self._judge_finished_turn(time.monotonic() + CYCLE_WALL_SECONDS)
        if self.due():
            outcome = self.cycle(shadow=shadow)
        if self.conclusions_due():
            outcome = (f"{outcome}; " if outcome else "") + "conclusions: " + self.conclude(shadow=shadow)
        return outcome

    def _judge_on(self) -> bool:
        if os.environ.get(ENV_JUDGE, "").strip().lower() in ("0", "off", "false", "no"):
            return False
        return not self.ledger.get_json("judge_off") and callable(getattr(self.api, "judge", None))

    def _judge_skips(self) -> bool:
        """SKIP mode: asked for (`PROBE_COMPANION_JUDGE=skip`) AND every kind proven."""
        if os.environ.get(ENV_JUDGE, "").strip().lower() != "skip" or not self._judge_on():
            return False
        proven = {k.strip() for k in os.environ.get(ENV_JUDGE_SKIP_KINDS, "").split(",") if k.strip()}
        return set(JUDGE_SKIP_KINDS) <= proven

    def _gate_turn(self, start: int, end: int, deadline: float) -> dict[str, float] | None:
        """The judge's per-kind answers about one finished turn (every slice asked;
        a kind's answer is its highest over the slices, "nothing" its lowest)."""
        events = self._turn_events(start, end)
        self.ledger.set_json("gated_through", end)
        if not events:
            return None
        text = "\n\n".join(t if role != observe.ASSISTANT else f"[assistant @{off}]\n{t}" for off, role, t in events)
        merged: dict[str, float] = {}
        for calls, at in enumerate(range(0, len(text), JUDGE_SLICE_CHARS)):
            if calls >= JUDGE_CALLS_PER_TURN or deadline - time.monotonic() < JUDGE_RESERVE_SECONDS:
                return None  # a turn not judged whole is never skipped
            answers = self._judge("gate", start, end, text[at : at + JUDGE_SLICE_CHARS], JUDGE_KIND_QUESTIONS)
            if not answers:
                return None
            for kind, p in answers.items():
                if isinstance(p, (int, float)):
                    merged[kind] = min(merged.get(kind, 1.0), p) if kind == "nothing" else max(merged.get(kind, 0.0), p)
        return merged or None

    def _skipped(self, start: int, end: int) -> bool:
        """Is (start, end] inside ONE turn the judge found empty (SKIP on)?"""
        if not self._judge_skips():
            return False
        return any(lo <= start and end <= hi for lo, hi in self.ledger.get_json("skip_ranges", []))

    def _holdout(self, turn: int) -> bool:
        digest = hashlib.sha256(f"{self.session_id}:{turn}".encode()).digest()
        return digest[0] % JUDGE_HOLDOUT_EVERY == 0

    def _judge_finished_turn(self, deadline: float) -> None:
        """At a turn's end: record the judge's answers about it (SHADOW), and in SKIP
        mode, when the turn holds nothing to record, mark it for skipping."""
        turn = self._turn_offset()
        gated = int(self.ledger.get_json("gated_through", 0) or 0)
        if turn is not None and turn < gated:
            # The transcript was rewritten (a shorter one restarts at 0): old marks mean nothing.
            gated = 0
            self.ledger.set_json("gated_through", 0)
            self.ledger.set_json("skip_ranges", [])
        if turn is None or turn <= gated or not self._judge_on():
            return
        verdict = self._gate_turn(gated, turn, deadline)
        if not (verdict and self._judge_skips()):
            return
        nothing = verdict.get("nothing", 0.0) >= JUDGE_SKIP_NOTHING_P
        quiet = all(verdict.get(k, 1.0) < JUDGE_SKIP_KIND_P for k in JUDGE_SKIP_KINDS)
        if not (nothing and quiet):
            return
        holdout = self._holdout(turn)
        self.ledger.add_judgment(purpose="holdout" if holdout else "skip", byte_start=gated, byte_end=turn,
                                 questions={}, answers=verdict)
        if not holdout:
            # This turn's range only: an earlier turn not yet read is never swept in.
            ranges = self.ledger.get_json("skip_ranges", [])
            self.ledger.set_json("skip_ranges", ranges[-199:] + [[gated, turn]])

    def _judge(self, purpose: str, start: int, end: int, text: str, questions: dict[str, str],
               notes: list[str] | tuple = (), timeout: float = 30) -> dict[str, float] | None:
        """One judge call, recorded whatever happens; the answers, or None."""
        assert self.api is not None
        if self.ledger.get_json("judge_off"):
            return None  # refused earlier in this session, maybe earlier in this turn
        record = {"purpose": purpose, "byte_start": start, "byte_end": end, "questions": questions}
        notes = [n[:JUDGE_NOTE_CHARS] for n in list(notes)[:JUDGE_NOTES]]
        room = _judge_budget(len(questions)) - sum(len(q) for q in questions.values()) - sum(map(len, notes)) - 200
        try:
            out = self.api.judge(text[-max(0, room):], questions, notes=notes, timeout=timeout)
        except (api_mod.Rejected, api_mod.Forbidden) as exc:
            # Not enabled for this workspace, or a server without the route: stop asking.
            self.ledger.set_json("judge_off", f"{type(exc).__name__}: {exc}")
            self.ledger.add_judgment(**record, error=f"{type(exc).__name__}: {exc}")
            return None
        except api_mod.ApiError as exc:
            self.ledger.add_judgment(**record, error=f"{type(exc).__name__}: {exc}")
            return None
        answers = out.get("answers") if isinstance(out.get("answers"), dict) else {}
        error = out.get("error") if isinstance(out.get("error"), str) else None
        self.ledger.add_judgment(**record, answers=answers or None, model=out.get("model"), error=error,
                                 elapsed_ms=out.get("elapsed_ms") if isinstance(out.get("elapsed_ms"), int) else None)
        return answers if answers and not error else None

    def _turn_events(self, start: int, size: int) -> list[tuple[int, str, str]]:
        """`(offset, role, rendered)` for the daemon-owned events of one turn."""
        agent = self._agent_ranges()
        out: list[tuple[int, str, str]] = []
        offset, carried = start, {}
        while offset < size:
            lines, new = observe.read_chunk(self.transcript, offset, max_bytes=CHUNK_BYTES)
            if new <= offset:
                break
            seen = observe.observe(self.source, [(o, raw) for o, raw in lines if o <= size], open_calls=carried)
            carried = seen.open_calls
            for event, index in observe.indexed(seen.events):
                if any(a < event.offset <= b for a, b in agent):
                    continue
                text = observe.render_event(event, index=index)
                if text:
                    out.append((event.offset, event.role, text if event.role != observe.ASSISTANT else event.text))
            offset = new
        return out

    def _judge_targets(self, start: int, size: int, deadline: float, notes: list[str],
                       max_calls: int = JUDGE_CALLS_PER_TURN, reserve: float = JUDGE_RESERVE_SECONDS,
                       newest_first: bool = False) -> list[dict]:
        """The agent's paragraphs in [start, size) that none of `notes` (what the
        session's entities already say) records: the conclusions pass's targets."""
        events = self._turn_events(start, size)
        calls = 0
        paragraphs = [
            (off, para.strip())
            for off, role, t in events if role == observe.ASSISTANT
            for para in re.split(r"\n\s*\n", t) if len(para.strip()) >= JUDGE_MIN_PARAGRAPH
        ]
        if newest_first:
            paragraphs.reverse()  # a long session's latest results are the likeliest missed
        targets: list[dict] = []
        for at in range(0, len(paragraphs), JUDGE_QUESTIONS_PER_CALL):
            left = deadline - time.monotonic() - reserve
            if calls >= max_calls or left < 5:
                break
            calls += 1
            batch = paragraphs[at : at + JUDGE_QUESTIONS_PER_CALL]
            ids = [_judge_id(i) for i in range(len(batch))]
            questions = {
                qid: ("The paragraph starting \"" + " ".join(para[:220].split()) + "\" states a result, "
                      "decision, chosen setting or caveat that none of the listed notes records.")
                for qid, (_, para) in zip(ids, batch, strict=True)
            }
            # The state is the batch's own paragraphs, so every paragraph asked about
            # is in the text judged, however long the turn.
            state = "\n\n".join(f"[assistant @{off}]\n{para[:1500]}" for off, para in batch)
            answers = self._judge("targets", start, size, state, questions, notes, timeout=min(30.0, left)) or {}
            targets += [{"offset": off, "paragraph": para[:1500], "p": round(float(answers[qid]), 3)}
                        for qid, (off, para) in zip(ids, batch, strict=True)
                        if isinstance(answers.get(qid), (int, float)) and answers[qid] >= JUDGE_TARGET_P]
        return targets

    def _read_quiet(self, path: str) -> Any:
        assert self.api is not None
        try:
            return self.api.get(path)
        except (api_mod.Rejected, api_mod.Forbidden, api_mod.Retryable):
            return None

    def _session_entities(self, mentioned: set[str] | frozenset = frozenset()) -> SessionEntities:
        """ENTITIES (projects/experiments and runs the session acted on, with what is
        still empty), FILES (result files on those runs whose notes are empty), and the
        runs ADMITTED from `mentioned`: a run the session's own work output named (a
        sweep script printing each run's URL) is the session's when it sits in a
        project or experiment the session acted on; any other named run is ignored."""
        entities: list[dict] = []
        files: dict[str, dict] = {}
        shown: list[dict] = []
        admitted: dict[str, str] = {}
        seen = self.ledger.seen_ids()
        contexts = self.ledger.seen_contexts()
        # Projects a mentioned run may be admitted from: ones the session named in a
        # `probe` command it ran (or launched a run into), never ones it only read
        # through a Probe tool.
        projects: set[str] = set()
        note_lines: list[str] = []  # what the entities' notes already say, for the judge

        def add_run(eid: str, row: dict) -> None:
            entities.append({
                "id": eid, "type": "run", "name": row.get("name"), "project_id": row.get("project_id"),
                "status": row.get("status"), "tags": row.get("tags") or [],
                "parent_run_id": row.get("parent_run_id"),
            })
            listing = self._read_quiet(f"/v1/runs/{eid}/artifacts") or []
            rows = listing.get("items", listing.get("artifacts", [])) if isinstance(listing, dict) else listing
            for art in rows or []:
                if not isinstance(art, dict) or not art.get("id") or art.get("kind") in ("code", "code_snapshot"):
                    continue
                if isinstance(art.get("notes"), str) and art["notes"].strip():
                    continue
                files[art["id"]] = {"listing": f"/v1/runs/{eid}/artifacts", "row": art}
                if len(shown) < MAX_FILES_SHOWN:
                    shown.append({"id": art["id"], "name": art.get("name"), "kind": art.get("kind"),
                                  "run": row.get("name"), "run_id": eid})

        # Every project and experiment the session named (few), then the newest other
        # ids: a sweep printing hundreds of run ids must not push the session's own
        # project out of the window.
        ordered = [(e, t) for e, t in seen.items() if t == "project"]
        ordered += [(e, t) for e, t in list(seen.items()) if t != "project"][-KNOWN_IDS_SHOWN:]
        for eid, etype in ordered:
            if etype in ("artifact", "paper"):
                continue
            row = self._read_quiet(f"/v1/projects/{eid}") if etype != "run" else None
            if isinstance(row, dict) and row.get("id"):
                if not observe.from_probe_tool(contexts.get(eid)):
                    projects.add(eid)
                entities.append({
                    "id": eid, "type": "project",
                    "kind": row.get("kind"), "name": row.get("name"),
                    "parent_project_id": row.get("parent_project_id"),
                    "description": (row.get("description") or "")[:300] or "(empty)",
                    "main_document": "(empty)" if not (row.get("notes") or "").strip() else "(written)",
                })
                if (row.get("notes") or "").strip():
                    note_lines.append(f"{row.get('name')}: {row['notes'].strip()[:400]}")
                listing = self._read_quiet(f"/v1/projects/{eid}/sub-notes") or {}
                for sub in listing.get("sub_notes", []) if isinstance(listing, dict) else []:
                    if isinstance(sub, dict) and sub.get("title"):
                        note_lines.append(f"{row.get('name')}: {sub['title']}")  # listings carry no body
                continue
            row = self._read_quiet(f"/v1/runs/{eid}") if etype in ("run", None) else None
            if isinstance(row, dict) and row.get("id"):
                if row.get("project_id") and not observe.from_probe_tool(contexts.get(eid)):
                    projects.add(row["project_id"])
                if (row.get("notes") or "").strip():
                    note_lines.append(f"{row.get('name')}: {row['notes'].strip()[:400]}")
                add_run(eid, row)
        for rid in sorted(set(mentioned) - set(seen))[:MAX_ADMITTED_RUNS]:
            row = self._read_quiet(f"/v1/runs/{rid}")
            created = _epoch(row.get("created_at")) if isinstance(row, dict) else None
            if (isinstance(row, dict) and row.get("id") and row.get("project_id") in projects and created is not None
                    and created >= self._session_started_at() - SESSION_START_SLACK_SECONDS):
                # Created during this session, in a project it ran a `probe` command
                # in: its own. A teammate's older run a script printed is not.
                admitted[rid] = "run"
                add_run(rid, row)
        return SessionEntities(entities, files, shown, admitted, note_lines)

    def _session_view(self, size: int) -> SessionView:
        """Everything the conclusions pass may show, read from the whole transcript:
        the researcher's prompts and the agent's words, and the output of the commands
        that did work (with the command). `_assemble_view` fits them to a budget."""
        agent = self._agent_ranges()

        def owned(offset: int) -> bool:
            return not any(a < offset <= b for a, b in agent)

        words: list[tuple[int, str]] = []
        prompts: list[tuple[int, str]] = []
        outputs: list[tuple[int, str]] = []
        produced: list[str] = []
        touched: set[str] = set()
        workdirs: list[str] = []
        carried: dict[str, dict] = {}
        pending: dict[str, observe.Event] = {}
        mentioned: set[str] = set()
        offset = 0
        while offset < size:
            lines, new = observe.read_chunk(self.transcript, offset, max_bytes=CHUNK_BYTES)
            if new <= offset:
                break
            seen = observe.observe(self.source, [(o, raw) for o, raw in lines if o <= size], open_calls=carried)
            produced.append(seen.produced_text)
            touched |= seen.touched_files
            workdirs += seen.workdirs
            calls = {**pending, **{e.call_id: e for e in seen.events if e.role == observe.TOOL_CALL and e.call_id}}
            mentioned |= {rid for rid, at in seen.mentioned_runs.items() if owned(at)}
            carried = seen.open_calls
            pending = {cid: calls[cid] for cid in carried if cid in calls}
            for event, index in observe.indexed(seen.events):
                text = observe.render_event(event, index=index)
                if not text:
                    continue
                if event.role == observe.USER and not event.call_id:
                    prompts.append((event.offset, text))
                elif not owned(event.offset):
                    continue
                elif event.role == observe.ASSISTANT:
                    words.append((event.offset, text))
                elif event.role == observe.TOOL_RESULT:
                    call = calls.get(event.call_id or "")
                    command = observe.command_of(call)
                    if observe.is_work_command(command):
                        # The command with its output: which run, which folder, which file.
                        outputs.append((event.offset, observe.render_event(call) + "\n" + text))
            offset = new
        return SessionView(prompts, words, outputs, "\n".join(produced), touched, mentioned, workdirs)

    @staticmethod
    def _assemble_view(view: SessionView, budget: int) -> str:
        """The view within `budget` characters: the researcher's first prompt (the
        goal), then the newest prompts and agent words, then the newest outputs.
        What does not fit is named, and is one `grep`/`expand` away."""
        chosen: list[tuple[int, str]] = []
        left = budget
        dropped = 0
        first = view.prompts[:1]
        for group in (first, sorted(view.prompts[1:] + view.words, reverse=True), list(reversed(view.outputs))):
            for off, text in group:
                if len(text) + 2 > left:
                    dropped += 1
                    continue
                chosen.append((off, text))
                left -= len(text) + 2
        body = "\n\n".join(t for _, t in sorted(chosen))
        if dropped:
            body = (f"[{dropped} older or larger events are not shown here to fit the request; "
                    "`grep` and `expand` reach every one of them]\n\n") + body
        return body

    def _conclusions_skipped(self, cycle_id: int, size: int, why: str) -> str:
        """A conclusions pass the gateway cannot take, recorded and passed over:
        the next turn's pass (or the session-end one) sees the whole session again."""
        with self.ledger.transaction():
            self.ledger.finish_cycle(cycle_id, outcome=f"conclusions skipped: {why}")
            self.ledger.set_json("conclusions_through", max(size, self._turn_offset() or 0))
            self.ledger.set_json("conclusions_at", time.time())
        return f"skipped ({why})"

    def conclude(self, *, shadow: bool = False, wall: float = CYCLE_WALL_SECONDS,
                 targets: list[dict] | None = None) -> str:
        """The conclusions pass: the whole session so far against WHAT YOU MUST
        RECORD, after the agent finished a turn and at session end. Its evidence may
        cite any range the daemon owns (never the agent's). The request is sized to
        the gateway's limit: the facts first, then as much of the session as fits."""
        assert self.api is not None
        deadline = time.monotonic() + wall
        size = self._transcript_size()
        previous = int(self.ledger.get_json("conclusions_through", 0) or 0)
        audit = targets is not None  # the session-end audit brings its own targets
        if not audit and self._owned_end(size) <= previous:
            # Nothing the daemon owns since the last pass (the agent had it back).
            self.ledger.set_json("conclusions_through", size)
            return "nothing new the daemon owns"
        if not audit and self._skipped(previous, size):
            # SKIP mode: the judge found nothing to record in these turns.
            self.ledger.set_json("conclusions_through", size)
            self.ledger.set_json("conclusions_at", time.time())
            return "nothing to record (the judge)"
        view = self._session_view(size)
        if not (view.words or view.outputs):
            self.ledger.set_json("conclusions_through", size)
            return "nothing to conclude"
        if self.spend.exhausted():
            raise api_mod.Budget(429, None, "device daily token ceiling reached")
        entities, files, shown, admitted, note_lines = self._session_entities(view.mentioned)
        known_ids = {**self.ledger.seen_ids(), **admitted}
        run_starts = set(self.ledger.get_json("run_starts", []))
        if not shadow and time.monotonic() - self.last_renew >= RENEW_EVERY_SECONDS and not self._renew():
            raise api_mod.Retryable(0, None, "could not renew the lease")  # the reads above took a while
        if targets is None:
            targets = self._judge_targets(previous, size, deadline, note_lines) if self._judge_on() else []
        facts = dict(
            prior_context="", known_ids=known_ids, decided=self.ledger.recent_decisions(limit=200), feedback=[],
            run_starts=sorted(run_starts), cwd=str(self.cwd), entities=entities, files=shown, conclusions=True,
            targets=targets, existing_notes=note_lines,
        )
        fixed = sum(len(m["content"]) for m in build_messages(chunk_text="", **facts))
        budget = min(CONCLUSIONS_BUDGET_CHARS, REQUEST_CHARS_LIMIT - fixed - 1_000)
        cycle_id = self.ledger.start_cycle(byte_start=0, byte_end=size, events=0)
        if budget < MIN_CONCLUSIONS_VIEW_CHARS:
            return self._conclusions_skipped(cycle_id, size, f"the facts alone are {fixed} characters")
        chunk_text = self._assemble_view(view, budget)
        messages = build_messages(chunk_text=chunk_text, **facts)
        try:
            answer = self._ask(messages, deadline, shadow=shadow, cycle_id=cycle_id, upto=size)
            proposals = _parse_proposals(answer)
        except UnusableAnswer as exc:
            return self._conclusions_skipped(cycle_id, size, str(exc))
        except api_mod.Rejected as exc:
            if exc.status in (413, 422):
                return self._conclusions_skipped(cycle_id, size, f"gateway refused ({exc.status})")
            self.ledger.finish_cycle(cycle_id, outcome=f"conclusions: gateway unusable ({exc.status})")
            raise GatewayUnusable(exc.status, exc.detail, f"gateway unusable: {exc}") from None
        except api_mod.ApiError as exc:
            self.ledger.finish_cycle(cycle_id, outcome=f"conclusions failed: {type(exc).__name__}")
            raise
        usage = answer.get("usage") or {}
        tokens = (int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0))
        decider = Decider(
            self.api, known_ids=known_ids, run_starts=run_starts,
            run_ends=set(self.ledger.get_json("run_ends", [])), cwd=self.cwd,
            touched=view.touched | set(self.ledger.get_json("touched", [])), chunk_text=chunk_text,
            range_start=0, range_end=size, agent_ranges=self._agent_ranges(),
            produced_text=view.produced, workdirs=view.workdirs or self.ledger.get_json("workdirs", []),
            files=files, started_at=self._session_started_at(),
        )
        decisions: list[dict] = []
        for index, raw in enumerate(proposals):
            if index >= MAX_PROPOSALS_PER_CYCLE * 2:
                decisions.append({"raw": raw, "held": "over the per-cycle cap"})
                continue
            try:
                decisions.append(decider.decide(raw))
            except Held as held:
                decisions.append({"raw": raw, "held": str(held)})
        # File notes come as their own map, one line per listed file: cheaper than a
        # proposal each, and outside the proposal cap (a sweep can list 150 files).
        # Their grounding is the FILES listing itself; the evidence is the last byte
        # the daemon owns.
        cite = self._owned_end(size)
        for fid, line in list(_file_notes_of(answer).items())[: 2 * MAX_FILES_SHOWN]:
            raw = {"kind": "file_note", "target": {"type": "artifact", "id": fid}, "notes": line,
                   "evidence": {"from": cite, "to": cite}, "why": "a result file with empty notes"}
            try:
                decisions.append(decider.decide(raw))
            except Held as held:
                decisions.append({"raw": raw, "held": str(held)})
        self._persist(cycle_id, decisions, shadow=shadow, tokens=tokens, feedback_ids=[], watermark=None,
                      conclusions=True)
        # Never below the turn offset: a turn signal past a rewritten transcript's
        # end must not make every later poll a full pass.
        self.ledger.set_json("conclusions_through", max(size, self._turn_offset() or 0))
        self.ledger.set_json("conclusions_at", time.time())
        return f"{len(decisions)} decided"

    def _session_started_at(self) -> float:
        """When the session began: its first timestamped transcript line, else when
        this worker's ledger was created."""
        cached = getattr(self, "_started_at", None)
        if cached is not None:
            return cached
        started = float(self.ledger.get_json("created_at", 0) or 0) or time.time()
        try:
            lines, _ = observe.read_chunk(self.transcript, 0, max_bytes=256 * 1024)
        except OSError:
            lines = []
        for _, raw in lines:
            try:
                stamp = json.loads(raw).get("timestamp")
            except (ValueError, AttributeError):
                continue
            parsed = _epoch(stamp)
            if parsed is not None:
                started = min(started, parsed)
                break
        self._started_at = started
        return started

    def audit(self, *, wall: float) -> str:
        """Session end (plan F12 b): every paragraph the agent wrote in the ranges
        the daemon owns, against the notes that now EXIST on the session's
        entities (read after publishing, so directed and inline notes count). A
        paragraph no note records becomes a target of one last conclusions pass;
        when there is none, no model call is made."""
        if not self._judge_on():
            return "judge off"
        deadline = time.monotonic() + wall
        notes = self._session_entities().note_lines
        targets = self._judge_targets(0, self._transcript_size(), deadline, notes, max_calls=JUDGE_AUDIT_CALLS,
                                      reserve=wall / 2, newest_first=True)  # half the time is the final pass's
        size = self._transcript_size()
        if not targets:
            if int(self.ledger.get_json("conclusions_through", 0) or 0) < size:
                return "nothing missing; " + self.conclude(wall=max(0.0, deadline - time.monotonic()))
            return "nothing missing"
        left = deadline - time.monotonic()
        if left < 10:
            return f"{len(targets)} target(s), no time left for a pass"
        return f"{len(targets)} target(s): " + self.conclude(wall=left, targets=targets)

    def _owned_end(self, size: int) -> int:
        """The last transcript offset at or before `size` the daemon owns."""
        end = size
        for start, stop in sorted(self._agent_ranges(), reverse=True):
            if start < end <= stop:
                end = start
        return end

    def _commit_watermark(self, end: int, run_starts: set, run_ends: set, touched: list,
                          open_calls: dict | None = None) -> None:
        self.ledger.set_watermark(end)
        if open_calls is not None:
            self.ledger.set_json("open_calls", open_calls)
        self.ledger.set_json("undecided_since", None)
        self.ledger.set_json("run_starts", sorted(run_starts))
        self.ledger.set_json("run_ends", sorted(run_ends))
        self.ledger.set_json("touched", touched)

    def _complete(self, messages: list[dict], deadline: float, *, shadow: bool, cycle_id: int | None = None) -> dict:
        """The gateway call, never started without the time -- and, when live,
        the lease -- to finish it. Every attempt is traced (`probe companion trace`)."""
        assert self.api is not None
        last: Exception | None = None
        request = [{**m, "content": secrets.redact(m.get("content") or "")[0]} for m in messages]
        for attempt in range(GATEWAY_ATTEMPTS):
            budget = deadline - time.monotonic()
            if not shadow:
                if not self.lease_ok():
                    raise api_mod.Retryable(0, None, "lease too short to call the gateway")
                if time.monotonic() - self.last_renew >= RENEW_EVERY_SECONDS and not self._renew():
                    raise api_mod.Retryable(0, None, "could not renew the lease")
                budget = min(budget, self.lease_left() - LEASE_MARGIN_SECONDS)
            timeout = min(GATEWAY_TIMEOUT_SECONDS, budget)
            if timeout < 5:
                break
            try:
                answer = self.api.complete(request, max_tokens=MAX_OUTPUT_TOKENS, timeout=timeout)
            except api_mod.ApiError as exc:
                self._trace(cycle_id, attempt, request, error=f"{type(exc).__name__}: {exc}")
                if not isinstance(exc, api_mod.Retryable):
                    raise
                last = exc
                time.sleep(min(2 ** (attempt + 1), max(0.0, deadline - time.monotonic())))
                continue
            usage = answer.get("usage") or {}
            # Every paid round counts against the device ceiling as it returns, even
            # when a later round fails or the answer proves unusable.
            self.spend.add(int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0))
            shown = dict(answer)
            if isinstance(shown.get("content"), str):
                shown["content"] = secrets.redact(shown["content"])[0]
            self._trace(cycle_id, attempt, request, response=shown)
            return answer
        raise last or api_mod.Retryable(0, None, "gateway deadline")

    def _ask(self, messages: list[dict], deadline: float, *, shadow: bool, cycle_id: int | None,
             upto: int) -> dict:
        """The model's answer, after up to RETRIEVAL_ROUNDS rounds in which it asked
        to see more (`{"need": [...]}`). Usage is summed over the rounds. When a
        follow-up comes back EMPTY (seen from the gateway's model on large
        requests) or the model is still asking after the last round, it is asked
        once more without retrieval, from what it was shown, rather than losing
        the range."""
        convo = list(messages)
        spent = {"requests": 0, "expanded": 0}
        tokens_in = tokens_out = 0

        def add(answer: dict) -> None:
            nonlocal tokens_in, tokens_out
            usage = answer.get("usage") or {}
            tokens_in += int(usage.get("input_tokens") or 0)
            tokens_out += int(usage.get("output_tokens") or 0)

        answer: dict = {}
        for round_no in range(RETRIEVAL_ROUNDS + 1):
            answer = self._complete(convo, deadline, shadow=shadow, cycle_id=cycle_id)
            add(answer)
            needs = _needs_of(answer)
            empty = not (answer.get("content") or "").strip()
            if needs is None and not (empty and round_no > 0):
                return {**answer, "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}}
            if needs is None or round_no == RETRIEVAL_ROUNDS:
                break
            parts = [self._fulfil(need, upto, spent) for need in needs]
            reply = "RETRIEVED (context only)\n\n" + "\n\n".join(parts)
            asked = answer.get("content") or ""
            room = REQUEST_CHARS_LIMIT - sum(len(m["content"]) for m in convo) - len(asked)
            if len(reply) > room:
                reply = reply[: max(0, room - 80)] + "\n…[cut to fit the request limit]"
            convo += [{"role": "assistant", "content": asked}, {"role": "user", "content": reply}]
        final = [*messages[:-1], {**messages[-1], "content": messages[-1]["content"] + NO_RETRIEVAL_NOTE}]
        answer = self._complete(final, deadline, shadow=shadow, cycle_id=cycle_id)
        add(answer)
        if _needs_of(answer) is not None:
            raise UnusableAnswer(502, None, "still asking for more after retrieval was withdrawn")
        return {**answer, "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out}}

    def _fulfil(self, need: Any, upto: int, spent: dict) -> str:
        """One retrieval request, as text for the model."""
        if spent["requests"] >= RETRIEVAL_REQUESTS:
            return "(request limit reached: answer with proposals now)"
        spent["requests"] += 1
        if not isinstance(need, dict):
            return "(ignored: a request is an object)"
        if isinstance(need.get("grep"), str) and 0 < len(need["grep"].strip()) <= 200:
            hits = observe.grep(self.transcript, self.source, need["grep"], upto)
            lines = [f"[{eid}] {text}" for eid, text in hits]
            return f'grep "{need["grep"]}": {len(hits)} hit(s)\n' + "\n".join(lines)
        target = need.get("expand")
        if not isinstance(target, str):
            return "(ignored: unknown request)"
        left = RETRIEVAL_EXPAND_CHARS - spent["expanded"]
        if left <= 0:
            return f"(expand {target}: the {RETRIEVAL_EXPAND_CHARS // 1024} KB expansion budget is spent)"
        if target.startswith("reference#"):
            section = _reference_sections().get(target.split("#", 1)[1].strip().lower())
            if section is None:
                return f"(no reference section {target})"
            spent["expanded"] += min(len(section), left)
            return f"[{target}]\n{section[:left]}"
        event = observe.read_event(self.transcript, self.source, target)
        if event is None or event.offset > upto:
            return f"(no event {target} in the transcript read so far)"
        text = observe.event_text(event)
        page = need.get("page") if isinstance(need.get("page"), int) and need["page"] >= 0 else 0
        pages = max(1, -(-len(text) // observe.EXPAND_PAGE_CHARS))
        start = page * observe.EXPAND_PAGE_CHARS
        piece = text[start : start + min(observe.EXPAND_PAGE_CHARS, left)]
        spent["expanded"] += len(piece)
        return (f"[{target} @{event.offset} page {page} of {pages - 1}, chars {start}-{start + len(piece)} "
                f"of {len(text)}]\n{piece}")

    def _trace(self, cycle_id: int | None, attempt: int, request: list[dict], *, response: Any = None,
               error: str | None = None) -> None:
        try:
            self.ledger.add_trace(cycle=cycle_id, round=attempt, request=request, response=response, error=error)
        except Exception:  # noqa: BLE001 - a trace is never worth a cycle
            log.warning("could not store a trace for cycle %s", cycle_id, exc_info=True)

    def publish_pending(self, deadline: float, *, leaving: bool = False) -> str:
        """Publish what is decided while the lease holds.

        Returns PUBLISH_OK (something landed, or nothing is waiting),
        PUBLISH_FAILING (every write tried failed transiently: the caller counts
        a failed cycle, so a daemon that cannot write hands the writing back) or
        PUBLISH_WAITING (writes are sitting out their backoff: neither).
        `leaving` is set while the switch moves to `on`, the one time a write is
        made with the state no longer `daemon`.
        """
        assert self.api is not None
        tried = failed = waiting = 0
        for proposal in self.ledger.pending():
            if time.monotonic() > deadline or not self.lease_ok():
                break
            if not leaving and lease.session_state(self.session_id) != STATE_DAEMON:
                break  # the switch moved mid-pass: not one more write
            if time.time() - proposal.updated_at < publish_backoff(proposal.attempts):
                waiting += 1
                continue
            if time.monotonic() - self.last_renew >= RENEW_EVERY_SECONDS and not self._renew():
                break
            tried += 1
            try:
                response = publish(self.api, proposal)
            except AlreadyDone as done:
                self.ledger.mark(proposal.id, ledger_mod.STATUS_PUBLISHED, reason=str(done))
            except (api_mod.Rejected, api_mod.Forbidden) as exc:
                if api_mod.error_code(exc.detail) == "idempotency_key_reused":
                    # An earlier attempt with this key landed; this one differs only
                    # because it was rebuilt from a fresh read.
                    self.ledger.mark(proposal.id, ledger_mod.STATUS_PUBLISHED, reason="landed earlier")
                else:
                    self.ledger.mark(proposal.id, ledger_mod.STATUS_FAILED, reason=str(exc)[:500],
                                     response=exc.detail)
            except api_mod.Retryable as exc:
                failed += 1
                if proposal.attempts + 1 >= PUBLISH_ATTEMPTS:
                    self.ledger.mark(proposal.id, ledger_mod.STATUS_FAILED, reason=f"gave up: {exc}"[:500])
                else:
                    self.ledger.bump_attempt(proposal.id, str(exc)[:500])
            else:
                self.ledger.mark(proposal.id, ledger_mod.STATUS_PUBLISHED, response=_brief(response))
        if tried and failed == tried:
            return PUBLISH_FAILING
        if not tried and waiting:
            return PUBLISH_WAITING
        return PUBLISH_OK

    # -- the loop --
    def _hand_back(self, state: str | None, reason: str, boundary_reason: str) -> None:
        if state == STATE_DAEMON and (self.live or self.released_reason != reason):
            self.release(reason, boundary_reason, debt=True)

    def _leave_daemon(self, state: str | None) -> None:
        """The researcher moved the switch. To `on`: the daemon finishes its own
        range -- one bounded last cycle, then delivers what it decided -- so the
        span where the agent was refused is not left to nobody. To `read` or
        `off`: not one more call, not one more write."""
        if not self.live:
            return
        if state == STATE_FULL and self.api is not None:
            deadline = time.monotonic() + FINAL_WALL_SECONDS
            try:
                if self.due(final=True):
                    log.info("leaving cycle: %s", self.cycle(shadow=False, wall=FINAL_WALL_SECONDS * 0.7))
            except Exception:  # noqa: BLE001 - leaving must still hand back
                log.exception("leaving cycle failed")
            if self.live:
                self.publish_pending(deadline, leaving=True)
        else:
            held = self.ledger.hold_pending(f"switch moved to {state}")
            if held:
                log.info("held %s undelivered writes: switch moved to %s", held, state)
        if self.live or self.ledger.writer == "daemon":
            self.release(lease.REASON_STOPPED, "disable")

    def run(self) -> int:
        """Until the switch leaves `daemon` (or the session ends).

        The lease is taken only while everything the daemon needs is in place --
        a key, budget, a gateway that answers -- and released WITH A REASON the
        moment one is not, so the agent's next prompt says why recording came
        back to it. A revoked key is not retried until the key CHANGES, and a
        failing gateway not before a cooldown, so the lease cannot flap.
        """
        while not _stop:
            if os.getppid() != self.parent_pid:
                log.info("tap watch is gone; finishing")
                break
            state = lease.session_state(self.session_id)
            shadow = state == STATE_FULL and shadow_enabled()
            if state != STATE_DAEMON:
                if self.live and self.api is None:
                    self.ensure_api()
                self._leave_daemon(state)
                if not shadow:
                    return self.finish()
            if not self.ensure_api():
                self._hand_back(state, lease.REASON_UNAUTHORIZED, "fallback")
                _sleep(60)
                continue
            assert self.api is not None
            if self.api.token == self.blocked_token:
                _sleep(60)
                continue
            if self.spend.exhausted():
                self._hand_back(state, lease.REASON_BUDGET, "budget")
                _sleep(300)
                continue
            if time.monotonic() < self.cooldown_until:
                _sleep(POLL_SECONDS)
                continue
            if state == STATE_DAEMON and not self.live:
                self.take_lease()
            try:
                cycled = False
                outcome = self.decide_due(shadow=shadow)
                if outcome is not None:
                    log.info("cycle: %s", outcome)
                    cycled = True
                    if self.skipped_cycles >= MAX_SKIPPED_CYCLES:
                        raise GatewayUnusable(0, None, "the gateway refused several prompts in a row")
                published = PUBLISH_OK
                if self.live and self.lease_ok():
                    published = self.publish_pending(time.monotonic() + CYCLE_WALL_SECONDS)
                if published == PUBLISH_FAILING:
                    raise api_mod.Retryable(0, None, "every write this pass failed")
                if published == PUBLISH_OK or cycled:
                    # Only a real success clears the failure count: a pass that
                    # only waited out backoff proves nothing.
                    self.failed_cycles = 0 if published == PUBLISH_OK else self.failed_cycles
                # A cycle FINISHED: renew -- at most every RENEW_EVERY_SECONDS
                # while idle, so a quiet session is not a file write per poll.
                if self.lease_ok() and time.monotonic() - self.last_renew >= RENEW_EVERY_SECONDS:
                    self._renew()
            except api_mod.Unauthorized as exc:
                log.warning("unauthorized: %s", exc)
                self.blocked_token = self.api.token if self.api else None
                self._hand_back(state, lease.REASON_UNAUTHORIZED, "fallback")
            except api_mod.Forbidden as exc:
                # The gateway refuses this tenant or this credential: nothing this
                # worker can fix. Hand back, and ask again much later.
                log.warning("forbidden: %s", exc)
                self._hand_back(state, lease.REASON_UNAUTHORIZED, "fallback")
                self.cooldown_until = time.monotonic() + FORBIDDEN_COOLDOWN_SECONDS
            except api_mod.Budget as exc:
                log.warning("budget: %s", exc)
                self._hand_back(state, lease.REASON_BUDGET, "budget")
                _sleep(300)
            except GatewayUnusable as exc:
                log.warning("%s", exc)
                self.skipped_cycles = 0
                self._hand_back(state, lease.REASON_GATEWAY, "fallback")
                self.cooldown_until = time.monotonic() + GATEWAY_COOLDOWN_SECONDS
            except api_mod.Retryable as exc:
                self.failed_cycles += 1
                log.warning("cycle failed (%s in a row): %s", self.failed_cycles, exc)
                if self.failed_cycles >= 2:
                    self._hand_back(state, lease.REASON_GATEWAY, "fallback")
                    self.cooldown_until = time.monotonic() + GATEWAY_COOLDOWN_SECONDS
            except Exception:  # noqa: BLE001 - a worker bug must hand writing back, not wedge
                log.exception("cycle crashed")
                self.failed_cycles += 1
                self._hand_back(state, lease.REASON_ERROR, "fallback")
                self.cooldown_until = time.monotonic() + GATEWAY_COOLDOWN_SECONDS
            _sleep(POLL_SECONDS)
        return self.finish()

    def finish(self) -> int:
        """Session end: one last bounded cycle, publish what is decided, hand back."""
        deadline = time.monotonic() + FINAL_WALL_SECONDS
        try:
            state = lease.session_state(self.session_id)
            if state == STATE_DAEMON and self.lease_ok() and self.ensure_api():
                if self.due(final=True):
                    log.info("final cycle: %s", self.cycle(shadow=False, wall=FINAL_WALL_SECONDS * 0.7))
                # What is decided goes out first: the final pass is a bonus, never the
                # reason a decided note stays in the ledger of an ended session.
                self.publish_pending(deadline - FINAL_PUBLISH_RESERVE_SECONDS)
                size = self._transcript_size()
                left = deadline - time.monotonic() - FINAL_PUBLISH_RESERVE_SECONDS
                audit = os.environ.get(ENV_JUDGE_AUDIT, "").strip().lower() in ("1", "on", "true", "yes")
                if audit and self._judge_on() and self.lease_ok() and left > 20:
                    # The audit replaces the plain final pass (one model call at most):
                    # its targets come from the whole session against the notes that exist.
                    try:
                        log.info("final audit: %s", self.audit(wall=left))
                    except Exception:  # noqa: BLE001 - what is already decided still publishes
                        log.exception("final audit failed")
                elif int(self.ledger.get_json("conclusions_through", 0) or 0) < size and self.lease_ok() and left > 20:
                    try:
                        log.info("final conclusions: %s", self.conclude(wall=left))
                    except Exception:  # noqa: BLE001 - what is already decided still publishes
                        log.exception("final conclusions failed")
                self.publish_pending(deadline)
        except Exception:  # noqa: BLE001
            log.exception("final cycle failed")
        finally:
            if self.live or self.ledger.writer == "daemon":
                self.release(lease.REASON_STOPPED, "session_end")
            self.ledger.close()
            self.spend.close()
        return 0


def _fit_to_budget(source: str, lines: list, end: int, budget: int) -> tuple[list, int]:
    """End the chunk at the last line whose events still fit `budget` rendered
    characters (at least one line, so a cycle always moves forward)."""
    events = observe.parse_lines(source, lines)
    per_line: dict[int, int] = {}
    for event in events:
        per_line[event.offset] = per_line.get(event.offset, 0) + len(observe.render_event(event)) + 2
    total = 0
    for index, (offset, _raw) in enumerate(lines):
        total += per_line.get(offset, 0)
        if total > budget and index > 0:
            return lines[:index], lines[index - 1][0]
    return lines, end


def _merge_workdirs(existing: list, new: list) -> list:
    """Folders the session's commands ran in, most recent last, each once, capped."""
    order = [d for d in existing if d not in new]
    for d in new:
        if d in order:
            order.remove(d)
        order.append(d)
    return order[-20:]


def _merge_touched(existing: list, new: set) -> list:
    """Files the session wrote, oldest first, newest kept when capped."""
    order = [p for p in existing if p not in new] + sorted(new)
    return order[-500:]


def _parse_proposals(answer: dict) -> list:
    """The model's proposals, or Retryable when the answer is not usable.

    An empty, truncated or non-JSON answer is a FAILED cycle, not "nothing to
    record": treating it as an empty list would advance the watermark over the
    range and lose it silently.
    """
    content = answer.get("content") or ""
    try:
        parsed = json.loads(content)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict) and "proposals" not in parsed and isinstance(parsed.get("file_notes"), dict):
        return []  # a conclusions answer with nothing to propose but file notes
    if not isinstance(parsed, dict) or not isinstance(parsed.get("proposals"), list):
        reason = answer.get("finish_reason") or "no JSON object with a proposals list"
        raise UnusableAnswer(502, None, f"unusable gateway answer ({reason})")
    return parsed["proposals"]


def _judge_budget(questions: int) -> int:
    """Characters one judge request may carry in all (the route refuses more)."""
    return int((JUDGE_TOKEN_BUDGET - JUDGE_TOKENS_PER_QUESTION * questions) * JUDGE_CHARS_PER_TOKEN)


def _judge_id(index: int) -> str:
    """A question id the judge route accepts (`^[a-z_]{1,32}$`: no digits)."""
    return "para_" + chr(ord("a") + index)


def _epoch(stamp: Any) -> float | None:
    """An ISO-8601 or epoch timestamp as epoch seconds, or None."""
    if isinstance(stamp, (int, float)):
        return float(stamp) / (1000.0 if stamp > 1e12 else 1.0)
    if isinstance(stamp, str) and stamp:
        try:
            return dt.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _deterministic(seen: observe.Observation) -> bool:
    """A chunk the judge never skips: a run started or ended, a directed command,
    a file written or produced."""
    return bool(seen.run_starts or seen.run_ends or seen.directed or seen.touched_files
                or seen.produced_text.strip())


def _worth_a_call(seen: observe.Observation) -> bool:
    """Does this chunk hold anything the model could record?"""
    return bool(
        any(e.role == observe.ASSISTANT and e.text.strip() for e in seen.events)
        or seen.run_starts or seen.run_ends or seen.directed or seen.touched_files
        or seen.produced_text.strip() or seen.ids
    )


def _needs_of(answer: dict) -> list | None:
    """The retrieval requests of an answer that asks for more instead of
    proposing, or None (a proposals answer, or one the caller's parser judges)."""
    try:
        parsed = json.loads(answer.get("content") or "")
    except ValueError:
        return None
    if not isinstance(parsed, dict) or isinstance(parsed.get("proposals"), list):
        return None
    needs = parsed.get("need")
    return needs[:RETRIEVAL_REQUESTS] if isinstance(needs, list) and needs else None


def _file_notes_of(answer: dict) -> dict[str, str]:
    """The answer's `file_notes` map (file id -> one line), or {}: a missing or
    malformed map costs the file notes, never the proposals beside it."""
    try:
        parsed = json.loads(answer.get("content") or "")
    except ValueError:
        return {}
    notes = parsed.get("file_notes") if isinstance(parsed, dict) else None
    if not isinstance(notes, dict):
        return {}
    return {str(k): v for k, v in notes.items() if isinstance(v, str) and v.strip()}


def _brief(response: Any) -> Any:
    if isinstance(response, dict):
        return {k: response[k] for k in ("id", "slug", "name", "title", "status") if k in response}
    return None


def _sleep(seconds: float) -> None:
    end = time.monotonic() + seconds
    while not _stop and time.monotonic() < end:
        time.sleep(min(1.0, end - time.monotonic()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tap companion")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transcript", required=True, type=Path)
    parser.add_argument("--cwd", required=True, type=Path)
    args = parser.parse_args(argv)
    log_dir = cfg.log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level="INFO",
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[logging.FileHandler(log_dir / f"{args.session_id}.companion.log")],
    )
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)
    # ONE worker per session, whatever spawned it: two `tap watch` processes can
    # briefly overlap for a session, and two workers would fight over the lease.
    import fcntl

    lock_path = ledger_mod.ledger_path(args.session_id).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_handle = lock_path.open("a")
    try:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Usually the previous worker finishing its final cycle: a later spawn
        # will get the lock, so this is not a reason to stop respawning.
        log.info("another worker holds %s; exiting", lock_path)
        return 0
    try:
        worker = Worker(
            session_id=args.session_id,
            transcript=args.transcript,
            cwd=args.cwd,
            source=cfg.capture_source(),
        )
    except ledger_mod.LedgerVersionError as exc:
        log.error("%s", exc)
        return EXIT_DO_NOT_RESPAWN
    return worker.run()
