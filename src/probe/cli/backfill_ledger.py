"""What a backfill has already done, so a second run does not do it again.

An import over a real research drive runs for hours across many agent
invocations. Anything that long WILL be interrupted -- a laptop lid, a dropped
VPN, an OOM, a Ctrl-C by someone who assumed it had hung. The question this
module answers is the only one that matters afterwards: which work is done.

APPEND-ONLY, REPLAYED. Every record is one line appended under a lock and
fsynced; state is the fold of the file, last write wins per unit. There is no
in-place mutation to tear, so a process killed mid-append loses at most the line
it was writing and the fold still parses. That is the same posture as
:mod:`probe.sdk.journal`, and it uses that module's primitives rather than a
second set.

NOT A SECOND OUTBOX. The outbox journal owns *deliveries* -- which uploads have
reached the server, with retries and dead letters. This owns *decisions* --
which units an agent has finished reading. They answer different questions and
a unit is only complete when both agree: the agent finished AND its ops drained.

IDENTITY SURVIVES A REMOUNT. Keying purely on the folder path looks fine until
a shared drive comes back as ``/Volumes/research`` instead of ``/mnt/research``
and a resumed import silently starts from zero. So a ledger records a
fingerprint of the folder's shape alongside its path, and
:func:`find_resumable` can match a moved folder and offer it rather than
pretending nothing was ever done.

stdlib plus the SDK's durable primitives -- ``probe log`` runs inside training
loops and must not pay for any of this.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..sdk.durable import file_lock, fsync_directory, now_iso

SCHEMA = "probe.backfill/1"


class UnitState(StrEnum):
    """Where one unit of work stands.

    RUNNING is not a transient nobody sees: a unit left RUNNING with no
    terminal record IS the crash signal, and it is the reason this is a log
    rather than a status field someone overwrites.
    """

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class Unit:
    """One agent's worth of work: a set of files bound for one project.

    A set of PATHS, not a directory. Classification is per-file (see
    :mod:`probe.cli.backfill_evidence`), so a unit routinely spans directories
    and a directory routinely splits across units. Recording the directory
    instead would lose exactly the grouping the classifier was run to find.
    """

    unit_id: str
    project: str
    paths: tuple[str, ...]

    @property
    def files(self) -> int:
        return len(self.paths)


@dataclass
class UnitRecord:
    """A unit plus everything the ledger has learned about it."""

    unit: Unit
    state: UnitState = UnitState.PENDING
    session_id: str | None = None
    attempts: int = 0
    #: Rows the AGENT wrote into the manifest. Not proof of anything reaching
    #: storage -- see `delivered`.
    enqueued: int = 0
    #: Rows this unit's manifest handed to the outbox. NONE means the enqueue
    #: was never ATTEMPTED; 0 means it ran and nothing landed.
    #:
    #: The distinction is load-bearing twice over. A unit is marked DONE the
    #: moment its manifest exists -- before the enqueue runs at all -- so a
    #: connection lost at that moment leaves DONE units, an empty outbox and
    #: nothing outstanding, and the next run re-classified the whole folder
    #: while complete manifests sat unused on disk. That is what None detects.
    #:
    #: And 0 must NOT read the same way, or a manifest whose rows are all
    #: rejected (the files were deleted after classification, say) is re-queued
    #: on every run forever, each one appending another ledger line and
    #: returning early so nothing else can ever happen.
    delivered: int | None = None
    error: str | None = None

    @property
    def needs_enqueue(self) -> bool:
        """Finished its agent, wrote rows, and the enqueue never RAN.

        Not "delivered nothing" -- see `delivered`. An enqueue that ran and
        rejected every row has been tried; retrying it forever is a loop, not
        a recovery.
        """
        return (
            self.state is UnitState.DONE
            and self.enqueued > 0
            and self.delivered is None
        )


@dataclass
class State:
    """The fold of a ledger file."""

    root: str | None = None
    fingerprint: str | None = None
    census_files: int = 0
    census_bytes: int = 0
    projects: list[str] = field(default_factory=list)
    units: dict[str, UnitRecord] = field(default_factory=dict)
    approved_at: str | None = None
    #: Whether this ledger's plan was written by a build that records delivery.
    tracks_delivery: bool = False

    @property
    def planned(self) -> bool:
        return bool(self.units)

    def by_state(self, state: UnitState) -> list[UnitRecord]:
        return [r for r in self.units.values() if r.state is state]

    def outstanding(self) -> list[UnitRecord]:
        """Units a resume must still run.

        RUNNING counts as outstanding. A unit that says it started and never
        said anything else did not finish -- treating that as done is how a
        resumed import silently drops whatever was in flight when the process
        died.
        """
        return [
            r
            for r in self.units.values()
            if r.state in (UnitState.PENDING, UnitState.RUNNING, UnitState.FAILED)
        ]

    def unenqueued(self) -> list[UnitRecord]:
        """Units whose manifest is written but whose enqueue never ran.

        Recoverable WITHOUT an agent: the manifest is on disk and complete, so
        this costs one `artifact add` rather than re-reading the folder. Kept
        apart from `outstanding()` for that reason -- they need a different,
        much cheaper thing done to them.

        EMPTY for a plan that predates delivery tracking. Those ledgers record
        no deliveries at all, so every unit in them looks stranded and the
        first run after an upgrade would re-queue an entire drive.
        """
        if not self.tracks_delivery:
            return []
        return [r for r in self.units.values() if r.needs_enqueue]

    def progress(self) -> tuple[int, int]:
        done = sum(1 for r in self.units.values() if r.state is UnitState.DONE)
        return done, len(self.units)


def default_dir() -> Path:
    """Where ledgers live. NOT inside the folder being imported.

    A shared research drive is routinely read-only to the person importing it,
    and writing bookkeeping into someone else's dataset directory is rude even
    when it is permitted. XDG state, like the outbox.
    """
    configured = os.environ.get("PROBE_BACKFILL_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "probe" / "backfill"


def fingerprint(root: Path, *, entries: list[str] | None = None) -> str:
    """A cheap shape-print of a folder, stable across a remount.

    Top-level entry NAMES only -- no contents, no counts of a subtree. It has to
    survive the import itself adding nothing and the folder gaining a file while
    someone was at lunch, so anything finer would stop matching the moment the
    drive was used. Names are what a person recognises too, which is what makes
    the "is this the same folder?" prompt answerable.
    """
    if entries is None:
        try:
            entries = sorted(p.name for p in root.iterdir() if not p.name.startswith("."))
        except OSError:
            entries = []
    digest = hashlib.sha256("\n".join(entries).encode("utf-8", "replace")).hexdigest()
    return digest[:16]


class Ledger:
    """One import's log. Open it, append to it, fold it."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(".lock")

    # -- construction --------------------------------------------------------

    @classmethod
    def for_folder(cls, root: Path, *, directory: Path | None = None) -> Ledger:
        root = Path(root).resolve()
        base = directory or default_dir()
        key = hashlib.sha256(str(root).encode("utf-8", "replace")).hexdigest()[:16]
        return cls(base / f"{key}.jsonl")

    def _ensure(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    # -- writing -------------------------------------------------------------

    def _append(self, record: dict) -> None:
        self._ensure()
        record.setdefault("schema", SCHEMA)
        record.setdefault("at", now_iso())
        line = json.dumps(record, ensure_ascii=False)
        with file_lock(self.lock_path):
            created = not self.path.exists()
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
            if created:
                # The directory entry needs fsyncing too, or a crash can leave a
                # file that exists in the page cache and nowhere else.
                fsync_directory(self.path.parent)
                try:
                    os.chmod(self.path, 0o600)
                except OSError:
                    pass

    def open_import(self, root: Path, *, files: int, bytes_: int) -> None:
        root = Path(root).resolve()
        self._append(
            {
                "t": "census",
                "root": str(root),
                "fingerprint": fingerprint(root),
                "files": files,
                "bytes": bytes_,
            }
        )

    def record_plan(self, units: list[Unit], projects: list[str]) -> None:
        """The classification, as the thing every later unit is measured against."""
        self._append(
            {
                "t": "plan",
                # SCHEMA MARKER. Ledgers written before delivery was tracked
                # carry no `enqueued` records, so every DONE unit in them would
                # read as never-enqueued and the whole folder would be re-queued
                # on the first run after upgrading. Absent means "cannot tell",
                # and cannot-tell must mean leave it alone.
                "tracks_delivery": True,
                "projects": list(projects),
                "units": [
                    {"unit_id": u.unit_id, "project": u.project, "paths": list(u.paths)}
                    for u in units
                ],
            }
        )

    def record_approval(self) -> None:
        """A human looked at the plan and said yes. Resume must not re-ask."""
        self._append({"t": "approved"})

    def start_unit(self, unit_id: str, *, session_id: str | None = None) -> None:
        self._append({"t": "unit", "unit_id": unit_id, "state": UnitState.RUNNING.value,
                      "session_id": session_id})

    def finish_unit(
        self, unit_id: str, *, ok: bool, enqueued: int = 0, error: str | None = None
    ) -> None:
        self._append(
            {
                "t": "unit",
                "unit_id": unit_id,
                "state": (UnitState.DONE if ok else UnitState.FAILED).value,
                "enqueued": enqueued,
                "error": error,
            }
        )

    def record_enqueued(self, unit_id: str, delivered: int) -> None:
        """What this unit's manifest actually handed to the outbox.

        Appended AFTER the enqueue, so the difference between "the agent wrote
        rows" and "the rows are queued" survives a crash between the two.
        """
        self._append({"t": "enqueued", "unit_id": unit_id, "delivered": delivered})

    # -- reading -------------------------------------------------------------

    def read(self) -> State:
        """Fold the log into current state.

        A torn or truncated final line is SKIPPED, not fatal. The whole point of
        append-only is that a process killed mid-write costs one record; a
        parser that refuses the file would turn that into losing the entire
        import's history.
        """
        state = State()
        if not self.path.exists():
            return state
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return state
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            kind = rec.get("t")
            if kind == "census":
                state.root = rec.get("root") or state.root
                state.fingerprint = rec.get("fingerprint") or state.fingerprint
                state.census_files = int(rec.get("files") or 0)
                state.census_bytes = int(rec.get("bytes") or 0)
            elif kind == "plan":
                state.tracks_delivery = bool(rec.get("tracks_delivery"))
                state.projects = [p for p in (rec.get("projects") or []) if isinstance(p, str)]
                state.units = {}
                for u in rec.get("units") or []:
                    if not isinstance(u, dict) or not u.get("unit_id"):
                        continue
                    unit = Unit(
                        unit_id=str(u["unit_id"]),
                        project=str(u.get("project") or ""),
                        paths=tuple(str(p) for p in (u.get("paths") or [])),
                    )
                    state.units[unit.unit_id] = UnitRecord(unit=unit)
            elif kind == "approved":
                state.approved_at = rec.get("at")
            elif kind == "unit":
                rid = str(rec.get("unit_id") or "")
                found = state.units.get(rid)
                if found is None:
                    continue
                try:
                    found.state = UnitState(rec.get("state"))
                except ValueError:
                    continue
                if found.state is UnitState.RUNNING:
                    found.attempts += 1
                    found.session_id = rec.get("session_id") or found.session_id
                found.enqueued = int(rec.get("enqueued") or found.enqueued)
                found.error = rec.get("error")
            elif kind == "enqueued":
                found = state.units.get(str(rec.get("unit_id") or ""))
                if found is not None:
                    found.delivered = int(rec.get("delivered") or 0)
        return state


def new_unit_id() -> str:
    return uuid.uuid4().hex[:12]


def find_resumable(root: Path, *, directory: Path | None = None) -> tuple[Ledger, State] | None:
    """An unfinished import of `root`, by path or by shape.

    Path first, because it is exact. Then shape, so a drive that came back on a
    different mount point is offered rather than silently restarted -- the
    caller should CONFIRM a shape match with the user rather than assume it,
    since two sibling checkouts of the same project legitimately look alike.
    """
    root = Path(root).resolve()
    exact = Ledger.for_folder(root, directory=directory)
    state = exact.read()
    if state.planned and state.outstanding():
        return exact, state

    base = directory or default_dir()
    if not base.exists():
        return None
    want = fingerprint(root)
    for candidate in sorted(base.glob("*.jsonl")):
        ledger = Ledger(candidate)
        found = ledger.read()
        if not found.planned or not found.outstanding():
            continue
        if found.fingerprint == want and found.root != str(root):
            return ledger, found
    return None
