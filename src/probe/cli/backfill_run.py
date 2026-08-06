"""The two-pass import: classify once, then upload in resumable units.

The shape exists to resolve a real conflict in the requirement. Files for one
line of work are scattered across directories, which wants ONE agent with the
whole folder in view. An import that runs for hours will be interrupted, which
wants MANY small agents you can resume. Those pull opposite ways, so the work is
split by what each half is good at:

    PASS A -- classify.  One agent, whole-folder view, uploads nothing. It sees
                         evidence (not a directory listing) and decides which
                         project each FILE belongs to. Cheap enough to redo.

    PASS B -- import.    One agent per unit, each told which project it is
                         filing into. Units are independent, so a crash costs
                         one unit rather than the run.

Between them sits a human, once, looking at the classification with the files
the agent was least sure about pulled to the top.

WHAT EACH HALF IS AUTHORITATIVE FOR, because getting this backwards is how a
backfill lies:

    the walk        how many files exist          (never the model)
    the agent       what they mean                (never the count)
    the ledger      which units are done          (never the agent's word)
    the outbox      what actually reached storage (never "the agent said so")

Agents do not upload. They write a manifest and one process enqueues it: a
process start plus a slug lookup per file is tens of CPU-hours before any bytes
move at the sizes this feature is for.
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import backfill as bf
from . import backfill_evidence as evidence_mod
from . import backfill_ledger as ledger_mod
from . import backfill_plan as plan_mod
from . import backfill_prompts as prompts

#: How many import units run at once. Bounded because each one is a whole agent
#: paying its own context floor, and because the drain behind them is a single
#: worker -- more concurrency here buys queue depth, not throughput.
DEFAULT_CONCURRENCY = 3

#: A unit that has not produced an event in this long is wedged. Long, because a
#: classification turn over a large evidence set legitimately thinks for
#: minutes; finite, because `timeout=None` means one stuck unit stalls an
#: overnight import and nobody finds out until morning.
UNIT_TIMEOUT_S = 45 * 60

#: The classify pass gets its own, larger deadline: it reads the whole evidence
#: set in one turn and there is exactly one of it.
CLASSIFY_TIMEOUT_S = 90 * 60


@dataclass
class UnitOutcome:
    unit: ledger_mod.Unit
    ok: bool
    manifest: Path | None = None
    rows: int = 0
    detail: str = ""


@dataclass
class Report:
    """What the caller pages at the end."""

    lines: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    units_done: int = 0
    units_total: int = 0
    enqueued: int = 0

    def add(self, *lines: str) -> None:
        self.lines.extend(lines)


# -- pass A ------------------------------------------------------------------


def classify(
    folder: Path,
    ev: evidence_mod.Evidence,
    *,
    agent: bf.Agent,
    existing: list[str],
    stream=None,
) -> tuple[plan_mod.Plan | None, str]:
    """Run the classification agent. Returns ``(plan, tail)``.

    Batched by construction rather than by chunking the evidence: the prompt
    carries the whole evidence set and `--autocompact auto` handles the rest.
    The one thing that must not happen quietly is compaction eating the evidence
    mid-decision, so :func:`probe.cli.backfill_prompts.classify` is told when
    the sample budget truncated the input and instructs the agent to mark those
    placements low confidence.
    """
    prompt = prompts.classify(
        root=folder,
        evidence_jsonl=evidence_mod.to_jsonl(ev),
        existing=existing,
        truncated=ev.sample_budget_hit,
    )
    ok, tail = bf.launch_agent(
        folder,
        prompt,
        agent=agent,
        total=ev.total_files,
        timeout=CLASSIFY_TIMEOUT_S,
        stream=stream,
        session_id=uuid.uuid4().hex if agent is bf.Agent.CLAUDE else None,
    )
    if not ok:
        return None, tail
    return plan_mod.parse(tail), tail


def describe_plan(
    ev: evidence_mod.Evidence,
    plan: plan_mod.Plan,
    assigned: dict[str, str],
    disc: plan_mod.Discrepancy,
) -> list[str]:
    """The approval screen's body: what will happen, and what is uncertain.

    Least-certain first. A reviewer reading top-down should meet the decisions
    worth arguing with before the ones that are obviously fine, because the
    whole value of the gate is catching the shared `data/` directory that got
    filed under one researcher.
    """
    per_project: dict[str, int] = {}
    for project in assigned.values():
        per_project[project] = per_project.get(project, 0) + 1

    lines = [f"{len(assigned):,} of {ev.total_files:,} files placed into "
             f"{len(per_project)} project(s):", ""]
    for project in sorted(per_project, key=lambda p: (-per_project[p], p)):
        lines.append(f"  {project:<32} {per_project[project]:>7,} files")

    low = [a for a in plan.assignments if a.confidence != "high"]
    unsure = list(dict.fromkeys(plan.unsure + [a.path for a in low]))
    if unsure:
        lines += ["", "Least certain — check these first:"]
        for path in unsure[:12]:
            lines.append(f"  {path}  ->  {assigned.get(path, '(unplaced)')}")
        if len(unsure) > 12:
            lines.append(f"  ... and {len(unsure) - 12:,} more")

    notes = disc.describe()
    if notes:
        lines += ["", *notes]
    if plan.summary:
        lines += ["", plan.summary]
    return lines


# -- pass B ------------------------------------------------------------------


def run_unit(
    folder: Path,
    unit: ledger_mod.Unit,
    *,
    agent: bf.Agent,
    ledger: ledger_mod.Ledger,
    manifest_dir: Path,
    resume: str | None = None,
    stream=None,
) -> UnitOutcome:
    """One agent, one project, one bounded set of files.

    The ledger is written BEFORE the agent starts and again when it stops. A
    unit recorded as started with no terminal record is the crash signal, and it
    is the only reason a resume knows the difference between "not begun" and
    "died halfway".
    """
    manifest = manifest_dir / f"{unit.unit_id}.jsonl"
    session = uuid.uuid4().hex if agent is bf.Agent.CLAUDE else None
    ledger.start_unit(unit.unit_id, session_id=session)

    prompt = prompts.import_unit(
        root=folder,
        project=unit.project,
        paths=list(unit.paths),
        manifest_path=str(manifest),
    )
    ok, tail = bf.launch_agent(
        folder,
        prompt,
        agent=agent,
        total=unit.files,
        timeout=UNIT_TIMEOUT_S,
        stream=stream,
        session_id=session,
        resume=resume,
    )
    rows = _manifest_rows(manifest)
    # The agent's own account is never the authority. A unit that reports
    # success having written no manifest produced nothing, whatever it said.
    if ok and rows == 0:
        ok = False
        tail = (tail + "\nthe agent wrote no manifest").strip()
    ledger.finish_unit(
        unit.unit_id,
        ok=ok,
        enqueued=rows,
        error=None if ok else (tail.splitlines()[-1] if tail else "failed"),
    )
    return UnitOutcome(
        unit=unit, ok=ok, manifest=manifest if rows else None, rows=rows,
        detail="" if ok else (tail.splitlines()[-1] if tail else "failed"),
    )


def _manifest_rows(path: Path) -> int:
    """How many usable rows a manifest carries.

    Counted here rather than trusted from the agent's summary, and tolerant of a
    torn final line for the same reason the ledger is: a killed agent should
    cost the row it was writing, not the manifest.
    """
    if not path.exists():
        return 0
    rows = 0
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("path"):
                rows += 1
    except OSError:
        return 0
    return rows


def run_units(
    folder: Path,
    units: list[ledger_mod.Unit],
    *,
    agent: bf.Agent,
    ledger: ledger_mod.Ledger,
    manifest_dir: Path,
    concurrency: int = DEFAULT_CONCURRENCY,
    sessions: dict[str, str] | None = None,
    stream=None,
) -> list[UnitOutcome]:
    """Run units concurrently, bounded.

    `sessions` maps a unit id to a session to resume, so a unit interrupted
    mid-turn keeps what it had already read. The ledger decides WHICH units run;
    this only decides whether a rerun starts cold.
    """
    if not units:
        return []
    sessions = sessions or {}
    workers = max(1, min(concurrency, len(units)))

    def one(unit: ledger_mod.Unit) -> UnitOutcome:
        return run_unit(
            folder, unit, agent=agent, ledger=ledger, manifest_dir=manifest_dir,
            resume=sessions.get(unit.unit_id), stream=stream,
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, units))


# -- projects ----------------------------------------------------------------


def ensure_projects(client, plan: plan_mod.Plan, assigned: dict[str, str]) -> tuple[list[str], list[str]]:
    """Create every project the plan needs, BEFORE any unit launches.

    Up front, and not lazily inside the units, because concurrent units filing
    into the same project would otherwise race `probe project create`. Creating
    them here removes the race by construction rather than by locking, and it
    means a killed import still leaves a readable, correctly-named skeleton
    instead of orphaned artifacts.

    Returns ``(slugs, problems)``. A project that cannot be created is a problem
    the caller must surface before uploading anything into a folder whose
    destination does not exist.
    """
    specs = {p.slug: p for p in plan.projects}
    wanted = sorted(set(assigned.values()))
    made: list[str] = []
    problems: list[str] = []
    for slug in wanted:
        spec = specs.get(slug)
        try:
            row = client.ensure_project(
                slug,
                name=(spec.name if spec else slug) or slug,
                description=(spec.description if spec else "") or None,
            )
            made.append(row.get("slug") or slug)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            problems.append(f"could not create project {slug!r}: {exc}")
    return made, problems


def enqueue_manifests(
    folder: Path, outcomes: list[UnitOutcome], *, project_of: dict[str, str]
) -> tuple[int, list[str]]:
    """Hand every unit's manifest to `artifact add --from-manifest`.

    THE CWD IS LOAD-BEARING. A manifest row's `path` is relative to the imported
    folder and the reader resolves it against the process working directory, so
    this runs with `cwd=folder`. Anywhere else, every row fails "is not a regular
    file" -- or worse, a same-named file under the wrong cwd uploads the wrong
    bytes under the right name.

    THE ANCHOR IS PASSED EXPLICITLY. Rows carry no anchor key, so without
    `--project` every row is rejected for want of one and the command exits 1
    having enqueued nothing -- a zero-import wearing a well-formed error.
    """
    import subprocess
    import sys

    enqueued = 0
    problems: list[str] = []
    for out in outcomes:
        if not out.manifest or not out.rows:
            continue
        project = project_of.get(out.unit.unit_id, out.unit.project)
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [
                sys.executable, "-c",
                "import sys; from probe.cli import main; sys.exit(main(sys.argv[1:]))",
                "artifact", "add", "--from-manifest", str(out.manifest),
                "--project", project, "--async",
            ],
            cwd=str(folder), capture_output=True, text=True,
        )
        try:
            summary = json.loads(proc.stdout.strip().splitlines()[-1])
            enqueued += int(summary.get("enqueued") or 0)
            for failure in summary.get("failures") or []:
                problems.append(f"{out.manifest.name} line {failure.get('line')}: "
                                f"{failure.get('error')}")
        except (ValueError, IndexError):
            problems.append(
                f"{out.manifest.name}: could not read the ingest summary "
                f"({(proc.stderr or proc.stdout or '').strip().splitlines()[-1:] or ['no output']})"
            )
    return enqueued, problems


def execute(
    *,
    client_factory,
    folder: Path,
    agent: bf.Agent,
    project: str | None = None,
    interactive: bool = True,
    yes: bool = False,
    concurrency: int | None = None,
) -> list[str]:
    """The whole import. Returns the lines the caller pages.

    Order is deliberate and each step is cheap-and-certain before the one after:
    census, evidence, classify, REVIEW, create projects, import, enqueue,
    reconcile. Nothing mutates anything server-side until the review has passed.
    """
    from probe.cli import tui

    report = Report()
    ledger = ledger_mod.Ledger.for_folder(folder)
    state = ledger.read()

    census = bf.scan(folder, cap=10**9)  # UNCAPPED: this is the denominator
    if census.files == 0:
        return [f"{folder} has no files to import."]
    ledger.open_import(folder, files=census.files, bytes_=census.bytes)

    manifest_dir = ledger.path.parent / f"{ledger.path.stem}-manifests"
    manifest_dir.mkdir(parents=True, exist_ok=True)

    # RESUME: a plan already approved means the classification stands.
    if state.planned and state.approved_at and state.outstanding():
        units = [r.unit for r in state.outstanding()]
        sessions = {r.unit.unit_id: r.session_id for r in state.outstanding() if r.session_id}
        report.add(f"Resuming: {len(units)} of {len(state.units)} units left.")
        assigned_project = {u.unit_id: u.project for u in units}
    else:
        tui.say(f"Reading {census.describe()} …")
        ev = evidence_mod.gather(folder)
        try:
            with client_factory() as client:
                # A backend that cannot list is not a reason to stop -- an empty
                # list just means the agent names everything fresh. FAILING TO
                # OPEN the client is different: that is the credential, and it
                # will fail again for every unit.
                try:
                    existing = [
                        p.get("slug")
                        for p in (client.list_projects() or [])
                        if isinstance(p, dict) and p.get("slug")
                    ]
                except Exception:  # noqa: BLE001
                    existing = []
        except Exception as exc:  # noqa: BLE001 - a credential problem is likely
            # Guarding the `with` itself, not just the call inside it: opening the
            # client is where an expired or absent credential actually fails, and
            # that must name the fix rather than traceback before anything runs.
            return [
                f"Could not reach Probe: {exc}",
                "Run `probe login`, then re-run — nothing was uploaded.",
            ]
        plan, tail = classify(folder, ev, agent=agent, existing=existing)
        if plan is None:
            return ["The classification did not produce a usable plan.",
                    (tail.splitlines()[-1] if tail else ""),
                    "Re-running is safe — nothing was uploaded."]

        assigned, disc = plan_mod.resolve(ev, plan)
        if not disc.trustworthy:
            return ["The classification cannot be trusted:", *disc.describe(),
                    "Re-run to try again — nothing was uploaded."]
        if project:
            assigned = dict.fromkeys(assigned, project)

        body = describe_plan(ev, plan, assigned, disc)
        if interactive and not yes:
            tui.page(body, prompt="Enter to import, Ctrl-C to stop: ")
        else:
            report.add(*body, "")

        with client_factory() as client:
            made, problems = ensure_projects(client, plan, assigned)
        if problems:
            return ["Could not create every project, so nothing was imported:", *problems]
        report.projects = made

        units = plan_mod.pack(ev, assigned)
        ledger.record_plan(units, made)
        ledger.record_approval()
        sessions = {}
        assigned_project = {u.unit_id: u.project for u in units}

    report.units_total = len(units)
    outcomes = run_units(
        folder, units, agent=agent, ledger=ledger, manifest_dir=manifest_dir,
        concurrency=concurrency or DEFAULT_CONCURRENCY, sessions=sessions,
    )
    report.units_done = sum(1 for o in outcomes if o.ok)

    enqueued, problems = enqueue_manifests(folder, outcomes, project_of=assigned_project)
    report.enqueued = enqueued

    report.add(f"Imported {folder}")
    if report.projects:
        report.add("Projects: " + ", ".join(report.projects))
    report.add("", f"{census.files:,} files found on disk · {enqueued:,} queued for upload"
                   f" · {report.units_done}/{report.units_total} units done.")
    if enqueued < census.files:
        report.add(f"{census.files - enqueued:,} not queued — build noise and caches are "
                   "expected here; `probe backfill --resume` picks up the rest.")
    report.add("", "The queue drains in the background. `probe outbox status` shows "
                   "how much has actually landed.")
    for problem in problems[:5]:
        report.add(f"  {problem}")
    failed = [o for o in outcomes if not o.ok]
    if failed:
        report.add("", f"{len(failed)} unit(s) did not finish. Re-running is safe — "
                       "identical content is deduplicated server-side.")
    return report.lines
