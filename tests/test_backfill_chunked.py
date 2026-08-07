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
from probe.cli import backfill_prompts as bp
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


# -- the review follow-ups ---------------------------------------------------


def test_a_rollup_travels_with_its_own_directory_not_at_the_end():
    """Emitting all sampled rows first and every rollup after them is harmless
    in one prompt and ruinous once sliced: sampling stops at
    SAMPLE_BUDGET_FILES, so on a folder big enough to chunk every `dir` row
    lands in a later slice holding no sample text at all -- and is then asked to
    place tail files "with their neighbours" while holding no neighbours."""
    files = [
        ev.FileEvidence(path=f"/drive/{area}/src/f{i}.py", size=10, mtime=0,
                        tier=ev.Tier.EVIDENCE, sample="x")
        for area in ("alpha", "beta") for i in range(3)
    ] + [
        ev.FileEvidence(path=f"/drive/{area}/ckpt/step_{i}.pt", size=10, mtime=0,
                        tier=ev.Tier.TAIL)
        for area in ("alpha", "beta") for i in range(4)
    ]
    e = ev.Evidence(root="/drive", files=files, clusters=[],
                    sampled_files=6, sampled_bytes=6)
    rows = [json.loads(ln) for ln in ev.to_jsonl(e).splitlines()]

    # Every rollup sits AMONG rows sharing its own top-level directory -- on
    # either side, since a directory's rollup may sort before or after its
    # sampled siblings depending on the names. What must not happen is every
    # rollup ending up in one block at the end.
    for i, row in enumerate(rows):
        if "dir" not in row:
            continue
        area = row["dir"].split("/")[0]
        window = rows[max(0, i - 4):i] + rows[i + 1:i + 5]
        assert any(n.get("path", "").startswith(area + "/") for n in window), (
            f"rollup {row['dir']} has no sampled neighbour from {area}"
        )
    # And they are not all bunched at the end.
    dir_positions = [i for i, r in enumerate(rows) if "dir" in r]
    assert min(dir_positions) < len(rows) - len(dir_positions), (
        "every rollup landed in a trailing block"
    )


def test_the_truncation_caveat_reaches_the_chunked_prompts(folder, monkeypatch):
    """The chunked route fires on exactly the folders that blow the 600-file
    sample cap, so `sample_budget_hit` is true in essentially every chunked run
    -- and the agent was never told."""
    agent = _Agent()
    monkeypatch.setattr(bf, "launch_agent", agent)
    big = _evidence(600)
    big.sample_budget_hit = True
    br.classify(folder, big, agent=bf.Agent.CLAUDE, existing=[],
                work_dir=folder / ".w")

    # BOTH passes, checked separately. Asserting only that "some chunked
    # prompt" carries it let the survey pass lose it silently while the assign
    # pass kept it -- and the survey pass is the one deciding what the folder
    # even contains.
    surveys = [p for p in agent.prompts if "work out what is in it" in p]
    assigns = [p for p in agent.prompts if "already decided" in p]
    assert surveys and assigns
    assert all("sample budget was reached" in p for p in surveys), "survey pass"
    assert all("sample budget was reached" in p for p in assigns), "assign pass"


def test_a_failed_pass_reports_what_the_agent_actually_said(folder, monkeypatch):
    """Substituting "slice 1 of 3 could not be read" hid every message worth
    acting on -- "`claude` is not on PATH", the confinement refusal, a
    timeout -- behind a sentence naming only where it happened."""
    def dying(folder_, prompt, **kw):
        return False, "`claude` is not on PATH — install Claude Code"

    monkeypatch.setattr(bf, "launch_agent", dying)
    _, tail, _ = br.classify(folder, _evidence(600), agent=bf.Agent.CLAUDE,
                             existing=[], work_dir=folder / ".w")
    assert "not on PATH" in tail


