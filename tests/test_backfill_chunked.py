"""Classifying a folder too big for one prompt.

The whole evidence set is ONE message, so `--autocompact` cannot save it -- it
compacts across turns and there are none yet. Past the window the request is
rejected outright, after the walk has already been paid for.

The chunked route splits it so that NO CHUNK EVER NEEDS ANOTHER CHUNK'S DETAIL.
That constraint is the design, and it is what these tests are really about:
feeding the agent slices in sequence and letting compaction absorb the overflow
would also "work", and would quietly place early files against evidence and
later ones against a summary of it, with nothing at the review gate able to tell
which was which.

  SURVEY  each slice describes itself. Local only.
  NAME    one pass over those summaries. The only global step.
  ASSIGN  each slice maps its own rows onto the fixed list. Independent.
"""

from __future__ import annotations

import json

import pytest

from probe.cli import backfill as bf
from probe.cli import backfill_evidence as ev
from probe.cli import backfill_run as br


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "drive"
    root.mkdir()
    (root / "a.py").write_text("import torch\n")
    return root


@pytest.fixture(autouse=True)
def agents_installed(monkeypatch):
    monkeypatch.setattr(bf, "which_agent", lambda agent: f"/bin/{agent.value}")


def _evidence(rows: int, sample_len: int = 600) -> ev.Evidence:
    """Evidence with a known number of sampled rows.

    Every file carries a sample, so `to_jsonl` emits one row each and none get
    rolled up -- the row count is the knob these tests turn.
    """
    files = [
        ev.FileEvidence(path=f"/drive/f{i}.py", size=100, mtime=0,
                        tier=ev.Tier.EVIDENCE, sample="x" * sample_len)
        for i in range(rows)
    ]
    return ev.Evidence(
        root="/drive", files=files, clusters=[],
        sampled_files=rows, sampled_bytes=rows * sample_len,
    )


def _envelope(payload: dict) -> str:
    """The agent's answer as it really arrives: JSON inside a stream envelope."""
    return json.dumps({"type": "result", "result": f"done\n{json.dumps(payload)}"})


class _Agent:
    """Answers each pass by the shape it was asked for."""

    def __init__(self, projects=("alpha", "beta"), fail_on=None):
        self.projects = list(projects)
        self.fail_on = fail_on  # "survey" | "name" | "assign"
        self.prompts: list[str] = []

    def __call__(self, folder, prompt, **kw):
        self.prompts.append(prompt)
        if "work out what is in it" in prompt:
            if self.fail_on == "survey":
                return False, "died"
            return True, _envelope({"findings": [{"work": "training", "evidence": [],
                                                  "approx_files": 3}], "notes": ""})
        if "deciding how one research folder" in prompt:
            if self.fail_on == "name":
                return False, "died"
            return True, _envelope({
                "projects": [{"slug": s, "name": s, "description": "d"}
                             for s in self.projects],
                "summary": "a folder",
            })
        if "already decided" in prompt:
            if self.fail_on == "assign":
                return False, "died"
            rows = [json.loads(ln) for ln in prompt.splitlines()
                    if ln.startswith('{"path"')]
            return True, _envelope({
                "assignments": [{"path": r["path"], "project": self.projects[0],
                                 "confidence": "high", "why": "w"} for r in rows],
                "unsure": [],
            })
        raise AssertionError(f"unrecognised prompt:\n{prompt[:300]}")


# -- the dispatch ------------------------------------------------------------


def test_a_folder_that_fits_still_goes_to_one_agent(folder, monkeypatch):
    """Single-shot is the BETTER classification -- every file judged against
    every other, in one decision -- so chunking must stay a fallback."""
    calls: list[str] = []

    def fake(folder_, prompt, **kw):
        calls.append(prompt)
        return True, _envelope({"projects": [{"slug": "alpha", "name": "a"}],
                                "assignments": [], "summary": "s"})

    monkeypatch.setattr(bf, "launch_agent", fake)
    br.classify(folder, _evidence(3), agent=bf.Agent.CLAUDE, existing=[],
                work_dir=folder / ".w")
    assert len(calls) == 1
    assert "deciding how one research folder should be organised" in calls[0]


def test_a_folder_too_big_for_one_prompt_is_chunked(folder, monkeypatch):
    agent = _Agent()
    monkeypatch.setattr(bf, "launch_agent", agent)
    big = _evidence(600)
    assert ev.estimate_tokens(ev.to_jsonl(big)) > ev.SINGLE_SHOT_TOKEN_BUDGET

    plan, tail, session = br.classify(folder, big, agent=bf.Agent.CLAUDE,
                                      existing=[], work_dir=folder / ".w")
    assert plan is not None
    assert "slices" in tail
    # No session: the chunked route runs many, and resuming "the" one would
    # resume whichever happened to be last.
    assert session is None


