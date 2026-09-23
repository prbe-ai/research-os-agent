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
import hashlib
import json
import logging
import mimetypes
import os
import re
import signal
import time
from pathlib import Path
from typing import Any

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
QUIET_SECONDS = 20
MAX_WAIT_SECONDS = 120
#: Raw transcript bytes per cycle. Rendering only shrinks it (per-event caps), so
#: one cycle's prompt -- this, CONTEXT_BYTES and the rules -- stays well under
#: the gateway's 400k-character request limit.
CHUNK_BYTES = 160 * 1024
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
PUBLISH_ATTEMPTS = 8
#: Per-session ceilings, enforced here and never raised by a server reply.
MAX_WRITES_PER_SESSION = 400
MAX_PROPOSALS_PER_CYCLE = 25
MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
#: A text file is scanned WHOLE for credentials before it may upload; one larger
#: than this is not uploaded at all.
MAX_TEXT_ARTIFACT_BYTES = 10 * 1024 * 1024
#: Runs checked for the same bytes before an upload (the session's most recent).
MAX_DEDUPE_RUNS = 40
#: What the daemon may upload, by extension. Text kinds are scanned whole; the
#: binary kinds are results a run produces (plots, arrays, tables, reports).
#: Anything else -- archives, databases, pickles, keystores -- is the
#: researcher's to upload with `--directed`.
TEXT_ARTIFACT_EXTENSIONS = frozenset({
    ".txt", ".md", ".log", ".csv", ".tsv", ".json", ".jsonl", ".yaml", ".yml", ".html", ".svg",
    ".ipynb", ".py", ".toml", ".tex",
})
BINARY_ARTIFACT_EXTENSIONS = frozenset({
    # Plots and reports a run produced. Arrays and tables (.npy, .parquet) are
    # as often the researcher's INPUT data: theirs to upload with --directed.
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf",
})

ENV_SHADOW = "PROBE_COMPANION_SHADOW"

KINDS = ("note", "describe", "tag", "paper", "edge", "run_end", "artifact")
TARGET_TYPES = ("project", "run")
RUN_RELATIONS = ("forked_from", "resumed_from", "retried_from", "branched_from", "derived_from")
NOTE_TITLE_PREFIX = "companion: "

