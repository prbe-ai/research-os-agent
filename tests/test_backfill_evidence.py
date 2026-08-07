"""ENUMERATE, deepened: Tier 1 facts, Tier 2 samples, Tier 3 by inheritance.

The load-bearing test in here is the LAST one. The evidence walk and the census
walk must agree about what a folder contains, or the denominator the whole
feature is built on is checking a different folder than the one the agent
classified.
"""

from __future__ import annotations

import json
import time

import pytest

from probe.cli import backfill
from probe.cli import backfill_evidence as ev


def _write(root, rel, text="x", *, mtime=None):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        p.write_bytes(text)
    else:
        p.write_text(text)
    if mtime is not None:
        import os

        os.utime(p, (mtime, mtime))
    return p


# -- tiering ----------------------------------------------------------------


@pytest.mark.parametrize(
    "name,size,expected",
    [
        ("README", 10, ev.Tier.EVIDENCE),
        ("readme.md", 10, ev.Tier.EVIDENCE),
        ("config.yaml", 10, ev.Tier.EVIDENCE),
        ("train.py", 10, ev.Tier.EVIDENCE),
        ("results.csv", 10, ev.Tier.EVIDENCE),
        ("model.safetensors", 10, ev.Tier.TAIL),
        ("step_4000.pt", 10, ev.Tier.TAIL),
        ("plot.png", 10, ev.Tier.TAIL),
        ("run-abc.wandb", 10, ev.Tier.TAIL),
        ("mystery", 10, ev.Tier.TAIL),
    ],
)
def test_tier_is_decided_by_name_alone(name, size, expected):
    assert ev.tier_for(name, size) is expected


def test_a_huge_text_file_is_tail_however_it_is_named():
    """A 10GB shard is a dataset whatever its extension claims, and its head
    would spend the sample budget describing a bracket."""
    assert ev.tier_for("shard.json", 10) is ev.Tier.EVIDENCE
    assert ev.tier_for("shard.json", backfill.REFERENCE_ABOVE_BYTES) is ev.Tier.TAIL


# -- tier 1 walk -------------------------------------------------------------


def test_walk_prunes_the_same_noise_the_census_does(tmp_path):
    _write(tmp_path, "keep.py")
    _write(tmp_path, "__pycache__/skip.pyc")
    _write(tmp_path, ".git/config")
    _write(tmp_path, "node_modules/pkg/index.js")
    found = {f.path.rsplit("/", 1)[-1] for f in ev.walk(tmp_path)}
    assert found == {"keep.py"}


def test_a_file_that_cannot_be_stat_ed_still_counts(tmp_path, monkeypatch):
    """It counts in the census too. An evidence list that skips it would make
    the two disagree, which is the one thing this module must not do."""
    _write(tmp_path, "a.py")
    real = ev.Path.stat

    def boom(self, *a, **k):
        if self.name == "a.py":
            raise OSError("gone")
        return real(self, *a, **k)

    monkeypatch.setattr(ev.Path, "stat", boom)
    files = ev.walk(tmp_path)
    assert len(files) == 1
    assert files[0].size == 0 and files[0].mtime == 0.0


# -- clustering --------------------------------------------------------------


def test_mtime_clusters_separate_two_runs(tmp_path):
    base = time.time() - 100_000
    for i in range(3):
        _write(tmp_path, f"run_a/{i}.txt", mtime=base + i * 10)
    for i in range(3):
        _write(tmp_path, f"run_b/{i}.txt", mtime=base + ev.CLUSTER_GAP_SECONDS * 3 + i * 10)
    clusters = ev.cluster_by_mtime(ev.walk(tmp_path))
    assert len(clusters) == 2
    assert all(len(c.paths) == 3 for c in clusters)


def test_clustering_crosses_directories(tmp_path):
    """The whole point: files written together are one burst even when the
    tree puts them far apart."""
    base = time.time() - 100_000
    _write(tmp_path, "michael/loss.csv", mtime=base)
    _write(tmp_path, "shared/data/config.yaml", mtime=base + 5)
    _write(tmp_path, "xian/notes.md", mtime=base + 9)
    clusters = ev.cluster_by_mtime(ev.walk(tmp_path))
    assert len(clusters) == 1
    assert len(clusters[0].paths) == 3


