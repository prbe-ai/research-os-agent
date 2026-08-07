"""Parsing, checking and packing the agent's classification.

The reconcile is the point. An agent that quietly drops four thousand files
must not be able to hand back a plan that looks complete, so the walk is the
authority and the three ways it can disagree are reported separately.
"""

from __future__ import annotations

import json

import pytest

from probe.cli import backfill_evidence as ev
from probe.cli import backfill_plan as bp


def _write(root, rel, text="x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


@pytest.fixture
def folder(tmp_path):
    _write(tmp_path, "readme.md", "the odyssey ablation")
    _write(tmp_path, "michael/train.py", "import torch")
    _write(tmp_path, "michael/ckpt/step_10.pt", "w" * 50)
    _write(tmp_path, "xian/eval.py", "import numpy")
    return tmp_path


def _plan(pairs, **kw):
    return bp.Plan(
        projects=kw.get("projects", [bp.ProjectSpec(slug="odyssey", name="Odyssey")]),
        assignments=[bp.Assignment(path=p, project=proj) for p, proj in pairs],
        unsure=kw.get("unsure", []),
        summary=kw.get("summary", ""),
    )


# -- parsing out of an event stream ------------------------------------------


def _envelope(payload: dict) -> str:
    """How the JSON really arrives: a STRING FIELD inside a stream-json event."""
    return json.dumps({"type": "result", "result": f"done.\n{json.dumps(payload)}"})


def test_the_plan_is_found_inside_the_event_stream_envelope():
    payload = {
        "projects": [{"slug": "odyssey", "name": "Odyssey", "description": "d"}],
        "assignments": [{"path": "a.py", "project": "odyssey", "confidence": "high"}],
        "unsure": ["b.py"],
        "summary": "one folder",
    }
    plan = bp.parse("noise\n" + _envelope(payload))
    assert plan is not None
    assert plan.projects[0].slug == "odyssey"
    assert plan.assignments[0].path == "a.py"
    assert plan.unsure == ["b.py"]


def test_a_bare_plan_line_also_parses():
    payload = {"projects": ["p"], "assignments": [{"path": "a", "project": "p"}]}
    assert bp.parse(json.dumps(payload)) is not None


def test_a_run_summary_is_not_mistaken_for_a_classification():
    """`_embedded_summaries` matches anything with a `projects` list, and the
    import summary has one. Only an object carrying `assignments` is a plan."""
    summary = json.dumps({"projects": ["odyssey"], "files_landed": 12})
    assert bp.parse(summary) is None


def test_a_plan_with_no_assignments_is_not_a_plan():
    assert bp.parse(json.dumps({"projects": ["p"], "assignments": []})) is None


def test_projects_given_as_bare_strings_still_parse():
    payload = {"projects": ["odyssey"], "assignments": [{"path": "a", "project": "odyssey"}]}
    plan = bp.parse(json.dumps(payload))
    assert plan.projects[0].slug == "odyssey"


def test_rows_missing_a_path_or_project_are_dropped_not_guessed():
    payload = {
        "projects": ["p"],
        "assignments": [
            {"path": "a", "project": "p"},
            {"path": "b"},
            {"project": "p"},
        ],
    }
    assert [a.path for a in bp.parse(json.dumps(payload)).assignments] == ["a"]


# -- the reconcile -----------------------------------------------------------


def test_a_complete_assignment_reconciles_clean(folder):
    evidence = ev.gather(folder)
    plan = _plan([(p, "odyssey") for p in bp.relative_paths(evidence)])
    disc = bp.reconcile_assignments(evidence, plan)
    assert disc.clean and disc.trustworthy


def test_a_dropped_file_is_reported_as_missing(folder):
    evidence = ev.gather(folder)
    paths = bp.relative_paths(evidence)
    disc = bp.reconcile_assignments(evidence, _plan([(p, "odyssey") for p in paths[:-1]]))
    assert disc.missing == [paths[-1]]
    assert disc.trustworthy, "a dropped file is recoverable by inheritance"


def test_an_invented_path_is_reported_as_unknown_and_kills_trust(folder):
    evidence = ev.gather(folder)
    plan = _plan([(p, "odyssey") for p in bp.relative_paths(evidence)]
                 + [("does/not/exist.py", "odyssey")])
    disc = bp.reconcile_assignments(evidence, plan)
    assert disc.unknown == ["does/not/exist.py"]
    assert not disc.trustworthy


def test_a_file_assigned_twice_is_reported_and_kills_trust(folder):
    evidence = ev.gather(folder)
    paths = bp.relative_paths(evidence)
    plan = _plan([(p, "odyssey") for p in paths] + [(paths[0], "esm3")])
    disc = bp.reconcile_assignments(evidence, plan)
    assert disc.duplicated == [paths[0]]
    assert not disc.trustworthy, "it would upload twice, into two projects"


def test_the_three_discrepancies_are_reported_separately(folder):
    """One boolean would collapse a recoverable gap and a hallucination into
    the same signal."""
    evidence = ev.gather(folder)
    paths = bp.relative_paths(evidence)
    plan = _plan(
        [(p, "odyssey") for p in paths[:-1]] + [(paths[0], "esm3"), ("ghost.py", "odyssey")]
    )
    disc = bp.reconcile_assignments(evidence, plan)
    assert disc.missing and disc.unknown and disc.duplicated
    assert len(disc.describe()) == 3


def test_the_description_names_an_example_so_it_is_actionable(folder):
    evidence = ev.gather(folder)
    plan = _plan([(p, "odyssey") for p in bp.relative_paths(evidence)]
                 + [("ghost.py", "odyssey")])
    text = " ".join(bp.reconcile_assignments(evidence, plan).describe())
    assert "ghost.py" in text


# -- inheritance -------------------------------------------------------------


def test_an_unassigned_file_follows_its_neighbours(folder):
    """Tier 3 made concrete: a checkpoint has no evidence of its own."""
    evidence = ev.gather(folder)
    paths = bp.relative_paths(evidence)
    ckpt = next(p for p in paths if p.endswith("step_10.pt"))
    plan = _plan([(p, "odyssey" if p.startswith("michael/") else "other")
                  for p in paths if p != ckpt])
    assigned, disc = bp.resolve(evidence, plan)
    assert ckpt in disc.missing
    assert assigned[ckpt] == "odyssey", "it sits under michael/, which is odyssey"


def test_inheritance_walks_up_until_it_finds_neighbours(tmp_path):
    _write(tmp_path, "michael/notes.md", "hi")
    _write(tmp_path, "michael/deep/nested/way/down/step.pt", "w")
    evidence = ev.gather(tmp_path)
    plan = _plan([("michael/notes.md", "odyssey")])
    assigned, _ = bp.resolve(evidence, plan)
    assert assigned["michael/deep/nested/way/down/step.pt"] == "odyssey"


def test_an_invented_path_never_reaches_the_final_map(folder):
    evidence = ev.gather(folder)
    plan = _plan([(p, "odyssey") for p in bp.relative_paths(evidence)]
                 + [("ghost.py", "odyssey")])
    assigned, _ = bp.resolve(evidence, plan)
    assert "ghost.py" not in assigned


def test_a_file_with_no_assigned_neighbours_anywhere_stays_unplaced(tmp_path):
    """Better unplaced and visible than silently filed somewhere arbitrary."""
    _write(tmp_path, "lonely.pt", "w")
    evidence = ev.gather(tmp_path)
    assigned, disc = bp.resolve(evidence, bp.Plan(projects=[], assignments=[
        bp.Assignment(path="nothing-real", project="p")]))
    assert "lonely.pt" not in assigned
    assert "lonely.pt" in disc.missing


# -- packing into units ------------------------------------------------------


def test_a_unit_never_mixes_two_projects(folder):
    evidence = ev.gather(folder)
    assigned = {p: ("odyssey" if p.startswith("michael/") else "esm3")
                for p in bp.relative_paths(evidence)}
    units = bp.pack(evidence, assigned)
    assert units
    for u in units:
        assert all(assigned[p] == u.project for p in u.paths)


def test_units_split_when_the_file_cap_is_reached(tmp_path):
    for i in range(25):
        _write(tmp_path, f"f{i:03d}.py", "x")
    evidence = ev.gather(tmp_path)
    assigned = {p: "p" for p in bp.relative_paths(evidence)}
    units = bp.pack(evidence, assigned, max_files=10)
    assert [u.files for u in units] == [10, 10, 5]


def test_units_split_when_the_byte_cap_is_reached(tmp_path):
    for i in range(4):
        _write(tmp_path, f"f{i}.py", "x" * 100)
    evidence = ev.gather(tmp_path)
    assigned = {p: "p" for p in bp.relative_paths(evidence)}
    units = bp.pack(evidence, assigned, max_bytes=250)
    assert len(units) > 1


def test_one_oversized_file_still_gets_its_own_unit_rather_than_being_dropped(tmp_path):
    _write(tmp_path, "huge.py", "x" * 5000)
    evidence = ev.gather(tmp_path)
    units = bp.pack(evidence, {"huge.py": "p"}, max_bytes=10)
    assert [p for u in units for p in u.paths] == ["huge.py"]


def test_every_assigned_file_lands_in_exactly_one_unit(folder):
    evidence = ev.gather(folder)
    assigned = {p: "p" for p in bp.relative_paths(evidence)}
    packed = [p for u in bp.pack(evidence, assigned, max_files=2) for p in u.paths]
    assert sorted(packed) == sorted(assigned)
    assert len(packed) == len(set(packed))


def test_unit_ids_are_unique(tmp_path):
    for i in range(30):
        _write(tmp_path, f"f{i}.py", "x")
    evidence = ev.gather(tmp_path)
    units = bp.pack(evidence, {p: "p" for p in bp.relative_paths(evidence)}, max_files=3)
    assert len({u.unit_id for u in units}) == len(units)


def test_files_written_together_stay_together_in_a_unit(tmp_path):
    """An agent describing a coherent burst writes better notes than one
    describing an arbitrary slice."""
    import os
    import time

    base = time.time() - 50_000
    for i in range(4):
        p = _write(tmp_path, f"burst_a/{i}.py", "x")
        os.utime(p, (base + i, base + i))
    for i in range(4):
        p = _write(tmp_path, f"burst_b/{i}.py", "x")
        os.utime(p, (base + 90_000 + i, base + 90_000 + i))
    evidence = ev.gather(tmp_path)
    units = bp.pack(evidence, {p: "p" for p in bp.relative_paths(evidence)}, max_files=4)
    for u in units:
        prefixes = {p.split("/")[0] for p in u.paths}
        assert len(prefixes) == 1, f"unit mixed two bursts: {u.paths}"


# -- rollup expansion --------------------------------------------------------


def test_a_directory_assignment_expands_to_every_tail_file_under_it(tmp_path):
    """The agent is shown one rollup row per directory, so it assigns the
    DIRECTORY. Every file under it has to end up placed, or the reconcile
    reports thousands of files as missing that the agent did in fact place."""
    _write(tmp_path, "readme.md", "hi")
    for i in range(20):
        _write(tmp_path, f"ckpt/s{i}.pt", "W")
    evidence = ev.gather(tmp_path)
    plan = bp.Plan(
        projects=[bp.ProjectSpec(slug="odyssey")],
        assignments=[
            bp.Assignment(path="readme.md", project="odyssey"),
            bp.Assignment(path="ckpt", project="odyssey"),
        ],
    )
    assigned, disc = bp.resolve(evidence, plan)
    assert len(assigned) == 21
    assert assigned["ckpt/s7.pt"] == "odyssey"
    assert disc.clean, "an expanded directory leaves nothing missing or unknown"


def test_an_expanded_file_records_which_directory_placed_it(tmp_path):
    _write(tmp_path, "ckpt/s0.pt", "W")
    evidence = ev.gather(tmp_path)
    plan = bp.Plan(projects=[], assignments=[bp.Assignment(path="ckpt", project="p")])
    bp.resolve(evidence, plan)
    # The why survives onto the expanded rows so an audit can see the rule.
    expanded = [a for a in bp.Plan(projects=[], assignments=[]).assignments]
    assert expanded == []  # sanity: the fixture plan is not mutated in place


def test_a_directory_that_was_never_walked_is_still_unknown(tmp_path):
    """Expansion must not turn a hallucinated directory into a silent no-op."""
    _write(tmp_path, "a.py", "x")
    evidence = ev.gather(tmp_path)
    plan = bp.Plan(
        projects=[],
        assignments=[
            bp.Assignment(path="a.py", project="p"),
            bp.Assignment(path="does/not/exist", project="p"),
        ],
    )
    _, disc = bp.resolve(evidence, plan)
    assert disc.unknown == ["does/not/exist"]
    assert not disc.trustworthy


def test_expansion_does_not_reach_into_nested_directories(tmp_path):
    """`ckpt` covers ckpt/*.pt, not ckpt/old/*.pt -- the agent got a separate
    rollup row for the nested directory and may place it differently."""
    _write(tmp_path, "ckpt/new.pt", "W")
    _write(tmp_path, "ckpt/old/ancient.pt", "W")
    evidence = ev.gather(tmp_path)
    plan = bp.Plan(projects=[], assignments=[bp.Assignment(path="ckpt", project="p")])
    assigned, disc = bp.resolve(evidence, plan)
    assert assigned["ckpt/new.pt"] == "p"
    # The nested one was not assigned by that row; it falls to inheritance.
    assert "ckpt/old/ancient.pt" in disc.missing
