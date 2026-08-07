"""The classification survives a failure in project creation.

`record_plan` used to run AFTER `ensure_projects`, so one refused project threw
away the classification and the human review that approved it -- the expensive
half of the run, discarded over something a retry fixes. Four consecutive real
imports of the same folder were lost that way, each one paying for a full
classify pass and then reaching a ledger holding nothing but census records.

So the ordering is the test: on disk first, create second. Which also means a
resumed import has to ENSURE its projects, because "plan recorded, projects
missing" is now a state that can exist -- and is exactly the one being
recovered.
"""

from __future__ import annotations

import pytest

from probe.cli import backfill as bf
from probe.cli import backfill_evidence as ev
from probe.cli import backfill_ledger as bl
from probe.cli import backfill_plan as bp
from probe.cli import backfill_run as br
from probe.cli import tui

#: The REAL enqueue, captured at import -- before the autouse `quiet` fixture
#: replaces the attribute with a stub for every test in this file. Two tests
#: below are about what the shipped function writes to the ledger, and calling
#: the stub would prove nothing about it.
_REAL_ENQUEUE = br.enqueue_manifests


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "drive"
    (root / "src").mkdir(parents=True)
    (root / "src" / "train.py").write_text("import torch\n")
    (root / "notes.md").write_text("# odyssey\n")
    return root