def test_files_with_no_usable_mtime_are_left_out_not_pooled_at_the_epoch(tmp_path):
    _write(tmp_path, "a.txt", mtime=time.time())
    files = ev.walk(tmp_path)
    files.append(ev.FileEvidence(path="/nope", size=0, mtime=0.0, tier=ev.Tier.TAIL))
    clusters = ev.cluster_by_mtime(files)
    assert len(clusters) == 1
    assert "/nope" not in clusters[0].paths


# -- tier 2 sampling ---------------------------------------------------------


def test_binary_is_rejected_by_nul_sniffing_not_by_suffix(tmp_path):
    _write(tmp_path, "looks_like_text.py", b"import os\x00\x00binary")
    assert ev.read_head(str(tmp_path / "looks_like_text.py")) is None


def test_one_stray_byte_does_not_lose_a_config(tmp_path):
    _write(tmp_path, "config.yaml", b"lr: 3e-4\n\xff\nseed: 7")
    head = ev.read_head(str(tmp_path / "config.yaml"))
    assert head is not None and "lr: 3e-4" in head and "seed: 7" in head


def test_a_binary_file_named_like_evidence_is_reclassified_not_sampled(tmp_path):
    _write(tmp_path, "train.py", b"\x00\x01\x02")
    sampled, n, _, _ = ev.sample(ev.walk(tmp_path))
    assert n == 0
    assert sampled[0].tier is ev.Tier.TAIL
    assert sampled[0].sample is None


def test_sampling_is_smallest_first_so_a_fixed_budget_buys_the_most_files(tmp_path):
    _write(tmp_path, "big.md", "b" * 4000)
    _write(tmp_path, "small.md", "s")
    sampled, n, _, hit = ev.sample(ev.walk(tmp_path), budget_files=1)
    assert n == 1 and hit is True
    got = {f.path.rsplit("/", 1)[-1]: f.sample for f in sampled}
    assert got["small.md"] == "s"
    assert got["big.md"] is None


def test_the_file_budget_reports_itself(tmp_path):
    for i in range(5):
        _write(tmp_path, f"c{i}.yaml", f"k: {i}")
    _, n, _, hit = ev.sample(ev.walk(tmp_path), budget_files=2)
    assert (n, hit) == (2, True)


def test_the_byte_budget_reports_itself(tmp_path):
    for i in range(5):
        _write(tmp_path, f"c{i}.yaml", "k" * 500)
    _, _, used, hit = ev.sample(ev.walk(tmp_path), budget_bytes=600)
    assert hit is True and used <= 1200


def test_an_untouched_budget_does_not_claim_to_be_hit(tmp_path):
    _write(tmp_path, "a.md", "hello")
    _, n, _, hit = ev.sample(ev.walk(tmp_path))
    assert (n, hit) == (1, False)


def test_a_head_of_something_much_larger_is_flagged_truncated(tmp_path):
    _write(tmp_path, "train.log", "L" * (ev.SAMPLE_BYTES * 4))
    sampled, _, _, _ = ev.sample(ev.walk(tmp_path))
    assert sampled[0].truncated is True
    assert len(sampled[0].sample) <= ev.SAMPLE_BYTES


# -- the prompt payload ------------------------------------------------------


def test_jsonl_paths_are_relative_to_the_root(tmp_path):
    _write(tmp_path, "michael/train.py", "import torch")
    rows = [json.loads(x) for x in ev.to_jsonl(ev.gather(tmp_path)).splitlines()]
    assert rows[0]["path"] == "michael/train.py"
    assert not rows[0]["path"].startswith("/")


