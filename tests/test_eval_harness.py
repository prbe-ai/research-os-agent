"""The eval harness has to be right, or its number is worse than no number.

An eval that silently mis-scores produces a figure people quote. These cover the
scoring logic, which is the part that can be wrong without anyone noticing --
the runner needs a live endpoint and is exercised by hand.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import yaml

_EVAL = Path(__file__).resolve().parent.parent / "evals" / "instructions"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, _EVAL / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_negative_control_tasks_expect_no_tool_call():
    """Instructions that make an agent reach indiscriminately are as broken as
    ones that never do, so the task set must contain both directions."""
    tasks = yaml.safe_load((_EVAL / "tasks.yaml").read_text())
    negatives = [t for t in tasks if not t.get("expect_first_tool")]
    assert len(negatives) >= 2, "no negative controls: over-reaching would score as success"
    ids = {t["id"] for t in negatives}
    assert "source-code-question" in ids  # NOT a research-tool question
    assert "team-discussion-question" in ids  # the other server's corpus


def test_every_task_states_the_miss_and_the_reason():
    """A task whose failure mode is unstated cannot be debugged when it regresses."""
    tasks = yaml.safe_load((_EVAL / "tasks.yaml").read_text())
    assert len(tasks) >= 10
    for task in tasks:
        assert task.get("miss"), f"{task['id']} does not say what a miss looks like"
        assert task.get("why"), f"{task['id']} does not say why it matters"


def test_scoring_treats_a_negative_control_correctly():
    run = _load("run")
    negative = {"id": "x", "expect_first_tool": []}
    positive = {"id": "y", "expect_first_tool": ["browse_research"]}
    # Calling nothing is CORRECT on a negative control and wrong on a positive one.
    assert run._correct(negative, None) is True
    assert run._correct(negative, "search_knowledge") is False
    assert run._correct(positive, None) is False
    assert run._correct(positive, "browse_research") is True


def test_scorer_reports_an_interval_not_just_a_rate():
    """50 runs is a small sample. A bare rate invites over-reading it."""
    score = _load("score")
    records = [{"arm": "baseline", "task_id": "t", "correct": i < 25} for i in range(50)]
    report = score.score(records)
    arm = report["arms"]["baseline"]
    assert arm["rate"] == 0.5
    low, high = arm["ci95"]
    assert low < 0.5 < high
    # Wilson, not normal-approx: an all-hits sample must NOT report [1.0, 1.0].
    perfect = score.score([{"arm": "a", "task_id": "t", "correct": True}] * 50)
    assert perfect["arms"]["a"]["ci95"][0] < 1.0


def test_scorer_flags_overlapping_intervals(capsys):
    """The honest read of a null result, printed rather than left to the reader."""
    score = _load("score")
    records = (
        [{"arm": "baseline", "task_id": "t", "correct": i < 25} for i in range(50)]
        + [{"arm": "instructions_only", "task_id": "t", "correct": i < 28} for i in range(50)]
    )
    import json as _json
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        for r in records:
            fh.write(_json.dumps(r) + "\n")
        path = fh.name
    import sys

    argv = sys.argv
    sys.argv = ["score.py", path]
    try:
        score.main()
    finally:
        sys.argv = argv
    assert "OVERLAP" in capsys.readouterr().out