@pytest.fixture(autouse=True)
def quiet(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("PROBE_BACKFILL_STATE_DIR", str(d))
    monkeypatch.setattr(bf, "which_agent", lambda agent: f"/bin/{agent.value}")
    monkeypatch.setattr(tui, "page", lambda lines, prompt=None: "")
    monkeypatch.setattr(br, "run_units", lambda *a, **k: [])
    monkeypatch.setattr(br, "enqueue_manifests", lambda *a, **k: (0, []))
    return d


def _plan(folder):
    files = sorted(bp.relative_paths(ev.gather(folder)))
    return bp.Plan(
        projects=[bp.ProjectSpec(slug="odyssey", name="Odyssey", description="d")],
        assignments=[bp.Assignment(path=f, project="odyssey") for f in files],
    )


class _Client:
    """A client whose project creation can be made to fail."""

    def __init__(self, refuse=()):
        self.refuse = set(refuse)
        self.made: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_projects(self):
        return []

    def resolve_project(self, slug):
        return {"slug": slug} if slug in self.made else None

    def create_project(self, slug, name=None, description=None):
        if slug in self.refuse:
            raise RuntimeError("refusing to create: near-miss")
        self.made.append(slug)
        return {"slug": slug}


def test_a_refused_project_does_not_cost_the_classification(folder, monkeypatch):
    plan = _plan(folder)
    classifies: list[int] = []

    def counted_classify(*a, **k):
        classifies.append(1)
        return plan, "", "sess"

    monkeypatch.setattr(br, "classify", counted_classify)
    client = _Client(refuse={"odyssey"})

    lines = br.execute(client_factory=lambda: client, folder=folder,
                       agent=bf.Agent.CLAUDE, yes=True)
    assert any("Could not create every project" in ln for ln in lines)
    assert any("plan is saved" in ln for ln in lines), \
        "the failure must say the expensive half survived"

    # THE POINT: the ledger holds the approved plan, so a retry resumes.
    state = bl.Ledger.for_folder(folder).read()
    assert state.planned, "the classification was thrown away"
    assert state.approved_at, "the review was thrown away too"
    assert state.outstanding(), "there is nothing left to resume"
    assert classifies == [1]


def test_the_retry_creates_the_projects_without_reclassifying(folder, monkeypatch):
    """The recovery path: plan on disk, projects missing. A resumed import has
    to ensure them, or it runs units filing into projects that do not exist."""
    plan = _plan(folder)
    classifies: list[int] = []

    def counted_classify(*a, **k):
        classifies.append(1)
        return plan, "", "sess"

    monkeypatch.setattr(br, "classify", counted_classify)

    refusing = _Client(refuse={"odyssey"})
    br.execute(client_factory=lambda: refusing, folder=folder,
               agent=bf.Agent.CLAUDE, yes=True)
    assert refusing.made == []

    # Second run: the guard no longer refuses. Nothing is re-read.
    working = _Client()
    lines = br.execute(client_factory=lambda: working, folder=folder,
                       agent=bf.Agent.CLAUDE, yes=True)
    assert working.made == ["odyssey"], "a resumed plan must still ensure its projects"
    assert classifies == [1], "the folder must not be classified a second time"
    assert any("Resuming" in ln for ln in lines)


def test_projects_are_still_created_before_any_unit_runs(folder, monkeypatch):
    """Reordering the ledger write must not reorder THIS: units filing into a
    project that does not exist yet is the race the up-front creation removes."""
    order: list[str] = []

    monkeypatch.setattr(br, "classify", lambda *a, **k: (_plan(folder), "", "s"))
    monkeypatch.setattr(br, "run_units",
                        lambda *a, **k: order.append("units") or [])

    class _Ordered(_Client):
        def create_project(self, slug, name=None, description=None):
            order.append("project")
            return super().create_project(slug, name, description)

    br.execute(client_factory=_Ordered, folder=folder, agent=bf.Agent.CLAUDE, yes=True)
    assert order == ["project", "units"]


def test_several_sibling_projects_are_all_created_from_a_real_run(folder, monkeypatch):
    """End to end, not just the unit: a plan naming several related projects has
    to complete. This is the shape that failed four times in a row."""
    files = sorted(bp.relative_paths(ev.gather(folder)))
    plan = bp.Plan(
        projects=[],
        assignments=[bp.Assignment(path=f, project=f"odyssey-sibling-{i}")
                     for i, f in enumerate(files)],
    )
    monkeypatch.setattr(br, "classify", lambda *a, **k: (plan, "", "s"))

    client = _Client()
    br.execute(client_factory=lambda: client, folder=folder,
               agent=bf.Agent.CLAUDE, yes=True)
    assert sorted(client.made) == sorted(
        {f"odyssey-sibling-{i}" for i in range(len(files))}
    )


# -- a connection lost during enqueue ---------------------------------------


def test_a_lost_enqueue_is_recovered_without_reclassifying(folder, monkeypatch):
    """The gap a dropped connection falls into.

    A unit is marked DONE the moment its manifest exists, but enqueueing
    resolves the project slug over the NETWORK. Lose the connection there and
    every unit reads DONE, the outbox is empty, and `outstanding()` returns
    nothing -- so the next run re-classified the whole folder while complete
    manifests sat unused on disk.
    """
    plan = _plan(folder)
    classifies: list[int] = []

    def counted(*a, **k):
        classifies.append(1)
        return plan, "", "s"

    monkeypatch.setattr(br, "classify", counted)

    # Run 1: the agent writes its manifest, then the enqueue cannot reach the API.
    def dead_network(folder_, outcomes, *, project_of, ledger=None):
        return 0, ["GET /v1/projects: Connection refused"]

    monkeypatch.setattr(br, "enqueue_manifests", dead_network)
    monkeypatch.setattr(br, "run_units", lambda folder_, units, **k: [
        _wrote_manifest(u, k["work_dir"], k["ledger"]) for u in units
    ])
    br.execute(client_factory=_Client, folder=folder, agent=bf.Agent.CLAUDE, yes=True)

    state = bl.Ledger.for_folder(folder).read()
    assert not state.outstanding(), "fixture: units should read DONE"
    assert state.unenqueued(), "a stranded manifest was not detectable"

    # Run 2: the network is back.
    seen: list = []

    def working(folder_, outcomes, *, project_of, ledger=None):
        seen.extend(outcomes)
        for o in outcomes:
            if ledger is not None:
                ledger.record_enqueued(o.unit.unit_id, o.rows)
        return sum(o.rows for o in outcomes), []

    monkeypatch.setattr(br, "enqueue_manifests", working)
    lines = br.execute(client_factory=_Client, folder=folder,
                       agent=bf.Agent.CLAUDE, yes=True)

    assert seen, "the stranded manifest was never re-queued"
    assert classifies == [1], "the folder was classified a second time"
    assert any("Re-queueing" in ln for ln in lines)


def _wrote_manifest(unit, work_dir, ledger):
    """A unit whose agent finished and left a real manifest behind.

    Writes the ledger the way `run_unit` does -- start then finish -- because
    the state this test is about (DONE, but never enqueued) only exists if the
    unit really was marked done.
    """
    import json as _json

    work_dir.mkdir(parents=True, exist_ok=True)
    m = work_dir / f"{unit.unit_id}.jsonl"
    m.write_text(_json.dumps({"path": "notes.md"}) + "\n")
    ledger.start_unit(unit.unit_id, session_id="s")
    ledger.finish_unit(unit.unit_id, ok=True, enqueued=1)
    return br.UnitOutcome(unit=unit, ok=True, manifest=m, rows=1)


def test_a_delivered_unit_is_not_requeued(folder, monkeypatch):
    """Recovery must be idempotent, or every later run re-uploads everything."""
    plan = _plan(folder)
    monkeypatch.setattr(br, "classify", lambda *a, **k: (plan, "", "s"))
    monkeypatch.setattr(br, "run_units", lambda folder_, units, **k: [
        _wrote_manifest(u, k["work_dir"], k["ledger"]) for u in units
    ])

    def working(folder_, outcomes, *, project_of, ledger=None):
        for o in outcomes:
            if ledger is not None:
                ledger.record_enqueued(o.unit.unit_id, o.rows)
        return sum(o.rows for o in outcomes), []

    monkeypatch.setattr(br, "enqueue_manifests", working)
    br.execute(client_factory=_Client, folder=folder, agent=bf.Agent.CLAUDE, yes=True)

    state = bl.Ledger.for_folder(folder).read()
    assert not state.unenqueued(), "a delivered unit still looks stranded"


def test_the_real_enqueue_records_what_it_delivered(tmp_path, monkeypatch):
    """Against the REAL `enqueue_manifests`, not a stub that records for it.

    The recovery hangs entirely on this write: without it every unit looks
    stranded forever and each run re-queues the whole folder. A test that
    stubs the enqueue and calls `record_enqueued` on its behalf proves
    nothing about the code that ships.
    """
    import json as _json

    manifest = tmp_path / "u-1.jsonl"
    manifest.write_text(_json.dumps({"path": "a.py"}) + "\n")
    unit = bl.Unit(unit_id="u-1", project="odyssey", paths=("a.py",))
    outcome = br.UnitOutcome(unit=unit, ok=True, manifest=manifest, rows=1)

    ledger = bl.Ledger.for_folder(tmp_path)
    ledger.record_plan([unit], ["odyssey"])
    ledger.record_approval()
    ledger.start_unit("u-1", session_id="s")
    ledger.finish_unit("u-1", ok=True, enqueued=1)
    assert ledger.read().unenqueued(), "fixture: the unit should look stranded"

    class _Done:
        stdout = _json.dumps({"enqueued": 7, "failures": []}, indent=2)
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda argv, **kw: _Done())
    n, problems = _REAL_ENQUEUE(tmp_path, [outcome],
                                project_of={"u-1": "odyssey"}, ledger=ledger)

    assert n == 7 and not problems
    assert ledger.read().units["u-1"].delivered == 7
    assert not ledger.read().unenqueued(), "still stranded after a successful enqueue"