def test_tail_files_are_rolled_up_per_directory_not_listed_one_by_one(tmp_path):
    """A tail file carries no text identifying anything -- that is the tier's
    definition -- so a row each says exactly what one row per directory says and
    costs thousands of times more. Listing them individually put a 200k-file
    drive past any context window."""
    _write(tmp_path, "readme.md", "the odyssey ablation")
    for i in range(40):
        _write(tmp_path, f"ckpt/step_{i}.pt", "W" * 10)
    rows = [json.loads(x) for x in ev.to_jsonl(ev.gather(tmp_path)).splitlines()]
    assert len(rows) == 2, "one evidence row, one rollup row"

    evidence_row = next(r for r in rows if r.get("tier") == "evidence")
    assert evidence_row["path"] == "readme.md"
    assert evidence_row["sample"] == "the odyssey ablation"

    rollup = next(r for r in rows if "dir" in r)
    assert rollup["dir"] == "ckpt"
    assert rollup["files"] == 40
    assert rollup["bytes"] == 400
    assert rollup["ext"] == [".pt"]
    assert "sample" not in rollup


def test_a_rollup_carries_the_time_span_so_bursts_stay_visible(tmp_path):
    import os
    import time

    base = time.time() - 90_000
    for i in range(3):
        p = _write(tmp_path, f"ckpt/s{i}.pt", "W")
        os.utime(p, (base + i * 600, base + i * 600))
    rollup = next(
        json.loads(x)
        for x in ev.to_jsonl(ev.gather(tmp_path)).splitlines()
        if "dir" in json.loads(x)
    )
    lo, hi = rollup["mtime_span"]
    assert hi - lo == 1200


def test_tail_files_at_the_root_roll_up_under_a_dot(tmp_path):
    _write(tmp_path, "model.pt", "W")
    rollup = next(
        json.loads(x)
        for x in ev.to_jsonl(ev.gather(tmp_path)).splitlines()
        if "dir" in json.loads(x)
    )
    # "." and not "": an empty string is falsy, and every consumer that checks
    # truthiness drops it -- which made root-level checkpoints, the most common
    # place a researcher leaves them, silently unassignable.
    assert rollup["dir"] == "."
    assert rollup["files"] == 1


def test_the_rollup_keeps_the_prompt_inside_a_context_window(tmp_path):
    """The regression guard. 3,000 tail files in one directory must not produce
    3,000 rows -- that shape is what made the classify pass unrunnable."""
    _write(tmp_path, "readme.md", "hi")
    for i in range(3000):
        _write(tmp_path, f"ckpt/s{i:05d}.pt", "W")
    text = ev.to_jsonl(ev.gather(tmp_path))
    assert len(text.splitlines()) == 2
    assert len(text) < 4000, "a 3,001-file folder must not cost more than a few KB"


def test_gather_describes_what_it_actually_did(tmp_path):
    _write(tmp_path, "readme.md", "hi")
    _write(tmp_path, "model.pt", "w")
    text = ev.gather(tmp_path).describe()
    assert "2 files" in text and "1 evidence-bearing, 1 sampled" in text


def test_a_budget_stop_is_visible_in_the_description(tmp_path):
    for i in range(5):
        _write(tmp_path, f"c{i}.yaml", "k")
    got = ev.gather(tmp_path)
    got.sample_budget_hit = True
    assert "sample budget reached" in got.describe()


# -- the invariant that matters ---------------------------------------------


def test_the_evidence_walk_and_the_census_count_the_same_folder(tmp_path):
    """If these disagree, the denominator is checking a different folder than
    the one the agent classified, and the reconcile is meaningless."""
    _write(tmp_path, "readme.md", "hi")
    _write(tmp_path, "michael/train.py", "import torch")
    _write(tmp_path, "michael/ckpt/step_10.pt", "w" * 100)
    _write(tmp_path, "shared/data/rows.csv", "a,b\n1,2\n")
    _write(tmp_path, "__pycache__/x.pyc", "noise")
    _write(tmp_path, ".hidden.txt", "noise")
    _write(tmp_path, ".git/HEAD", "noise")

    census = backfill.scan(tmp_path)
    evidence = ev.gather(tmp_path)

    assert evidence.total_files == census.files
    assert evidence.total_bytes == census.bytes