def test_every_row_survives_the_round_trip(folder, monkeypatch):
    """A classification silently missing files is the one outcome worth failing
    over -- `resolve` would report them as unassigned and the plan untrusted."""
    agent = _Agent()
    monkeypatch.setattr(bf, "launch_agent", agent)
    big = _evidence(600)
    rows = [json.loads(ln)["path"] for ln in ev.to_jsonl(big).splitlines()]

    plan, _, _ = br.classify(folder, big, agent=bf.Agent.CLAUDE, existing=[],
                             work_dir=folder / ".w")
    assert sorted(a.path for a in plan.assignments) == sorted(rows)


# -- the constraint the design exists for ------------------------------------


def test_no_survey_slice_is_told_about_another_slice(folder, monkeypatch):
    """Local only. A slice that could see its siblings would be back to needing
    the whole folder in context, which is the thing that does not fit."""
    agent = _Agent()
    monkeypatch.setattr(bf, "launch_agent", agent)
    br.classify(folder, _evidence(600), agent=bf.Agent.CLAUDE, existing=[],
                work_dir=folder / ".w")

    surveys = [p for p in agent.prompts if "work out what is in it" in p]
    assert len(surveys) > 1, "the evidence did not actually split"
    for prompt in surveys:
        assert "Do NOT name projects yet" in prompt
        assert "not all of it" in prompt


def test_the_assign_pass_is_given_the_projects_and_cannot_invent_one(
    folder, monkeypatch
):
    """This is what makes assignment independent: the global decision is
    already made, so a slice needs nothing from its siblings."""
    agent = _Agent(projects=("alpha", "beta"))
    monkeypatch.setattr(bf, "launch_agent", agent)
    br.classify(folder, _evidence(600), agent=bf.Agent.CLAUDE, existing=[],
                work_dir=folder / ".w")

    assigns = [p for p in agent.prompts if "already decided" in p]
    assert assigns
    for prompt in assigns:
        assert "alpha" in prompt and "beta" in prompt
        assert "may not invent one" in prompt


def test_an_invented_project_parks_the_row_rather_than_dropping_it(
    folder, monkeypatch
):
    """The agent was told not to invent a slug. If it does anyway, losing the
    row is worse than filing it wrong: a reviewer can move a flagged file, but
    cannot see one that was never mentioned."""
    class _Inventing(_Agent):
        def __call__(self, folder_, prompt, **kw):
            if "already decided" in prompt:
                rows = [json.loads(ln) for ln in prompt.splitlines()
                        if ln.startswith('{"path"')]
                return True, _envelope({
                    "assignments": [{"path": r["path"], "project": "made-up"}
                                    for r in rows],
                    "unsure": [],
                })
            return super().__call__(folder_, prompt, **kw)

    monkeypatch.setattr(bf, "launch_agent", _Inventing())
    big = _evidence(600)
    plan, _, _ = br.classify(folder, big, agent=bf.Agent.CLAUDE, existing=[],
                             work_dir=folder / ".w")
    rows = len(ev.to_jsonl(big).splitlines())
    assert len(plan.assignments) == rows, "rows were dropped"
    assert all(a.project in {"alpha", "beta"} for a in plan.assignments)
    assert len(plan.unsure) == rows, "the reviewer was not told"


# -- failure is total, never partial -----------------------------------------


@pytest.mark.parametrize("stage", ["survey", "name", "assign"])
def test_a_failed_pass_returns_no_plan_at_all(folder, monkeypatch, stage):
    """Half a classification uploads half a folder into projects nobody
    approved for the rest of it."""
    monkeypatch.setattr(bf, "launch_agent", _Agent(fail_on=stage))
    plan, tail, _ = br.classify(folder, _evidence(600), agent=bf.Agent.CLAUDE,
                                existing=[], work_dir=folder / ".w")
    assert plan is None
    assert tail


def test_a_naming_pass_that_names_nothing_is_a_failure(folder, monkeypatch):
    class _Empty(_Agent):
        def __call__(self, folder_, prompt, **kw):
            if "deciding how one research folder" in prompt:
                return True, _envelope({"projects": [], "summary": ""})
            return super().__call__(folder_, prompt, **kw)

    monkeypatch.setattr(bf, "launch_agent", _Empty())
    plan, tail, _ = br.classify(folder, _evidence(600), agent=bf.Agent.CLAUDE,
                                existing=[], work_dir=folder / ".w")
    assert plan is None and "named" in tail


# -- the chunker itself ------------------------------------------------------


def test_chunks_stay_under_the_budget():
    jsonl = "\n".join(json.dumps({"path": f"f{i}.py", "sample": "x" * 600})
                      for i in range(500))
    for chunk in ev.chunk_lines(jsonl):
        assert ev.estimate_tokens("\n".join(chunk)) <= ev.CHUNK_TOKEN_BUDGET


