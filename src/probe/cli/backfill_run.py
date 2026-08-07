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


def new_session_id() -> str:
    """A session id Claude Code will accept.

    `str(uuid4())`, NOT `uuid4().hex`. `--session-id` validates the DASHED
    canonical form and rejects the 32-char hex with "Invalid session ID. Must be
    a valid UUID" -- which kills the agent before it reads a single file, so the
    classify pass returns no plan and the import stops having done nothing.

    Caught by the first real end-to-end run and by nothing before it: every test
    fakes `launch_agent`, so the id never reached the binary that validates it.
    """
    return str(uuid.uuid4())


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
    work_dir: Path,
    stream=None,
) -> tuple[plan_mod.Plan | None, str, str | None]:
    """Run the classification agent. Returns ``(plan, tail, session_id)``.

    The session comes back so the review gate can RESUME it to apply a
    correction. That is the difference between a revision costing one turn and
    costing the whole folder again: the evidence is already in that session's
    context. `None` for Codex, which has no resume here, and the caller falls
    back to a cold re-classify that carries the correction in its prompt.

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
        work_dir=str(work_dir),
    )
    session = new_session_id() if agent is bf.Agent.CLAUDE else None
    ok, tail = bf.launch_agent(
        folder,
        prompt,
        agent=agent,
        workdir=work_dir,
        heading=f"Reading {folder.name} — {ev.total_files:,} files",
        total=ev.total_files,
        timeout=CLASSIFY_TIMEOUT_S,
        stream=stream,
        session_id=session,
    )
    if not ok:
        return None, tail, session
    return plan_mod.parse(tail), tail, session


def revise(
    folder: Path,
    ev: evidence_mod.Evidence,
    feedback: str,
    *,
    agent: bf.Agent,
    session_id: str | None,
    work_dir: Path,
    stream=None,
) -> tuple[plan_mod.Plan | None, str, str | None]:
    """Re-run the classifier with a correction. Returns ``(plan, tail, session)``.

    Resumes the classify session when there is one, so the agent still has the
    evidence and the correction costs a single turn. Without one it starts cold
    and the prompt says so -- an agent told to "revise your plan" with no plan
    in context will otherwise invent a fresh one that quietly drops everything
    the reviewer did not mention.

    A failed revision returns no plan and the CALLER KEEPS THE OLD ONE. Losing
    a good-enough plan because the correction round-tripped badly would make
    typing anything at the gate a gamble, which is the opposite of the point.

    The session is returned for the SAME reason it is taken: corrections come
    in rounds. A cold rerun mints one of its own and hands it back, so the
    second correction resumes the first one's work instead of starting over
    again -- which is what "revise, look, revise again" costs otherwise.
    """
    prompt = prompts.revise(
        feedback=feedback,
        root=folder,
        work_dir=str(work_dir),
        resumed=session_id is not None,
    )
    # Mutually exclusive: --resume ADOPTS a session, --session-id MINTS one.
    # Resuming keeps the same id, so `session` is what the next round uses
    # either way.
    fresh = None if session_id else (
        new_session_id() if agent is bf.Agent.CLAUDE else None
    )
    ok, tail = bf.launch_agent(
        folder,
        prompt,
        agent=agent,
        workdir=work_dir,
        heading=f"Revising the plan for {folder.name}",
        total=ev.total_files,
        timeout=CLASSIFY_TIMEOUT_S,
        stream=stream,
        resume=session_id,
        session_id=fresh,
    )
    session = session_id or fresh
    if not ok:
        return None, tail, session
    return plan_mod.parse(tail), tail, session


def _destination(path: str, assigned: dict[str, str]) -> str:
    """Where `path` is going, whether it names a FILE or a rollup DIRECTORY.

    `assigned` is keyed by the expanded per-file paths, so a bare lookup misses
    every rollup row and printed `(unplaced)` beside a directory the plan had in
    fact placed -- next to a header saying all 204 files were placed. The label
    was the only thing wrong, which is worse than a real gap: it sends a
    reviewer hunting for a problem that does not exist.
    """
    direct = assigned.get(path)
    if direct:
        return direct
    prefix = path.rstrip("/") + "/"
    under = {project for p, project in assigned.items() if p.startswith(prefix)}
    if len(under) == 1:
        return f"{under.pop()}  (all {sum(1 for p in assigned if p.startswith(prefix)):,} files under it)"
    if under:
        return "split across " + ", ".join(sorted(under))
    return "(unplaced)"


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
            lines.append(f"  {path}  ->  {_destination(path, assigned)}")
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
    work_dir: Path,
    resume: str | None = None,
    stream=None,
    paint_to=None,
) -> UnitOutcome:
    """One agent, one project, one bounded set of files.

    The ledger is written BEFORE the agent starts and again when it stops. A
    unit recorded as started with no terminal record is the crash signal, and it
    is the only reason a resume knows the difference between "not begun" and
    "died halfway".

    `work_dir` is where the manifest goes, and it is deliberately NOT under the
    folder being imported: the agent may write here and nowhere else, so the
    import cannot leave a trace in the customer's directory.
    """
    manifest = work_dir / f"{unit.unit_id}.jsonl"
    session = new_session_id() if agent is bf.Agent.CLAUDE else None
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
        workdir=work_dir,
        heading=f"Importing into {unit.project}",
        total=unit.files,
        timeout=UNIT_TIMEOUT_S,
        stream=stream,
        session_id=session,
        resume=resume,
        paint_to=paint_to,
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
    work_dir: Path,
    concurrency: int = DEFAULT_CONCURRENCY,
    sessions: dict[str, str] | None = None,
    stream=None,
) -> list[UnitOutcome]:
    """Run units concurrently, bounded.

    `sessions` maps a unit id to a session to resume, so a unit interrupted
    mid-turn keeps what it had already read. The ledger decides WHICH units run;
    this only decides whether a rerun starts cold.

    Concurrent units share ONE screen through a `tui.Board` -- a row each,
    addressed absolutely. Left to themselves they all repaint the same line and
    two of the three are invisible.
    """
    if not units:
        return []
    from probe.cli import tui

    sessions = sessions or {}
    workers = max(1, min(concurrency, len(units)))

    board = None
    if stream is None and tui.interactive():
        board = tui.Board(
            f"Importing {len(units)} unit(s)",
            [f"{u.project[:24]:<26}" for u in units],
        )
        board.open()
        # EVERY ROW PAINTED UP FRONT. Only `concurrency` units start at once, so
        # a board headed "Importing 6 unit(s)" showed three lines and three
        # blanks -- and a blank row is indistinguishable from a row that is not
        # there, which reads as three units having vanished. Saying "queued" is
        # the difference between a queue and a bug.
        for index, unit in enumerate(units):
            board.update(index, bf.Activity(total=unit.files, queued=True).line(0.0))

    def one(pair: tuple[int, ledger_mod.Unit]) -> UnitOutcome:
        index, unit = pair
        return run_unit(
            folder, unit, agent=agent, ledger=ledger, work_dir=work_dir,
            resume=sessions.get(unit.unit_id), stream=stream,
            paint_to=board.row(index) if board else None,
        )

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(one, enumerate(units)))
    finally:
        if board:
            board.close()


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
            row = _resolve_or_create(
                client,
                slug,
                name=(spec.name if spec else slug) or slug,
                description=(spec.description if spec else "") or None,
            )
            made.append(row.get("slug") or slug)
        except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
            problems.append(f"could not create project {slug!r}: {exc}")
    return made, problems


def _resolve_or_create(client, slug: str, *, name: str, description: str | None) -> dict:
    """Use the project called `slug`, creating it if it is not there.

    EXPLICITLY, not through `ensure_project`. That method runs the near-miss
    guard, which refuses a slug resembling one already in the namespace -- and
    the namespace it reads includes the projects this very loop created moments
    earlier. A folder splitting into `odyssey-cluster-deploy`,
    `odyssey-protein-benchmarks` and `odyssey-protein-lm` therefore could not be
    imported at all: the guard read the siblings as typos of each other, refused
    the last, and left the rest behind as orphans.

    The guard is right for what it was built for -- a training loop or a
    detached `probe run start`, where a typo silently opens a second identity
    and nobody is watching. A backfill is the opposite situation, and its slugs
    have already been checked twice by the time they arrive here:

        the classify prompt hands the agent the existing projects and tells it
        to prefer them (REUSE), and a human then reads every project name at
        the review gate with its file count beside it.

    So the guard would be a third opinion holding strictly LESS information
    than the person who just approved the list, and it is the one that cannot
    tell a sibling from a typo. Its own error says the way out is to "create it
    explicitly" -- this is that path, taken deliberately rather than after a
    failure. Duplicates are still impossible: an existing slug resolves and is
    reused, never re-created.
    """
    from probe.sdk import errors

    row = client.resolve_project(slug)
    if row is not None:
        return row
    try:
        return client.create_project(slug, name, description=description)
    except errors.ConflictError:
        # Lost a create race with a concurrent process. What we promise is that
        # the project EXISTS afterwards, not that we made it.
        row = client.resolve_project(slug)
        if row is None:
            raise
        return row


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
                # NO `--async`. It is a ROOT option, so after the subcommand
                # Typer rejects it -- "Error: No such option: --async" -- and
                # every manifest failed to enqueue while all six units reported
                # success. 204 files read, described and manifested; 0 uploaded.
                #
                # Moving it before `artifact` would parse, but it is not wanted
                # either: `--from-manifest` queues to the outbox and lets the
                # drainer deliver "whether or not --async is set -- that is the
                # point of the verb". So the flag is dropped, not relocated.
                "artifact", "add", "--from-manifest", str(out.manifest),
                "--project", project,
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

    # The agents' scratch directory, and the ONLY place either may write. It
    # sits beside the ledger under XDG state rather than inside `folder`,
    # because the folder is the thing being protected: an import must not leave
    # a manifest, a stray note or a half-written file in someone's research
    # directory. See CONFINEMENT in `backfill`.
    work_dir = ledger.path.parent / f"{ledger.path.stem}-manifests"
    work_dir.mkdir(parents=True, exist_ok=True)

    # RESUME: a plan already approved means the classification stands.
    if state.planned and state.approved_at and state.outstanding():
        units = [r.unit for r in state.outstanding()]
        sessions = {r.unit.unit_id: r.session_id for r in state.outstanding() if r.session_id}
        report.add(f"Resuming: {len(units)} of {len(state.units)} units left.")
        assigned_project = {u.unit_id: u.project for u in units}
        # A resumed plan still has to ENSURE its projects. They usually exist
        # already, in which case this resolves them and does nothing -- but the
        # case that matters is the one where the previous attempt recorded the
        # plan and then FAILED to create them. That is now a recoverable state,
        # and this is what recovers it. Names come back as slugs: the specs were
        # the classifier's and are not in the ledger, and a project that already
        # exists keeps the name it was made with either way.
        plan = plan_mod.Plan(projects=[], assignments=[])
        assigned = dict(assigned_project)
    else:
        # A PAGE, not a `say()`. The line version printed wherever the cursor
        # happened to be -- under the leftover folder picker -- and then the
        # agent's status line appended below it, so the whole import read as
        # another program's output leaking into the wizard.
        #
        # It has to be here and not merely in `classify`: sampling a large tree
        # is the slowest step in the run and it emits nothing, so without this
        # the screen holds still on the picker for minutes before the agent's
        # own page replaces this one.
        tui.page([f"Reading {census.describe()} …", "",
                  "Sampling what each file says. Nothing is uploaded yet."])
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
        plan, tail, session = classify(
            folder, ev, agent=agent, existing=existing, work_dir=work_dir
        )
        if plan is None:
            return ["The classification did not produce a usable plan.",
                    (tail.splitlines()[-1] if tail else ""),
                    "Re-running is safe — nothing was uploaded."]

        def settle(p: plan_mod.Plan):
            """A plan as the three things the gate shows: map, discrepancy, page."""
            a, d = plan_mod.resolve(ev, p)
            if project:
                a = dict.fromkeys(a, project)
            return a, d, describe_plan(ev, p, a, d)

        assigned, disc, body = settle(plan)
        if not disc.trustworthy:
            return ["The classification cannot be trusted:", *disc.describe(),
                    "Re-run to try again — nothing was uploaded."]

        if interactive and not yes:
            # THE GATE IS A CONVERSATION, not a yes/no. Accept-or-abandon meant
            # a plan that was 90% right had the same two options as one that was
            # wrong, and abandoning re-paid for the whole classify pass without
            # ever telling the agent WHAT was wrong -- so the rerun tended to
            # produce the same plan. Typing a correction resumes the classify
            # session, which still holds the evidence, and costs one turn.
            note = ""
            while True:
                # `page()` wraps anything over the block width, so the note
                # goes in as one line rather than being pre-broken here.
                feedback = tui.page(
                    body + (["", note] if note else []),
                    prompt="Enter to import, or say what to change: ",
                )
                if not feedback:
                    break
                revised, rtail, session = revise(
                    folder, ev, feedback, agent=agent,
                    session_id=session, work_dir=work_dir,
                )
                if revised is None:
                    # KEEP THE PLAN WE HAD. A correction that round-trips badly
                    # must not cost the reviewer the plan they were reading, or
                    # typing anything here becomes a gamble.
                    note = ("Could not revise: "
                            f"{rtail.splitlines()[-1] if rtail else 'the agent failed'}. "
                            "The plan above is unchanged — try rewording, or "
                            "press Enter to import it as it stands.")
                    continue
                new_assigned, new_disc, new_body = settle(revised)
                if not new_disc.trustworthy:
                    note = ("The revised plan did not account for every file, so "
                            "it was discarded: " + "; ".join(new_disc.describe()) +
                            " The plan above is unchanged.")
                    continue
                plan, assigned, disc, body = revised, new_assigned, new_disc, new_body
                note = ""
        else:
            report.add(*body, "")

        # THE PLAN IS RECORDED BEFORE ANYTHING IS CREATED. It used to be written
        # after `ensure_projects`, so a single refused project threw away the
        # classification AND the review that approved it -- the expensive half of
        # the run, discarded over something a retry fixes. Four consecutive real
        # imports were lost that way. On disk first means a failure below costs a
        # re-run of project creation, not of the folder.
        units = plan_mod.pack(ev, assigned)
        ledger.record_plan(units, sorted(set(assigned.values())))
        ledger.record_approval()
        sessions = {}
        assigned_project = {u.unit_id: u.project for u in units}

    # Common to both paths, and AFTER the ledger write on the fresh one.
    with client_factory() as client:
        made, problems = ensure_projects(client, plan, assigned)
    if problems:
        return ["Could not create every project, so nothing was imported:", *problems,
                "", f"The plan is saved — `probe backfill {folder}` retries just this "
                    "step, without re-reading the folder."]
    report.projects = made

    report.units_total = len(units)
    outcomes = run_units(
        folder, units, agent=agent, ledger=ledger, work_dir=work_dir,
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
        # `probe backfill --resume` was what this said, and there is no such
        # flag -- it exits 2, at the exact moment someone needs the command to
        # work. Resuming is what re-running IS: the ledger decides what is left.
        report.add(f"{census.files - enqueued:,} not queued — build noise and caches are "
                   f"expected here; `probe backfill {folder}` picks up the rest.")
    report.add("", "The queue drains in the background. `probe outbox status` shows "
                   "how much has actually landed.")
    for problem in problems[:5]:
        report.add(f"  {problem}")
    failed = [o for o in outcomes if not o.ok]
    if failed:
        report.add("", f"{len(failed)} unit(s) did not finish. Re-running is safe — "
                       "identical content is deduplicated server-side.")
    return report.lines