#: Never uploaded, whatever the model says: the names credentials live under.
_SECRET_NAME_RE = re.compile(
    r"(^|/)(\.env.*|\.netrc|\.pgpass|id_[a-z0-9]+|.*\.pem|.*\.key|.*\.p12|.*\.pfx|.*\.jks|"
    r".*\.keystore|.*credential.*|.*secret.*|.*token.*|config\.json|\.npmrc|\.pypirc|kubeconfig|"
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

FRAMING = """You are the Probe daemon. You read a coding agent's session transcript and decide what to record in Probe, the team's system of record for ML work, so the agent does not have to. The agent still starts runs itself (`probe exec`, `probe run start`, the SDK) and writes what the researcher explicitly asks for (commands carrying `--directed`); never repeat those.

The recording rules below are the same ones the agent follows. Apply them to what the transcript shows, and write only facts the transcript states: never guess a number, a result, a cause or an id.

Answer with ONE JSON object: {"proposals": [...]} and nothing else. An empty list is a good answer when nothing new deserves recording. Each proposal:
  {"kind": one of "note" | "describe" | "tag" | "paper" | "edge" | "run_end" | "artifact",
   "target": {"type": "project" | "run", "id": "<uuid seen in this session>"},
   "evidence": {"from": <offset>, "to": <offset>},  -- the [.. @offset] markers of the events that justify it
   "why": "<one sentence>",
   ...kind fields}
Kind fields:
  note      "title": short, "body": markdown. A decision, finding, caveat or result worth a later reader's time.
  describe  any of "name" (real English, not a re-cased slug), "description", "notes" (what a later reader should distrust). Only fills EMPTY fields.
  tag       "tags": [lowercase-kebab concepts].
  paper     target is the project; "title", "source_url", optional "summary_md", "tags". Only a paper the session actually read.
  edge      target is the NEW run; "source_run_id": the run it came from; "relation": forked_from | resumed_from | retried_from | branched_from | derived_from; "reason".
  run_end   target is a run the session opened with `probe run start` whose process the transcript shows finished; "status": completed | failed.
  artifact  target is the run or project; "path": a file the session produced (as written in the transcript); optional "kind", "notes".
The ids you may use are listed under KNOWN IDS. The set is closed: an id that is not listed cannot be used, so leave the proposal out rather than inventing one. Do not re-propose anything under ALREADY DECIDED. Never delete, never start a run, never mark anything invalid."""


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


def build_messages(
    *,
    chunk_text: str,
    prior_context: str,
    known_ids: dict[str, str | None],
    decided: list[dict],
    feedback: list[dict],
    run_starts: list[str],
    cwd: str,
) -> list[dict]:
    known = [{"id": eid, "type": etype} for eid, etype in list(known_ids.items())[-60:]]
    facts = {
        "working_directory": cwd,
        "KNOWN IDS": known,
        "runs_this_session_opened_with_probe_run_start": run_starts[-20:],
        "ALREADY DECIDED": decided,
    }
    if feedback:
        facts["researcher_feedback_on_your_earlier_writes"] = feedback
    user = [f"SESSION FACTS\n{json.dumps(facts, indent=1)}"]
    if prior_context:
        user.append(
            "EARLIER IN THE SESSION (the agent recorded this part itself; context only, "
            f"propose nothing about it)\n{prior_context}"
        )
    user.append(f"NEW TRANSCRIPT\n{chunk_text}")
    rules = _rules()
    system = FRAMING + ("\n\n# Recording rules (track-work)\n\n" + rules if rules else "")
    return [{"role": "system", "content": system}, {"role": "user", "content": "\n\n".join(user)}]


# ---------------------------------------------------------------------------
# Deciding: validate, ground, read before write.
# ---------------------------------------------------------------------------


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
                 produced_text: str = "", workdirs: list[str] | tuple = ()) -> None:
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
        self.produced_text = produced_text
        self.workdirs = list(workdirs)
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
        body = _str(raw.get("body"), 4000)
        if not title or not body:
            raise Held("note needs a title and a body")
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
        Then: no hidden directory on the way, no credential-shaped name, an
        allowed extension, a text file scanned whole by the same scanner capture
        uses, and never bytes the session already recorded (`_already_recorded`).
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
        suffix = resolved.suffix.lower()
        size = resolved.stat().st_size
        if suffix in TEXT_ARTIFACT_EXTENSIONS:
            if size == 0 or size > MAX_TEXT_ARTIFACT_BYTES:
                raise Held("text file size outside the daemon's limit")
            if secrets.scan(resolved.read_bytes().decode("utf-8", "replace")):
                raise Held("file content matched the secret scanner")
        elif suffix in BINARY_ARTIFACT_EXTENSIONS:
            if size == 0 or size > MAX_ARTIFACT_BYTES:
                raise Held("file size outside the daemon's limit")
        else:
            raise Held(f"the daemon does not upload {suffix or 'extension-less'} files")
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
        seen = observe.observe(self.source, lines)
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
                self._commit_watermark(end, run_starts, run_ends, touched)
            return "nothing to decide"
        if self.spend.exhausted():
            raise api_mod.Budget(429, None, "device daily token ceiling reached")

        prior = ""
        if not self.ledger.get_json("context_shown", False) and start > 0:
            ctx_lines, _ = observe.read_chunk(self.transcript, max(0, start - CONTEXT_BYTES), max_bytes=CONTEXT_BYTES)
            ctx_lines = [(off, raw) for off, raw in ctx_lines if off <= start]
            prior = observe.render(observe.observe(self.source, ctx_lines).events)[-CONTEXT_BYTES:]
        feedback = self.ledger.unconsumed_feedback()
        messages = build_messages(
            chunk_text=chunk_text,
            prior_context=prior,
            known_ids=self.ledger.seen_ids(),
            decided=self.ledger.recent_decisions(),
            feedback=feedback,
            run_starts=sorted(run_starts),
            cwd=str(self.cwd),
        )
        cycle_id = self.ledger.start_cycle(byte_start=start, byte_end=end, events=len(seen.events))
        try:
            answer = self._complete(messages, deadline, shadow=shadow)
            proposals = _parse_proposals(answer)
        except UnusableAnswer as exc:
            streak = self.unusable[1] + 1 if self.unusable[0] == start else 1
            self.unusable = (start, streak)
            if streak >= MAX_UNUSABLE_ANSWERS:
                # The model cannot answer this range: skip it, on the record,
                # rather than hand the lease back and forth over it forever.
                with self.ledger.transaction():
                    self._commit_watermark(end, run_starts, run_ends, touched)
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
                    self._commit_watermark(end, run_starts, run_ends, touched)
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
        self.spend.add(tokens_in + tokens_out)

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

        committed = self.ledger.committed_writes()
        # PERSIST: every decision, the watermark and the cycle, in one transaction.
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
                elif committed >= MAX_WRITES_PER_SESSION:
                    status, reason = ledger_mod.STATUS_HELD, "session write cap reached"
                added = self.ledger.add_proposal(
                    idem_key=idem_key(self.session_id, item["kind"], target, item["payload"]),
                    cycle=cycle_id, kind=item["kind"], target_type=item["target_type"],
                    target_id=item["target_id"], payload=item["payload"],
                    evidence={**item["evidence"], "_op": list(item["op"])},
                    status=status, reason=reason,
                )
                if added and status == ledger_mod.STATUS_PENDING:
                    committed += 1
            self._commit_watermark(end, run_starts, run_ends, touched)
            self.ledger.consume_feedback([f["feedback_id"] for f in feedback])
            self.ledger.set_json("context_shown", True)
            self.ledger.finish_cycle(cycle_id, outcome=f"{len(decisions)} decided",
                                     input_tokens=tokens_in, output_tokens=tokens_out)
        return f"{len(decisions)} decided"

    def _commit_watermark(self, end: int, run_starts: set, run_ends: set, touched: list) -> None:
        self.ledger.set_watermark(end)
        self.ledger.set_json("undecided_since", None)
        self.ledger.set_json("run_starts", sorted(run_starts))
        self.ledger.set_json("run_ends", sorted(run_ends))
        self.ledger.set_json("touched", touched)

    def _complete(self, messages: list[dict], deadline: float, *, shadow: bool) -> dict:
        """The gateway call, never started without the time -- and, when live,
        the lease -- to finish it."""
        assert self.api is not None
        last: Exception | None = None
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
                return self.api.complete(messages, max_tokens=MAX_OUTPUT_TOKENS, timeout=timeout)
            except api_mod.Retryable as exc:
                last = exc
                time.sleep(min(2 ** (attempt + 1), max(0.0, deadline - time.monotonic())))
        raise last or api_mod.Retryable(0, None, "gateway deadline")

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
                if self.due():
                    outcome = self.cycle(shadow=shadow)
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
                self.publish_pending(deadline)
        except Exception:  # noqa: BLE001
            log.exception("final cycle failed")
        finally:
            if self.live or self.ledger.writer == "daemon":
                self.release(lease.REASON_STOPPED, "session_end")
            self.ledger.close()
            self.spend.close()
        return 0


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
    if not isinstance(parsed, dict) or not isinstance(parsed.get("proposals"), list):
        reason = answer.get("finish_reason") or "no JSON object with a proposals list"
        raise UnusableAnswer(502, None, f"unusable gateway answer ({reason})")
    return parsed["proposals"]


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