def test_row_count_is_capped_independently_of_tokens():
    """The assign pass emits one object per row, so a chunk of tiny rollup rows
    can fit the INPUT budget and still ask for more output than the model
    produces -- and a truncated final message loses assignments silently."""
    jsonl = "\n".join(json.dumps({"dir": f"d{i}"}) for i in range(5_000))
    chunks = ev.chunk_lines(jsonl)
    assert all(len(c) <= ev.CHUNK_MAX_ROWS for c in chunks)
    assert len(chunks) >= 10


def test_no_row_is_lost_or_split_by_chunking():
    jsonl = "\n".join(json.dumps({"path": f"f{i}.py"}) for i in range(1_000))
    flat = [ln for c in ev.chunk_lines(jsonl) for ln in c]
    assert flat == jsonl.splitlines()


def test_a_row_bigger_than_the_budget_still_gets_through():
    """Half a JSON object is not evidence. An oversized row gets its own chunk
    rather than being dropped or cut."""
    jsonl = json.dumps({"path": "huge.py", "sample": "x" * 400_000})
    chunks = ev.chunk_lines(jsonl)
    assert len(chunks) == 1 and chunks[0] == [jsonl]


def test_the_estimate_runs_high_not_low():
    """Guessing low is the expensive direction: it sends a prompt the model
    rejects after the walk has already been paid for."""
    # JSON tokenises worse than prose, so the estimate must exceed chars/4.
    text = json.dumps({"path": "a/b/c.py", "sample": "def f(): return 1"}) * 100
    assert ev.estimate_tokens(text) > len(text) / 4


# -- the two regressions the review caught ----------------------------------


def test_a_rollup_row_answered_by_dir_is_not_dropped(folder, monkeypatch):
    """The assign prompt tells the agent to use a rollup's "dir" value verbatim,
    so it answers with that key -- and accepting only "path" dropped every one
    of them. `backfill_plan._plan_from` already carries this exact fix, with a
    comment saying it cost "thousands of files ... with nothing reported"."""
    class _ByDir(_Agent):
        def __call__(self, folder_, prompt, **kw):
            if "already decided" in prompt:
                rows = [json.loads(ln) for ln in prompt.splitlines()
                        if ln.startswith('{"path"')]
                return True, _envelope({
                    # The agent echoes `dir`, as the prompt taught it to.
                    "assignments": [{"dir": r["path"], "project": self.projects[0]}
                                    for r in rows],
                    "unsure": [],
                })
            return super().__call__(folder_, prompt, **kw)

    monkeypatch.setattr(bf, "launch_agent", _ByDir())
    big = _evidence(600)
    plan, _, _ = br.classify(folder, big, agent=bf.Agent.CLAUDE, existing=[],
                             work_dir=folder / ".w")
    assert plan is not None
    assert len(plan.assignments) == len(ev.to_jsonl(big).splitlines())


def test_a_plan_that_places_nothing_is_a_failure_not_an_empty_success(
    folder, monkeypatch
):
    """Without this, an all-empty result is not an error: resolve returns an
    empty map, nothing is unknown or duplicated so the plan reads trustworthy,
    pack yields no units, and the run reports "0 queued · 0/0 units done" as a
    success. A green no-op is the answer a reader stops investigating."""
    class _Empty(_Agent):
        def __call__(self, folder_, prompt, **kw):
            if "already decided" in prompt:
                return True, _envelope({"assignments": [], "unsure": ["x"]})
            return super().__call__(folder_, prompt, **kw)

    monkeypatch.setattr(bf, "launch_agent", _Empty())
    plan, tail, _ = br.classify(folder, _evidence(600), agent=bf.Agent.CLAUDE,
                                existing=[], work_dir=folder / ".w")
    assert plan is None
    assert "placed" in tail


def test_a_relocated_row_is_marked_low_confidence(folder, monkeypatch):
    """It was told not to invent a slug. A row we relocated on its behalf is
    not a confident placement, and anything filtering on confidence would
    otherwise read it as one."""
    class _Inventing(_Agent):
        def __call__(self, folder_, prompt, **kw):
            if "already decided" in prompt:
                rows = [json.loads(ln) for ln in prompt.splitlines()
                        if ln.startswith('{"path"')]
                return True, _envelope({
                    "assignments": [{"path": r["path"], "project": "made-up",
                                     "confidence": "high"} for r in rows],
                    "unsure": [],
                })
            return super().__call__(folder_, prompt, **kw)

    monkeypatch.setattr(bf, "launch_agent", _Inventing())
    plan, _, _ = br.classify(folder, _evidence(600), agent=bf.Agent.CLAUDE,
                             existing=[], work_dir=folder / ".w")
    assert plan.assignments and all(a.confidence == "low" for a in plan.assignments)
