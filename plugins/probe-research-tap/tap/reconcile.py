"""Reconciler — eventual consistency for transcript capture.

Capture used to depend on a hook firing at exactly the right moment: SessionStart
spawned a daemon, SessionEnd stopped it, and anything that happened outside that
window was lost silently. Three evidenced failure modes on one machine:

  1. A resumed session's SessionStart ran but left no daemon (no pid, no log, no
     process) — 4h/1MB appended to a transcript nobody was watching.
  2. A resume/compaction leg fired SessionStart for a NEW session id whose
     transcript file did not exist yet. The daemon logged "transcript missing"
     for its whole life, exited, and the file appeared afterwards at 2MB — never
     captured.
  3. `drain_once` popped outbox rows only for its OWN session_id, so batches
     belonging to a session that never came back sat in a durable outbox forever
     (two rows stranded nine days).

The fix is to stop relying on the moment. Any live daemon periodically sweeps
ALL local transcripts, compares each against the `file_offsets` cursor, and
backfills whatever grew without a watcher; then it drains every due outbox row
regardless of which session queued it. A missed spawn becomes a delay instead of
a hole, which is why (1)'s unproven root cause no longer has to be found.

Backfill is IN-PROCESS, not a re-spawn. The sweeping daemon reads the gap and
enqueues it under the ORIGINAL session's id and batch sequence. Nothing is
forked, no pid file is written, and no process is signalled — process management
is exactly where the lifecycle bugs live, so the reconciler stays out of it.

WHAT IS ELIGIBLE — this is the load-bearing decision, not a detail. A machine
carries far more transcript history than the tap ever captured: on the box this
was written against, 877 transcripts held 672MB of bytes past the recorded
offsets, most of it predating the plugin. "Diff every transcript against
file_offsets" would have uploaded all of it. A file qualifies only when:

  - it already has a `file_offsets` row (our own cursor — the gap is a tail we
    were watching and lost), OR
  - the tap has a session log for it, which is proof a daemon was spawned for
    that session and therefore that the session started while capture was
    enabled. This is what readmits (2)'s late-materialising fork file.

Everything else is pre-install history or a subagent sidechain transcript
(`agent-*.jsonl`, which never gets a SessionStart of its own and which the tap
has never captured). Adopting those would be a scope change — new categories of
data shipped without the user asking — not a reliability fix. A recency horizon
bounds it further, and a per-sweep byte budget keeps a first sweep on a busy
machine from stampeding the backend. Measured on the same box: 672MB → 60MB at
the 48h default, spread over several sweeps.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from tap import config as cfg
from tap import outbox
from tap.storage import FileOffset, Storage
from tap.transcript import read_new, validate_json

log = logging.getLogger("probe-research-tap.reconcile")

# Sweep cadence, in daemon ticks. At the 60s active interval this is ~10min; at
# the 300s idle interval ~50min. The sweep stats the transcript tree, so it is
# cheap but not free, and nothing it recovers is urgent by definition.
RECONCILE_EVERY_TICKS = 10

# Only consider transcripts modified within this window. Bounds a first sweep on
# a machine with months of history, and keeps the scan off files that will never
# change again. Override with reconcile_horizon_hours (.config) or
# PROBE_RESEARCH_TAP_RECONCILE_HORIZON_HOURS.
DEFAULT_HORIZON_HOURS = 48

# Per-sweep ceilings. A gap can be tens of MB; shipping it in one sweep would
# dump it on the backend in a single burst. These spread recovery over sweeps —
# the cursor advances by whatever was enqueued, so the next sweep resumes where
# this one stopped and nothing is re-read.
MAX_BACKFILL_BYTES_PER_SWEEP = 8 * 1024 * 1024
MAX_BACKFILL_FILES_PER_SWEEP = 4

# Target size for one enqueued batch body. The ingest gateway caps bodies at 2MB
# and `httpclient.classify` maps the resulting 413 to POISON — a silent drop. A
# backfill is precisely where oversized ticks show up (a 2MB gap is one batch if
# nothing splits it), so the reconciler chunks. Budgeted against RAW line bytes,
# which over-estimates: sanitization only ever removes bytes.
MAX_BATCH_BYTES = 1024 * 1024

# How long one daemon holds the right to sweep. Several sessions can be live at
# once and a duplicated sweep would double-ship; the lease makes it one at a
# time. Long enough to cover a sweep that is doing real backfill work, short
# enough that a daemon killed mid-sweep does not block the next one for long.
RECONCILE_LEASE_SECONDS = 300
RECONCILE_LEASE_KEY = "reconcile_lease_until"

# How long a claimed outbox row stays invisible to other drainers. Same idea, one
# row at a time. A crash mid-POST costs at most this much delay before retry.
OUTBOX_LEASE_SECONDS = 120

# Bound on rows drained per global pass, so a large stranded backlog cannot hold
# a tick open indefinitely.
MAX_GLOBAL_DRAIN_PER_SWEEP = 64

# Codex rollout filenames end with the session uuid: rollout-<ts>-<uuid>.jsonl.
_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


@dataclass(frozen=True)
class Gap:
    """One transcript whose bytes ran past the cursor with nobody watching."""

    path: Path
    session_id: str
    cwd: str
    size: int
    byte_offset: int
    last_line_no: int
    tracked: bool
    mtime: int

    @property
    def gap_bytes(self) -> int:
        return self.size - self.byte_offset


@dataclass
class SweepResult:
    files_scanned: int = 0
    gaps_found: int = 0
    files_backfilled: int = 0
    bytes_backfilled: int = 0
    batches_enqueued: int = 0
    rows_drained: int = 0
    skipped_no_lease: bool = False


def horizon_seconds() -> int:
    env = os.environ.get("PROBE_RESEARCH_TAP_RECONCILE_HORIZON_HOURS")
    hours = cfg.parse_positive_int(env)
    if hours is None:
        hours = cfg.parse_positive_int(cfg.read_config_value("reconcile_horizon_hours"))
    return (hours or DEFAULT_HORIZON_HOURS) * 3600


def transcript_root() -> Path:
    """Where this flavour's transcripts live.

    Claude Code writes ~/.claude/projects/<slug>/<session_id>.jsonl. Codex writes
    date-partitioned rollouts under ~/.codex/sessions, which session-start.sh
    passes as --transcript-dir; PRBE_CODEX_SESSIONS_DIR overrides both there and
    here so the two always agree on the tree.
    """
    if cfg.capture_source() == "codex":
        env = os.environ.get("PRBE_CODEX_SESSIONS_DIR")
        return Path(env) if env else Path.home() / ".codex" / "sessions"
    env = os.environ.get("PROBE_RESEARCH_TAP_PROJECTS_DIR")
    return Path(env) if env else Path.home() / ".claude" / "projects"


def session_id_for(path: Path) -> str | None:
    """Recover the session id a transcript belongs to, or None if it isn't one.

    Claude Code names the file for the session. Codex prefixes a timestamp, so
    the uuid is taken off the end. `agent-*.jsonl` is a subagent sidechain: it
    never gets a SessionStart, the tap has never captured one, and picking them
    up here would silently widen what the plugin ships.
    """
    stem = path.stem
    if stem.startswith("agent-"):
        return None
    if cfg.capture_source() == "codex":
        m = _UUID_RE.search(stem)
        return m.group(1) if m else None
    return stem or None


def has_live_daemon(session_id: str) -> bool:
    """True if a wrapper for this session is still running.

    Read-only on purpose: existence plus signal 0. The reconciler never signals a
    daemon and never unlinks a pid file — an unexplained teardown that SIGTERMs
    and unlinks these exact files is still open (see the daemon-lifecycle note),
    and a second sweeper of the same namespace would make it unfalsifiable.
    """
    pid_file = Path(str(cfg.shutdown_sentinel(session_id)).replace(".shutdown", ".pid"))
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # alive, owned by someone else
    except OSError:
        return False
    return True


def _logged_sessions() -> set[str]:
    """Session ids the tap has a daemon log for — proof capture was live for them."""
    try:
        return {p.stem for p in cfg.log_dir().glob("*.log")}
    except OSError:
        return set()


def find_gaps(storage: Storage, *, now: int, horizon_s: int | None = None) -> list[Gap]:
    """Every eligible transcript whose bytes ran past our cursor, biggest first."""
    horizon = horizon_seconds() if horizon_s is None else horizon_s
    root = transcript_root()
    if not root.is_dir():
        return []
    logged = _logged_sessions()
    gaps: list[Gap] = []
    for path in root.rglob("*.jsonl"):
        try:
            st = path.stat()
        except OSError:
            continue
        if not st.st_size:
            continue
        if now - int(st.st_mtime) > horizon:
            continue
        session_id = session_id_for(path)
        if not session_id:
            continue
        prev = storage.get_offset(str(path))
        if prev is None and session_id not in logged:
            # Never ours: pre-install history, or a session that ran while
            # capture was disabled. See the module docstring.
            continue
        byte_offset = prev.byte_offset if prev else 0
        if st.st_size <= byte_offset:
            continue
        if has_live_daemon(session_id):
            continue  # its own daemon owns that cursor
        gaps.append(
            Gap(
                path=path,
                session_id=session_id,
                cwd=prev.cwd if prev else str(path.parent),
                size=st.st_size,
                byte_offset=byte_offset,
                last_line_no=prev.last_line_no if prev else 0,
                tracked=prev is not None,
                mtime=int(st.st_mtime),
            )
        )
    # Freshest first, NOT biggest first. Sorting by size looks right — clear the
    # most bytes per sweep — and is exactly backwards: a live session that just
    # lost its daemon has a small, growing gap, and it would queue behind every
    # multi-megabyte historical file until those finished, taking many sweeps to
    # recover the one conversation still being written. Measured while testing:
    # a 4.5KB gap on an active session lost its sweep to four files averaging
    # 1.7MB apiece. Recency puts the budget where the conversation is.
    gaps.sort(key=lambda g: g.mtime, reverse=True)
    return gaps


def chunk_lines(lines: list[bytes], max_bytes: int | None = None) -> list[list[bytes]]:
    """Split lines into groups that will serialise under the gateway's body cap.

    A single line larger than the budget becomes its own group — it cannot be
    split without destroying the event, and shipping one oversized body that may
    be 413'd beats dropping the event outright.

    The budget is resolved at CALL time, not bound as a default argument: a
    default would freeze MAX_BATCH_BYTES at import and silently ignore any later
    change to it.
    """
    if max_bytes is None:
        max_bytes = MAX_BATCH_BYTES
    groups: list[list[bytes]] = []
    current: list[bytes] = []
    size = 0
    for line in lines:
        n = len(line)
        if current and size + n > max_bytes:
            groups.append(current)
            current = []
            size = 0
        current.append(line)
        size += n
    if current:
        groups.append(current)
    return groups


def _next_batch_seq(storage: Storage, session_id: str) -> int:
    """Resume the session's durable batch sequence.

    Mirrors _run_loop: the R2 key is "<session>:<batch_seq>", so a reused seq
    overwrites an earlier blob. The high-water mark in meta outlives the drained
    rows that max_batch_seq can see.
    """
    from tap.main import _batch_seq_meta_key, _read_int_meta

    return (
        max(
            storage.max_batch_seq(session_id),
            _read_int_meta(storage, _batch_seq_meta_key(session_id), default=-1),
        )
        + 1
    )


def backfill_gap(
    storage: Storage, gap: Gap, *, device_id: str, budget_bytes: int
) -> tuple[int, int]:
    """Read a gap and enqueue it under its OWN session id. Returns (bytes, batches).

    The cursor advances only for the bytes actually enqueued, so a budget that
    cuts a large gap short simply leaves the rest for the next sweep. An enqueue
    failure leaves the cursor untouched and the same bytes are re-read — the
    same contract the live tail uses.
    """
    from tap.main import _batch_seq_meta_key

    try:
        res = read_new(gap.path, gap.byte_offset)
    except (FileNotFoundError, OSError):
        return 0, 0
    if not res.lines:
        return 0, 0

    valid = [ln for ln in res.lines if validate_json(ln)]
    dropped = len(res.lines) - len(valid)
    if dropped:
        log.warning("reconcile: dropped %d malformed lines in %s", dropped, gap.path.name)

    seq = _next_batch_seq(storage, gap.session_id)
    seq_key = _batch_seq_meta_key(gap.session_id)

    consumed_lines = 0
    consumed_bytes = 0
    batches = 0
    line_no = gap.last_line_no

    for group in chunk_lines(valid):
        group_bytes = sum(len(ln) + 1 for ln in group)
        if consumed_bytes and consumed_bytes + group_bytes > budget_bytes:
            break
        body = outbox.build_batch_body(
            device_id=device_id,
            session_id=gap.session_id,
            batch_seq=seq,
            cwd=gap.cwd,
            base_line_no=line_no,
            lines=group,
        )
        if body is not None:
            try:
                outbox.enqueue(
                    storage=storage,
                    session_id=gap.session_id,
                    batch_seq=seq,
                    cwd=gap.cwd,
                    body=body,
                    now=int(time.time()),
                )
            except Exception:
                # Most likely UNIQUE(session_id, batch_seq): that session's own
                # daemon started mid-sweep and is enqueueing too. Stop here and
                # leave the cursor where it is — it owns the tail now.
                log.warning("reconcile: enqueue failed for %s; leaving gap", gap.session_id)
                break
            storage.set_meta(seq_key, str(seq))
            seq += 1
            batches += 1
        # Whether or not the sanitizer kept anything, these lines are processed.
        line_no += len(group)
        consumed_lines += len(group)
        consumed_bytes += group_bytes

    if not consumed_lines:
        return 0, 0

    # Re-derive the exact byte cursor for the lines we actually consumed, rather
    # than trusting the accumulated estimate: split_lines strips \r and skips
    # blank lines, so summed line lengths are not a file position.
    new_offset = byte_offset_after(gap.path, gap.byte_offset, consumed_lines)
    storage.upsert_offset(
        FileOffset(
            path=str(gap.path),
            session_id=gap.session_id,
            cwd=gap.cwd,
            last_line_no=gap.last_line_no + consumed_lines,
            last_seen_at=int(time.time()),
            inode=res.inode,
            size=res.file_size,
            byte_offset=new_offset,
        )
    )
    return new_offset - gap.byte_offset, batches


def byte_offset_after(path: Path, start: int, line_count: int) -> int:
    """Byte position just past the Nth newline at or after `start`."""
    seen = 0
    pos = start
    with path.open("rb") as f:
        f.seek(start)
        while seen < line_count:
            chunk = f.readline()
            if not chunk:
                break
            pos += len(chunk)
            if chunk.endswith(b"\n"):
                if chunk.strip():
                    seen += 1
            else:
                pos -= len(chunk)  # partial trailing line: do not consume
                break
    return pos


def drain_all_due(
    storage: Storage, *, token: str, base_url: str, limit: int = MAX_GLOBAL_DRAIN_PER_SWEEP
) -> int:
    """Drain due outbox rows for EVERY session. Returns rows processed.

    Classification is untouched — SUCCESS deletes, POISON drops, HALT raises and
    latches exactly as the per-session path does. The only change is which rows
    are visible: `drain_once` scoped to one session_id so two concurrent daemons
    could not race the same row, which also meant nothing ever retried a batch
    whose session did not come back. A short lease replaces the scoping, so rows
    are still claimed by one drainer at a time.
    """
    drained = 0
    while drained < limit:
        if not outbox.drain_once(
            storage=storage,
            token=token,
            base_url=base_url,
            session_id=None,
            lease_seconds=OUTBOX_LEASE_SECONDS,
        ):
            break
        drained += 1
    return drained


def sweep(
    storage: Storage,
    *,
    token: str,
    base_url: str,
    device_id: str,
    now: int | None = None,
) -> SweepResult:
    """One reconciliation pass: close capture gaps, then drain everything due.

    Raises HaltError (401) so the caller can stop the daemon exactly as it does
    for the per-session drain. Every other failure is contained per file — the
    reconciler is a safety net and must never be the thing that stops capture.
    """
    now = int(time.time()) if now is None else now
    result = SweepResult()

    if not storage.try_claim_lease(RECONCILE_LEASE_KEY, now, RECONCILE_LEASE_SECONDS):
        result.skipped_no_lease = True
        return result

    try:
        gaps = find_gaps(storage, now=now)
    except OSError:
        log.exception("reconcile: transcript scan failed")
        gaps = []
    result.gaps_found = len(gaps)

    budget = MAX_BACKFILL_BYTES_PER_SWEEP
    for gap in gaps[:MAX_BACKFILL_FILES_PER_SWEEP]:
        if budget <= 0:
            break
        try:
            written, batches = backfill_gap(
                storage, gap, device_id=device_id, budget_bytes=budget
            )
        except Exception:
            log.exception("reconcile: backfill failed for %s", gap.path.name)
            continue
        if written:
            log.info(
                "reconcile: backfilled %d bytes (%d batches) for session=%s %s(gap was %d)",
                written,
                batches,
                gap.session_id,
                "" if gap.tracked else "adopted ",
                gap.gap_bytes,
            )
            result.files_backfilled += 1
            result.bytes_backfilled += written
            result.batches_enqueued += batches
            budget -= written

    result.rows_drained = drain_all_due(storage, token=token, base_url=base_url)

    with contextlib.suppress(Exception):
        storage.release_lease(RECONCILE_LEASE_KEY)
    return result
