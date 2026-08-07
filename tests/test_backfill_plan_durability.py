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