def test_a_failed_enqueue_records_no_delivery(tmp_path, monkeypatch):
    """The unit must stay recoverable. Recording a delivery on a failure is
    how a stranded manifest becomes invisible."""
    import json as _json

    manifest = tmp_path / "u-2.jsonl"
    manifest.write_text(_json.dumps({"path": "a.py"}) + "\n")
    unit = bl.Unit(unit_id="u-2", project="odyssey", paths=("a.py",))
    ledger = bl.Ledger.for_folder(tmp_path)
    ledger.record_plan([unit], ["odyssey"])
    ledger.record_approval()
    ledger.start_unit("u-2", session_id="s")
    ledger.finish_unit("u-2", ok=True, enqueued=1)

    class _Dead:
        stdout = ""
        stderr = "GET /v1/projects: Connection refused"

    monkeypatch.setattr("subprocess.run", lambda argv, **kw: _Dead())
    n, problems = _REAL_ENQUEUE(
        tmp_path, [br.UnitOutcome(unit=unit, ok=True, manifest=manifest, rows=1)],
        project_of={"u-2": "odyssey"}, ledger=ledger)

    assert n == 0 and problems
    # NONE, not 0. The enqueue never successfully ran, so it has not been
    # attempted -- 0 would mean "ran and rejected everything", which must not
    # be retried forever.
    assert ledger.read().units["u-2"].delivered is None
    assert ledger.read().unenqueued(), "a failed enqueue must stay recoverable"


def test_a_manifest_whose_rows_are_all_rejected_is_not_retried_forever(
    tmp_path, monkeypatch
):
    """Attempted-and-empty is not the same as never-attempted.

    If the files were deleted after classification, every row is rejected and
    the summary legitimately reports 0. Treating that as "still stranded"
    re-queued the same unit on every run, appended a ledger line each time, and
    returned early -- so the folder could never do anything else again."""
    import json as _json

    manifest = tmp_path / "u-3.jsonl"
    manifest.write_text(_json.dumps({"path": "gone.py"}) + "\n")
    unit = bl.Unit(unit_id="u-3", project="odyssey", paths=("gone.py",))
    ledger = bl.Ledger.for_folder(tmp_path)
    ledger.record_plan([unit], ["odyssey"])
    ledger.record_approval()
    ledger.start_unit("u-3", session_id="s")
    ledger.finish_unit("u-3", ok=True, enqueued=1)

    class _AllRejected:
        stdout = _json.dumps({"enqueued": 0, "failures": [
            {"line": 1, "error": "gone.py is not a regular file"}]}, indent=2)
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda argv, **kw: _AllRejected())
    _REAL_ENQUEUE(tmp_path, [br.UnitOutcome(unit=unit, ok=True,
                                            manifest=manifest, rows=1)],
                  project_of={"u-3": "odyssey"}, ledger=ledger)

    st = ledger.read()
    assert st.units["u-3"].delivered == 0, "the attempt was not recorded"
    assert not st.unenqueued(), "an attempted unit must not be retried forever"


