"""The agent's classification, checked against the walk, packed into units.

DECIDE hands back prose-adjacent JSON. This module is the seam where that
becomes something the rest of the import can act on, and it is deliberately
suspicious: the denominator comes from the filesystem, never from the model, and
the same rule applies one step earlier here. An agent that quietly drops four
thousand files must not be able to produce a plan that looks complete.

So :func:`reconcile_assignments` compares what came back against what was walked
and reports THREE separate discrepancies rather than one boolean:

    missing     walked, never assigned  -- would be silently skipped
    unknown     assigned, never walked  -- hallucinated, or a path typo
    duplicated  assigned more than once -- would upload twice, into two projects

Only `missing` is recoverable by falling back (an unassigned file still has a
home: the project its neighbours went to). `unknown` and `duplicated` mean the
answer cannot be trusted as given, so they surface rather than get patched over.

Packing into units is the other half. A unit is one agent's turn: one project,
a bounded number of files, and -- where it can be arranged -- files that were
written together, because an agent describing a coherent burst writes better
notes than one describing an arbitrary slice.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .backfill import _embedded_summaries
from .backfill_evidence import Evidence
from .backfill_ledger import Unit, new_unit_id

#: Files per unit. Sized so one agent turn stays inside a context it can hold
#: while still reading each file it describes -- and so a crash costs a bounded
#: amount of re-reading rather than an hour.
MAX_UNIT_FILES = 400

#: Bytes per unit, counting only what will actually be uploaded. One unit full
#: of small configs and one full of 90MB archives are very different jobs.
MAX_UNIT_BYTES = 2 * 1024 * 1024 * 1024


@dataclass
class ProjectSpec:
    """A project the classification asked for."""

    slug: str
    name: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)


@dataclass
class Assignment:
    path: str
    project: str
    confidence: str = "high"
    why: str = ""


@dataclass
class Discrepancy:
    """What the walk and the classification disagree about."""

    missing: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    duplicated: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not (self.missing or self.unknown or self.duplicated)

    @property
    def trustworthy(self) -> bool:
        """Whether the plan can be used at all, with or without a fallback.

        `missing` is recoverable. `unknown` and `duplicated` are not: one means
        the model invented paths, the other means it would file one file into
        two projects, and neither is something to paper over silently.
        """
        return not (self.unknown or self.duplicated)

    def describe(self) -> list[str]:
        out: list[str] = []
        if self.missing:
            out.append(
                f"{len(self.missing):,} files were walked but never assigned "
                "— they will follow their neighbours."
            )
        if self.unknown:
            out.append(
                f"{len(self.unknown):,} assigned paths are not in this folder "
                f"(e.g. {self.unknown[0]}) — the classification cannot be trusted."
            )
        if self.duplicated:
            out.append(
                f"{len(self.duplicated):,} files were assigned more than once "
                f"(e.g. {self.duplicated[0]}) — they would upload twice."
            )
        return out


@dataclass
class Plan:
    projects: list[ProjectSpec]
    assignments: list[Assignment]
    unsure: list[str] = field(default_factory=list)
    summary: str = ""


def parse(tail: str) -> Plan | None:
    """Pull the classification out of an agent's event stream.

    Reuses :func:`probe.cli.backfill._embedded_summaries` rather than writing a
    second extractor. That function already solves the hard part: both agents
    are run with `--output-format stream-json`, so the JSON we want is a STRING
    FIELD inside an envelope and its braces are nested in one line of stdout.
    A second implementation here would rot against that one.
    """
    best: Plan | None = None
    for line in reversed(tail.splitlines()):
        if "projects" not in line:
            continue
        for data in _embedded_summaries(line.strip()):
            if not isinstance(data.get("assignments"), list):
                continue  # a run summary, not a classification
            best = _plan_from(data)
            if best is not None:
                return best
    return best


def _plan_from(data: dict) -> Plan | None:
    projects: list[ProjectSpec] = []
    for p in data.get("projects") or []:
        if isinstance(p, str):
            projects.append(ProjectSpec(slug=p, name=p))
        elif isinstance(p, dict) and p.get("slug"):
            projects.append(
                ProjectSpec(
                    slug=str(p["slug"]),
                    name=str(p.get("name") or p["slug"]),
                    description=str(p.get("description") or ""),
                    tags=[str(t) for t in (p.get("tags") or []) if t],
                )
            )
    assignments: list[Assignment] = []
    for a in data.get("assignments") or []:
        if not isinstance(a, dict) or not a.get("path") or not a.get("project"):
            continue
        assignments.append(
            Assignment(
                path=str(a["path"]),
                project=str(a["project"]),
                confidence=str(a.get("confidence") or "high"),
                why=str(a.get("why") or ""),
            )
        )
    if not assignments:
        return None
    return Plan(
        projects=projects,
        assignments=assignments,
        unsure=[str(u) for u in (data.get("unsure") or []) if u],
        summary=str(data.get("summary") or ""),
    )


def relative_paths(evidence: Evidence) -> list[str]:
    root = Path(evidence.root)
    out: list[str] = []
    for f in evidence.files:
        try:
            out.append(str(Path(f.path).relative_to(root)))
        except ValueError:  # pragma: no cover - the walk never leaves the root
            out.append(f.path)
    return out


def reconcile_assignments(evidence: Evidence, plan: Plan) -> Discrepancy:
    """What the walk and the classification disagree about. See module docs."""
    walked = relative_paths(evidence)
    walked_set = set(walked)
    seen: dict[str, int] = {}
    unknown: list[str] = []
    for a in plan.assignments:
        if a.path not in walked_set:
            unknown.append(a.path)
            continue
        seen[a.path] = seen.get(a.path, 0) + 1
    return Discrepancy(
        missing=sorted(walked_set - set(seen)),
        unknown=sorted(set(unknown)),
        duplicated=sorted(p for p, n in seen.items() if n > 1),
    )


def _neighbour_project(path: str, assigned: dict[str, str]) -> str | None:
    """The project of the nearest assigned file, walking up the tree.

    This is the Tier 3 inheritance rule made concrete, and it is applied to
    UNASSIGNED files too. Nearest-directory-first rather than nearest-by-name:
    a checkpoint's siblings are what identify it, and its name is a step number.
    """
    parent = str(Path(path).parent)
    while True:
        prefix = "" if parent == "." else parent + "/"
        here = [p for p in assigned if p.startswith(prefix) and p != path]
        if here:
            counts: dict[str, int] = {}
            for p in here:
                counts[assigned[p]] = counts.get(assigned[p], 0) + 1
            return max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        if parent in (".", "", "/"):
            return None
        nxt = str(Path(parent).parent)
        if nxt == parent:
            return None
        parent = nxt


def resolve(evidence: Evidence, plan: Plan) -> tuple[dict[str, str], Discrepancy]:
    """Final path -> project map, with unassigned files placed by inheritance.

    Returns the map and the discrepancy that produced it, so a caller can show
    what it had to fill in. Silently completing the map would hide exactly the
    thing worth showing a human before anything uploads.
    """
    # A rollup row names a DIRECTORY, so an assignment against one stands for
    # every tail file under it. Expanded BEFORE the reconcile, or the walk would
    # report thousands of "missing" files the agent did in fact place -- turning
    # the honest-denominator check into noise nobody reads.
    walked = set(relative_paths(evidence))
    expanded: list[Assignment] = []
    claimed: set[str] = set()
    # LONGEST PREFIX FIRST. Rollup keys are capped at ROLLUP_MAX_DEPTH but not
    # padded to it, so `a/b` and `a/b/c` can both be rows -- and recursive
    # expansion of the shorter one would claim the longer one's files as well,
    # assigning them twice. Twice is not a cosmetic problem: `duplicated` makes
    # the whole plan untrustworthy, which is correct, so the specific row has to
    # win before the general one is applied.
    ordered = sorted(plan.assignments, key=lambda a: (a.path not in walked, -len(a.path)))
    for a in ordered:
        if a.path in walked:
            if a.path not in claimed:
                claimed.add(a.path)
                expanded.append(a)
            continue
        prefix = "" if a.path in ("", ".") else a.path.rstrip("/") + "/"
        # RECURSIVE, because the rollup that produced this row is. Keys are
        # capped at ROLLUP_MAX_DEPTH, so a `dir` of "michael/odyssey/ckpt"
        # stands for everything beneath it too -- non-recursive expansion would
        # leave every deeper file unassigned and the reconcile would report
        # thousands of files as missing that the agent did place.
        #
        # A more specific row still wins: `direct` is applied after this loop,
        # so an evidence file inside the subtree keeps its own assignment.
        members = [w for w in walked if w.startswith(prefix) and w not in claimed]
        if members:
            claimed.update(members)
            expanded += [
                Assignment(path=m, project=a.project, confidence=a.confidence,
                           why=a.why or f"directory {a.path}")
                for m in members
            ]
        else:
            # Neither a file nor a directory we walked. Genuinely unknown, and
            # the reconcile must still say so.
            expanded.append(a)
    plan = Plan(projects=plan.projects, assignments=expanded,
                unsure=plan.unsure, summary=plan.summary)

    disc = reconcile_assignments(evidence, plan)
    assigned = {a.path: a.project for a in plan.assignments if a.path not in disc.unknown}
    for path in disc.missing:
        inherited = _neighbour_project(path, assigned)
        if inherited is not None:
            assigned[path] = inherited
    return assigned, disc


def pack(
    evidence: Evidence,
    assigned: dict[str, str],
    *,
    max_files: int = MAX_UNIT_FILES,
    max_bytes: int = MAX_UNIT_BYTES,
) -> list[Unit]:
    """Group the assignment map into units: one project, bounded size.

    Files are ordered by mtime inside a project before chunking, so a unit tends
    to hold one burst of work rather than an arbitrary slice. An agent
    describing a coherent burst writes better notes than one describing a
    scatter, and the notes are the part that makes a file findable later.
    """
    root = Path(evidence.root)
    size_of: dict[str, int] = {}
    mtime_of: dict[str, float] = {}
    for f in evidence.files:
        try:
            rel = str(Path(f.path).relative_to(root))
        except ValueError:  # pragma: no cover
            rel = f.path
        size_of[rel] = f.size
        mtime_of[rel] = f.mtime

    by_project: dict[str, list[str]] = {}
    for path, project in assigned.items():
        by_project.setdefault(project, []).append(path)

    units: list[Unit] = []
    for project in sorted(by_project):
        paths = sorted(by_project[project], key=lambda p: (mtime_of.get(p, 0.0), p))
        batch: list[str] = []
        batch_bytes = 0
        for path in paths:
            size = size_of.get(path, 0)
            over_files = len(batch) >= max_files
            over_bytes = batch and batch_bytes + size > max_bytes
            if over_files or over_bytes:
                units.append(
                    Unit(unit_id=new_unit_id(), project=project, paths=tuple(batch))
                )
                batch, batch_bytes = [], 0
            batch.append(path)
            batch_bytes += size
        if batch:
            units.append(Unit(unit_id=new_unit_id(), project=project, paths=tuple(batch)))
    return units
