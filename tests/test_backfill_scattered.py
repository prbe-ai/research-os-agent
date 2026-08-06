"""The motivating case, end to end: one drive, two lines of work, shared data.

This is the folder shape from the 2026-08-05 Anthrogen check-in -- several
researchers' directories under one root, a dataset both of them use, and
checkpoints that carry no text identifying anything. It is the case that makes
folder-level grouping wrong, so it gets a test rather than a docstring.

No agent runs here. The classification is a fixture, because what is under test
is what the deterministic halves do AROUND the agent: whether the walk catches
what the model left out, whether inheritance places it sensibly, and whether
anything can go missing between the two.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from probe.cli import backfill
from probe.cli import backfill_evidence as ev
from probe.cli import backfill_plan as bp

BURST = 200_000  # seconds between the two lines of work


@pytest.fixture
def drive(tmp_path):
    base = time.time() - 500_000
    spec = [
        ("michael/odyssey/README.md", "# Odyssey infill v3\nFSQ structure ablation.", 0),
        ("michael/odyssey/train.py", "import torch\n# odyssey infill", 5),
        ("michael/odyssey/config.yaml", "lr: 3e-4\nmodel: odyssey", 8),
        ("michael/odyssey/ckpt/step_4000.pt", "W" * 5000, 30),
        ("shared/data/rows.parquet", "B" * 9000, 100),
        ("shared/data/schema.json", '{"cols":["seq","label"]}', 105),
        ("xian/attn/notes.md", "standard attention baseline, consensus paper", BURST),
        ("xian/attn/eval.py", "import numpy  # attention baseline", BURST + 5),
        ("xian/attn/results.csv", "model,acc\nbaseline,0.81\n", BURST + 9),
    ]
    for rel, txt, off in spec:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(txt)
        os.utime(p, (base + off, base + off))
    for noise in ("__pycache__/x.pyc", ".git/HEAD"):
        p = tmp_path / noise
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("noise")
    return tmp_path


def _classification() -> str:
    """A realistic answer: the readable files placed, the shared schema placed
    but flagged, and the two binaries never mentioned at all."""
    payload = {
        "projects": [
            {"slug": "odyssey-infill-v3", "name": "Odyssey Infill v3", "description": "FSQ ablation"},
            {"slug": "attention-baseline", "name": "Attention Baseline", "description": "baseline"},
        ],
        "assignments": [
            {"path": "michael/odyssey/README.md", "project": "odyssey-infill-v3", "confidence": "high"},
            {"path": "michael/odyssey/train.py", "project": "odyssey-infill-v3", "confidence": "high"},
            {"path": "michael/odyssey/config.yaml", "project": "odyssey-infill-v3", "confidence": "high"},
            {"path": "xian/attn/notes.md", "project": "attention-baseline", "confidence": "high"},
            {"path": "xian/attn/eval.py", "project": "attention-baseline", "confidence": "high"},
            {"path": "xian/attn/results.csv", "project": "attention-baseline", "confidence": "high"},
            {"path": "shared/data/schema.json", "project": "odyssey-infill-v3", "confidence": "low"},
        ],
        "unsure": ["shared/data/schema.json"],
        "summary": "Two lines of work sharing one dataset.",
    }
    return json.dumps({"type": "result", "result": f"done\n{json.dumps(payload)}"})


def test_the_walk_and_the_census_agree_on_this_drive(drive):
    assert ev.gather(drive).total_files == backfill.scan(drive).files == 9


def test_the_two_lines_of_work_separate_by_when_they_were_written(drive):
    """Directories do not say Michael and Xian are different work. The clock
    does: the bursts are days apart."""
    clusters = ev.gather(drive).clusters
    assert len(clusters) == 2
    assert sorted(len(c.paths) for c in clusters) == [3, 6]


def test_binaries_are_not_sampled_but_are_still_counted(drive):
    e = ev.gather(drive)
    tail = {f.path.rsplit("/", 1)[-1] for f in e.files if f.tier is ev.Tier.TAIL}
    assert tail == {"step_4000.pt", "rows.parquet"}
    assert e.sampled_files == 7 and e.total_files == 9


def test_files_the_classifier_never_mentioned_are_caught_by_the_walk(drive):
    """The model is never the authority on how many files exist."""
    e = ev.gather(drive)
    disc = bp.reconcile_assignments(e, bp.parse(_classification()))
    assert sorted(disc.missing) == [
        "michael/odyssey/ckpt/step_4000.pt",
        "shared/data/rows.parquet",
    ]
    assert disc.trustworthy, "an omission is recoverable, unlike a hallucination"
    assert not disc.clean, "but it is still reported, never silently patched"


def test_a_checkpoint_inherits_from_the_work_it_sits_beside(drive):
    e = ev.gather(drive)
    assigned, _ = bp.resolve(e, bp.parse(_classification()))
    assert assigned["michael/odyssey/ckpt/step_4000.pt"] == "odyssey-infill-v3"


def test_shared_data_follows_the_file_that_was_placed_beside_it(drive):
    """`rows.parquet` says nothing about itself; `schema.json` next to it does."""
    e = ev.gather(drive)
    assigned, _ = bp.resolve(e, bp.parse(_classification()))
    assert assigned["shared/data/rows.parquet"] == assigned["shared/data/schema.json"]


def test_nothing_on_the_drive_ends_up_unplaced(drive):
    e = ev.gather(drive)
    assigned, _ = bp.resolve(e, bp.parse(_classification()))
    assert set(assigned) == set(bp.relative_paths(e))


def test_every_file_lands_in_exactly_one_unit_and_no_unit_mixes_projects(drive):
    e = ev.gather(drive)
    assigned, _ = bp.resolve(e, bp.parse(_classification()))
    units = bp.pack(e, assigned)
    packed = [p for u in units for p in u.paths]
    assert sorted(packed) == sorted(assigned)
    assert len(packed) == len(set(packed))
    for u in units:
        assert {assigned[p] for p in u.paths} == {u.project}


def test_the_shared_directory_is_what_the_reviewer_is_shown_first(drive):
    """It is the decision most likely to be wrong and the one lateral move
    exists to undo, so it must not be buried."""
    from probe.cli import backfill_run as br

    e = ev.gather(drive)
    plan = bp.parse(_classification())
    assigned, disc = bp.resolve(e, plan)
    text = "\n".join(br.describe_plan(e, plan, assigned, disc))
    least = text.split("Least certain")[1]
    assert "shared/data/schema.json" in least