def test_a_ledger_from_before_delivery_tracking_is_left_alone(tmp_path):
    """THE UPGRADE PATH. Ledgers already on disk record no deliveries at all,
    so every DONE unit in them reads as never-enqueued -- and the first run
    after upgrading would re-queue an entire drive that imported perfectly."""
    unit = bl.Unit(unit_id="old-1", project="odyssey", paths=("a.py",))
    ledger = bl.Ledger.for_folder(tmp_path)
    ledger.open_import(tmp_path, files=1, bytes_=1)
    # A plan as an older build wrote it: no `tracks_delivery` marker.
    ledger._append({"t": "plan", "projects": ["odyssey"],
                    "units": [{"unit_id": "old-1", "project": "odyssey",
                               "paths": ["a.py"]}]})
    ledger.record_approval()
    ledger.start_unit("old-1", session_id="s")
    ledger.finish_unit("old-1", ok=True, enqueued=99)

    st = ledger.read()
    assert not st.tracks_delivery
    assert st.units["old-1"].needs_enqueue, "the record itself still says so"
    assert st.unenqueued() == [], "an old ledger must not be re-queued wholesale"


def test_a_new_ledger_does_track_delivery(tmp_path):
    ledger = bl.Ledger.for_folder(tmp_path)
    ledger.record_plan([bl.Unit(unit_id="n-1", project="p", paths=("a.py",))], ["p"])
    assert ledger.read().tracks_delivery


def test_a_stranded_unit_with_no_manifest_left_does_not_dead_end(folder, monkeypatch):
    """The scratch dir can be reaped while the ledger survives. Dropping those
    units silently made the folder permanently un-importable: every later run
    reported "nothing else was left to do" having done nothing at all."""
    plan = _plan(folder)
    classifies: list[int] = []

    def counted(*a, **k):
        classifies.append(1)
        return plan, "", "s"

    monkeypatch.setattr(br, "classify", counted)
    monkeypatch.setattr(br, "run_units", lambda folder_, units, **k: [
        _wrote_manifest(u, k["work_dir"], k["ledger"]) for u in units
    ])
    monkeypatch.setattr(br, "enqueue_manifests",
                        lambda f, o, *, project_of, ledger=None: (0, ["dead"]))
    br.execute(client_factory=_Client, folder=folder, agent=bf.Agent.CLAUDE, yes=True)

    # The manifests are reaped, the ledger survives.
    led = bl.Ledger.for_folder(folder)
    work = led.path.parent / f"{led.path.stem}-manifests"
    for m in work.glob("*.jsonl"):
        m.unlink()
    assert led.read().unenqueued(), "fixture: units should still look stranded"

    monkeypatch.setattr(br, "enqueue_manifests",
                        lambda f, o, *, project_of, ledger=None: (1, []))
    lines = br.execute(client_factory=_Client, folder=folder,
                       agent=bf.Agent.CLAUDE, yes=True)

    assert any("cannot be re-queued" in ln for ln in lines), "the loss was hidden"
    assert len(classifies) == 2, "it must fall through and read the files again"


def test_a_failed_requeue_is_not_reported_as_a_finished_job(folder, monkeypatch):
    """The exact failure this path exists to fix, dressed as success.

    The early return fired regardless of whether the re-queue worked, so a run
    with the network still down closed with "Nothing else was left to do. The
    queue drains in the background" — and pointed the user at an empty outbox.
    """
    plan = _plan(folder)
    monkeypatch.setattr(br, "classify", lambda *a, **k: (plan, "", "s"))
    monkeypatch.setattr(br, "run_units", lambda folder_, units, **k: [
        _wrote_manifest(u, k["work_dir"], k["ledger"]) for u in units
    ])
    monkeypatch.setattr(br, "enqueue_manifests",
                        lambda f, o, *, project_of, ledger=None: (0, ["dead"]))
    br.execute(client_factory=_Client, folder=folder, agent=bf.Agent.CLAUDE, yes=True)
    assert bl.Ledger.for_folder(folder).read().unenqueued(), "fixture: still stranded"

    # Second run: the network is STILL down.
    lines = br.execute(client_factory=_Client, folder=folder,
                       agent=bf.Agent.CLAUDE, yes=True)
    joined = "\n".join(lines)
    assert "did not succeed" in joined, f"a failed re-queue read as done:\n{joined}"
    assert "Nothing else was left to do" not in joined
    assert bl.Ledger.for_folder(folder).read().unenqueued(), (
        "a failed re-queue must stay recoverable"
    )
