"""Every line of every REAL captured session must translate.

These fixtures came out of `~/.pi/agent/sessions/` after real pi runs. They
are the reason this suite catches producer/consumer drift: hand-authored
fixtures agree with whatever the consumer expects, which is exactly how the
Harbor sandbox hash-mode mismatch survived review.
"""
import json
from pathlib import Path

import pytest

from tap.pi_sanitize import sanitize_event

FIXTURES = sorted((Path(__file__).parent / "fixtures" / "pi").glob("*.jsonl"))


def test_fixtures_exist():
    assert FIXTURES, "generate real pi sessions before running this suite"


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_every_line_translates_without_raising(path):
    for line in path.read_text().splitlines():
        if line.strip():
            assert sanitize_event(json.loads(line)) is not None


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_no_real_line_lands_in_the_unknown_bucket(path):
    """Unknown passthrough is for FORKS. If upstream pi emits a type we do not
    handle, that is a gap in this translator, not a fork."""
    unknown = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        out = sanitize_event(json.loads(line))
        for one in (out if isinstance(out, list) else [out]):
            if str(one.get("subtype", "")).startswith("unknown:"):
                unknown.append(one["subtype"])
    assert not unknown, f"unhandled upstream entry types: {sorted(set(unknown))}"


def _edit_arg_shapes() -> set[str]:
    """Which `edit` argument shapes the real corpus actually contains."""
    shapes: set[str] = set()
    for path in FIXTURES:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("type") != "message":
                continue
            for block in (entry.get("message") or {}).get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "toolCall":
                    continue
                if block.get("name") != "edit":
                    continue
                args = block.get("arguments") or {}
                if isinstance(args.get("edits"), list):
                    shapes.add("array")
                elif "oldText" in args or "newText" in args:
                    shapes.add("flat")
    return shapes


def test_the_corpus_contains_both_live_edit_schemas():
    """pi's `edit` schema CHANGED and both shapes are in the wild.

    Sessions published in early 2026 use flat {path, oldText, newText}; pi
    0.84.3 emits {path, edits: [{oldText, newText}]}. Both were captured from
    real producers, not written by hand.

    This guards the failure that motivated handling both: a translator built
    against only the current schema passes every unit test and records ZERO
    edit stats against historical sessions, so a backfill reports success and
    captures nothing. If this assertion ever fails, the corpus lost a shape
    and that regression is no longer covered.
    """
    assert _edit_arg_shapes() == {"array", "flat"}


def test_both_edit_schemas_produce_edit_stats():
    """Neither shape may silently yield no counts."""
    measured = []
    for path in FIXTURES:
        for line in path.read_text().splitlines():
            if not line.strip():
                continue
            out = sanitize_event(json.loads(line))
            for one in out if isinstance(out, list) else [out]:
                for block in (one.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("name") == "edit":
                        measured.append(block.get("edit_stats"))
    assert measured, "corpus has no edit tool calls to measure"
    assert all(s and s["op"] == "edit" for s in measured), measured


def test_fork_lineage_survives_translation():
    """/fork and /clone write a NEW file naming the original. Without this a
    forked session looks like an unrelated one starting mid-conversation."""
    parents = []
    for path in FIXTURES:
        header = json.loads(path.read_text().splitlines()[0])
        if header.get("parentSession"):
            parents.append(sanitize_event(header)["_pi_extras"].get("parent_session"))
    assert parents, "corpus has no forked session to exercise lineage"
    assert all(p and p.endswith(".jsonl") for p in parents), parents