def test_a_transient_pass_failure_is_retried(folder, monkeypatch):
    """Losing a forty-slice run to one dropped connection, after an hour of
    successful slices, is the expensive outcome."""
    attempts: list[int] = []

    class _Flaky(_Agent):
        def __call__(self, folder_, prompt, **kw):
            if "work out what is in it" in prompt:
                attempts.append(1)
                if len(attempts) == 1:
                    return False, "connection reset"
            return super().__call__(folder_, prompt, **kw)

    monkeypatch.setattr(bf, "launch_agent", _Flaky())
    plan, _, _ = br.classify(folder, _evidence(600), agent=bf.Agent.CLAUDE,
                             existing=[], work_dir=folder / ".w")
    assert plan is not None, "one transient failure killed the whole run"
    assert len(attempts) > 1


def test_slice_summaries_are_dropped_whole_never_cut_mid_object():
    """A fixed character slice lands inside an object: the naming pass then
    sees a malformed tail, silently never learns what the last slices held, and
    names projects those slices' files are afterwards forced into."""
    findings = [{"slice": i, "work": "w" * 5_000, "evidence": []} for i in range(200)]
    text, dropped = br._fit_findings(findings)
    assert dropped > 0, "this fixture was meant to overflow"
    # VALID JSON is the property the character slice broke: it cut inside an
    # object, so the naming pass saw a malformed tail and simply never learned
    # what the last slices held.
    kept = json.loads(text)
    assert len(kept) == len(findings) - dropped
    # Every entry that survived is intact, not a prefix of itself.
    assert all(len(k["work"]) == 5_000 for k in kept)
    assert [k["slice"] for k in kept] == list(range(len(kept)))


def test_the_naming_pass_carries_a_reviewer_correction():
    prompt = bp.name_projects(root="/r", findings_json="[]", existing=[],
                              feedback="lockfiles are not research")
    assert "lockfiles are not research" in prompt
    assert "outranks" in prompt


def test_a_correction_on_a_chunked_folder_reruns_the_chunked_route(
    folder, monkeypatch
):
    """`revise` would start cold and be told to "re-read the evidence" -- on a
    folder just measured as too big to read in one prompt. It would answer from
    nothing, and because nothing is unknown or duplicated that plan reads as
    trustworthy and REPLACES the good one."""
    monkeypatch.setattr(bf, "launch_agent", _Agent())
    monkeypatch.setattr(br, "run_units", lambda *a, **k: [])
    monkeypatch.setattr(br, "enqueue_manifests", lambda *a, **k: (0, []))
    monkeypatch.setattr(br, "ensure_projects", lambda c, p, a: (["alpha"], []))
    monkeypatch.setattr(br.evidence_mod, "gather", lambda f: _evidence(600))

    revises: list = []
    monkeypatch.setattr(br, "revise", lambda *a, **k: revises.append(1) or (None, "", None))
    chunked: list = []
    real = br.classify_chunked
    monkeypatch.setattr(br, "classify_chunked",
                        lambda *a, **k: chunked.append(k.get("feedback")) or real(*a, **k))

    answers = iter(["merge the two projects", ""])
    # `tui` is imported inside `execute`, so the module itself is patched.
    from probe.cli import tui
    monkeypatch.setattr(tui, "page",
                        lambda lines, prompt=None: next(answers, "") if prompt else "")
    monkeypatch.setattr(tui, "interactive", lambda: True)

    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def list_projects(self): return []

    br.execute(client_factory=_C, folder=folder, agent=bf.Agent.CLAUDE)
    assert revises == [], "the single-shot reviser must not touch a chunked folder"
    assert "merge the two projects" in chunked


def test_tags_survive_the_chunked_route(folder, monkeypatch):
    """`name_projects` asks for them and `_plan_from` parses them on the other
    route; dropping them made the two paths produce different Plans from the
    same answer."""
    class _Tagged(_Agent):
        def __call__(self, folder_, prompt, **kw):
            if "deciding how one research folder" in prompt:
                return True, _envelope({
                    "projects": [{"slug": "alpha", "name": "a", "description": "d",
                                  "tags": ["proteins", "rl"]}],
                    "summary": "s",
                })
            return super().__call__(folder_, prompt, **kw)

    monkeypatch.setattr(bf, "launch_agent", _Tagged(projects=("alpha",)))
    plan, _, _ = br.classify(folder, _evidence(600), agent=bf.Agent.CLAUDE,
                             existing=[], work_dir=folder / ".w")
    assert plan.projects[0].tags == ["proteins", "rl"]
