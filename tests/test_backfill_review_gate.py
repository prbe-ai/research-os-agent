"""The review gate is a conversation, not a yes/no.

Accept-or-abandon meant a plan that was 90% right had the same two options as
one that was wrong -- and abandoning re-paid for the whole classify pass without
ever telling the agent WHAT was wrong, so the rerun tended to produce the same
plan. Typing a correction resumes the classify session, which still holds the
evidence, so a revision costs one turn.

Two properties matter more than the loop itself, and both are about what a
reviewer risks by typing:

  * a revision that FAILS must leave the plan they were reading intact
  * a revision that comes back UNTRUSTWORTHY must be discarded, not imported

Either one going the other way makes typing at this gate a gamble, which is the
opposite of the point.
"""

from __future__ import annotations


import pytest

from probe.cli import backfill as bf
from probe.cli import backfill_evidence as ev
from probe.cli import backfill_plan as bp
from probe.cli import backfill_run as br
from probe.cli import tui


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "drive"
    (root / "src").mkdir(parents=True)
    (root / "src" / "train.py").write_text("import torch\n")
    (root / "notes.md").write_text("# odyssey\n")
    (root / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    return root


@pytest.fixture(autouse=True)
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("PROBE_BACKFILL_STATE_DIR", str(d))
    monkeypatch.setattr(bf, "which_agent", lambda agent: f"/bin/{agent.value}")
    return d


def _plan(mapping: dict[str, str], summary: str = "s"):
    return bp.Plan(
        projects=[bp.ProjectSpec(slug=p, name=p, description="d")
                  for p in sorted(set(mapping.values()))],
        assignments=[bp.Assignment(path=k, project=v) for k, v in mapping.items()],
        summary=summary,
    )


def _all_files(folder) -> list[str]:
    return sorted(bp.relative_paths(ev.gather(folder)))


class _Client:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def list_projects(self):
        return []

    def ensure_project(self, slug, name=None, description=None):
        return {"slug": slug}


# -- the loop ---------------------------------------------------------------


def test_a_bare_enter_imports_the_plan_unchanged(folder, monkeypatch):
    files = _all_files(folder)
    plan = _plan(dict.fromkeys(files, "odyssey"))
    revisions: list = []

    monkeypatch.setattr(br, "classify", lambda *a, **k: (plan, "", "sess-1"))
    monkeypatch.setattr(br, "revise", lambda *a, **k: revisions.append(a) or (plan, "", "s"))
    monkeypatch.setattr(br, "run_units", lambda *a, **k: [])
    monkeypatch.setattr(br, "enqueue_manifests", lambda *a, **k: (0, []))
    monkeypatch.setattr(tui, "page", lambda lines, prompt=None: "")

    br.execute(client_factory=_Client, folder=folder, agent=bf.Agent.CLAUDE)
    assert revisions == [], "Enter must not call the agent again"


def test_typed_feedback_revises_and_shows_the_gate_again(folder, monkeypatch):
    files = _all_files(folder)
    first = _plan(dict.fromkeys(files, "everything"))
    better = _plan({f: ("junk" if "lock" in f else "odyssey") for f in files})

    seen: list[list[str]] = []
    answers = iter(["lockfiles are not research, split them out", ""])
    sent: list[str] = []

    def fake_page(lines, prompt=None):
        if prompt is None:
            return ""  # the "Reading N files" screen, not the gate
        seen.append(list(lines))
        return next(answers, "")

    def fake_revise(folder_, ev_, feedback, **kw):
        sent.append(feedback)
        return better, "", "sess-1"

    monkeypatch.setattr(br, "classify", lambda *a, **k: (first, "", "sess-1"))
    monkeypatch.setattr(br, "revise", fake_revise)
    monkeypatch.setattr(br, "run_units", lambda *a, **k: [])
    monkeypatch.setattr(br, "enqueue_manifests", lambda *a, **k: (0, []))
    monkeypatch.setattr(tui, "page", fake_page)

    br.execute(client_factory=_Client, folder=folder, agent=bf.Agent.CLAUDE)

    assert sent == ["lockfiles are not research, split them out"]
    assert len(seen) == 2, "the revised plan must be shown before it is imported"
    # The second page is the REVISED plan, not the first one again.
    assert any("junk" in line for line in seen[1])
    assert not any("junk" in line for line in seen[0])


def test_the_revision_resumes_the_classify_session(folder, monkeypatch):
    """The whole reason a correction is cheap: the evidence is already there."""
    files = _all_files(folder)
    plan = _plan(dict.fromkeys(files, "odyssey"))
    kwargs: list[dict] = []

    def fake_revise(folder_, ev_, feedback, **kw):
        kwargs.append(kw)
        return plan, "", kw.get("session_id")

    monkeypatch.setattr(br, "classify", lambda *a, **k: (plan, "", "sess-abc"))
    monkeypatch.setattr(br, "revise", fake_revise)
    monkeypatch.setattr(br, "run_units", lambda *a, **k: [])
    monkeypatch.setattr(br, "enqueue_manifests", lambda *a, **k: (0, []))
    answers = iter(["change something", ""])
    monkeypatch.setattr(tui, "page",
                        lambda lines, prompt=None: next(answers, "") if prompt else "")

    br.execute(client_factory=_Client, folder=folder, agent=bf.Agent.CLAUDE)
    assert kwargs[0]["session_id"] == "sess-abc"


def test_a_second_correction_resumes_the_first_revision(folder, monkeypatch):
    """Rounds must compound. A cold rerun mints its own session and hands it
    back, or every round after the first re-reads the whole folder."""
    files = _all_files(folder)
    plan = _plan(dict.fromkeys(files, "odyssey"))
    sessions: list = []

    def fake_revise(folder_, ev_, feedback, **kw):
        sessions.append(kw.get("session_id"))
        return plan, "", "sess-2"  # a cold rerun that minted its own

    monkeypatch.setattr(br, "classify", lambda *a, **k: (plan, "", None))  # codex
    monkeypatch.setattr(br, "revise", fake_revise)
    monkeypatch.setattr(br, "run_units", lambda *a, **k: [])
    monkeypatch.setattr(br, "enqueue_manifests", lambda *a, **k: (0, []))
    answers = iter(["first", "second", ""])
    monkeypatch.setattr(tui, "page",
                        lambda lines, prompt=None: next(answers, "") if prompt else "")

    br.execute(client_factory=_Client, folder=folder, agent=bf.Agent.CODEX)
    assert sessions == [None, "sess-2"], f"rounds did not compound: {sessions}"


# -- what a reviewer risks by typing ----------------------------------------


def test_a_failed_revision_keeps_the_plan_they_were_reading(folder, monkeypatch):
    """Typing must never cost the reviewer a plan that was good enough."""
    files = _all_files(folder)
    good = _plan(dict.fromkeys(files, "odyssey"))

    pages: list[list[str]] = []
    answers = iter(["do something impossible", ""])

    def fake_page(lines, prompt=None):
        if prompt is None:
            return ""  # the "Reading N files" screen, not the gate
        pages.append(list(lines))
        return next(answers, "")

    monkeypatch.setattr(br, "classify", lambda *a, **k: (good, "", "s"))
    monkeypatch.setattr(br, "revise", lambda *a, **k: (None, "the agent died", "s"))
    monkeypatch.setattr(br, "run_units", lambda *a, **k: [])
    monkeypatch.setattr(br, "enqueue_manifests", lambda *a, **k: (0, []))
    monkeypatch.setattr(tui, "page", fake_page)
    created: list[str] = []
    monkeypatch.setattr(br, "ensure_projects",
                        lambda c, p, a: (created.extend(sorted(set(a.values()))) or (created, [])))

    br.execute(client_factory=_Client, folder=folder, agent=bf.Agent.CLAUDE)

    # The gate came back, said so, and the plan underneath is the original.
    assert len(pages) == 2
    assert any("Could not revise" in line for line in pages[1])
    assert any("odyssey" in line for line in pages[1])
    assert created == ["odyssey"], "the original plan must still be what imports"


def test_an_untrustworthy_revision_is_discarded_not_imported(folder, monkeypatch):
    """A revision that stops accounting for every file is the one case where
    accepting it silently would upload against a plan nobody approved."""
    files = _all_files(folder)
    good = _plan(dict.fromkeys(files, "odyssey"))
    # Names a file the walk never saw, and drops the ones it did: `unknown`
    # fires, so `trustworthy` is False.
    broken = _plan({"ghost.py": "odyssey"})

    pages: list[list[str]] = []
    answers = iter(["break it", ""])

    def fake_page(lines, prompt=None):
        if prompt is None:
            return ""  # the "Reading N files" screen, not the gate
        pages.append(list(lines))
        return next(answers, "")

    monkeypatch.setattr(br, "classify", lambda *a, **k: (good, "", "s"))
    monkeypatch.setattr(br, "revise", lambda *a, **k: (broken, "", "s"))
    monkeypatch.setattr(br, "run_units", lambda *a, **k: [])
    monkeypatch.setattr(br, "enqueue_manifests", lambda *a, **k: (0, []))
    monkeypatch.setattr(tui, "page", fake_page)
    created: list[str] = []
    monkeypatch.setattr(br, "ensure_projects",
                        lambda c, p, a: (created.extend(sorted(set(a.values()))) or (created, [])))

    br.execute(client_factory=_Client, folder=folder, agent=bf.Agent.CLAUDE)

    assert any("discarded" in line for line in pages[1])
    assert created == ["odyssey"], "the broken revision must not reach the import"


def test_a_non_interactive_run_never_stops_to_ask(folder, monkeypatch):
    """`--yes` and piped output must not block forever on an input() nobody
    is there to answer."""
    files = _all_files(folder)
    plan = _plan(dict.fromkeys(files, "odyssey"))
    asked: list = []

    monkeypatch.setattr(br, "classify", lambda *a, **k: (plan, "", "s"))
    monkeypatch.setattr(br, "revise", lambda *a, **k: asked.append(1) or (plan, "", "s"))
    monkeypatch.setattr(br, "run_units", lambda *a, **k: [])
    monkeypatch.setattr(br, "enqueue_manifests", lambda *a, **k: (0, []))
    # Records only PROMPTING pages. A page with no prompt is just output and
    # is already safe -- `page()` prints verbatim when it cannot ask.
    monkeypatch.setattr(tui, "page",
                        lambda lines, prompt=None: (asked.append(prompt) if prompt else None) or "")

    lines = br.execute(client_factory=_Client, folder=folder, agent=bf.Agent.CLAUDE,
                       interactive=False)
    assert asked == [], "nothing may prompt when there is nobody to prompt"
    assert any("odyssey" in ln for ln in lines), "the plan still has to be reported"


# -- the display ------------------------------------------------------------


def test_a_rollup_directory_shows_where_it_actually_went():
    """`(unplaced)` beside a placed directory, under a header saying every file
    was placed. The label was the only thing wrong, which is worse than a real
    gap: it sends a reviewer hunting for a problem that does not exist."""
    assigned = {
        "sample_data/aws/a.tf": "cluster-deploy",
        "sample_data/aws/b.tf": "cluster-deploy",
        "sample_data/aws/c.tf": "cluster-deploy",
    }
    out = br._destination("sample_data/aws", assigned)
    assert "cluster-deploy" in out
    assert "3 files" in out
    assert "unplaced" not in out


def test_a_directory_split_across_projects_says_so():
    assigned = {"d/a.py": "alpha", "d/b.py": "beta"}
    out = br._destination("d", assigned)
    assert out.startswith("split across")
    assert "alpha" in out and "beta" in out


def test_a_genuinely_unplaced_path_still_says_unplaced():
    """The label has to keep working, or fixing the false alarm hides real ones."""
    assert br._destination("nowhere", {"d/a.py": "alpha"}) == "(unplaced)"


def test_a_file_still_resolves_directly():
    assert br._destination("d/a.py", {"d/a.py": "alpha"}) == "alpha"


# -- page() hands back what was typed ---------------------------------------


def test_page_returns_the_typed_text(monkeypatch, capsys):
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "rows", lambda: 40)
    monkeypatch.setattr("builtins.input", lambda prompt="": "  move the configs  ")
    assert tui.page(["body"], prompt="? ") == "move the configs"


def test_page_returns_empty_for_a_bare_enter(monkeypatch):
    monkeypatch.setattr(tui, "interactive", lambda: True)
    monkeypatch.setattr(tui, "rows", lambda: 40)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    assert tui.page(["body"], prompt="? ") == ""


def test_page_returns_empty_when_it_cannot_ask(monkeypatch):
    monkeypatch.setattr(tui, "interactive", lambda: False)
    assert tui.page(["body"], prompt="? ") == ""
    assert tui.page(["body"]) == ""
