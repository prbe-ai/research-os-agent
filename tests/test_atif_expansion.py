"""Trajectory -> span-tree expansion (connectors.atif) — Phase 2.

The golden fixtures under tests/fixtures/atif/ are real ATIF documents from
Harbor upstream's own test suite (see the README there), so these tests prove
we parse what Harbor actually emits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from probe.connectors import atif
from probe.connectors.atif import (
    PlannedSpan,
    detect_trajectory_format,
    expand_trajectory,
    parse_atif,
    register_trajectory_parser,
    span_id_for,
)
from probe.connectors.harbor import MANIFEST_KIND, capture_trial

FIXTURES = Path(__file__).parent / "fixtures" / "atif"
GOLDEN = sorted(FIXTURES.glob("*.trajectory*.json"))


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


def _steps_recursive(doc: dict) -> int:
    n = len(doc.get("steps") or [])
    for sub in doc.get("subagent_trajectories") or []:
        n += _steps_recursive(sub)
    return n


def _calls_recursive(doc: dict) -> int:
    n = sum(len(s.get("tool_calls") or []) for s in doc.get("steps") or [])
    for sub in doc.get("subagent_trajectories") or []:
        n += _calls_recursive(sub)
    return n


# -- format detection ----------------------------------------------------------
def test_detect_format():
    assert detect_trajectory_format({"schema_version": "ATIF-v1.7"}) == "ATIF-v1.7"
    assert detect_trajectory_format({"schema": "atif@1"}) == "atif@1"
    assert detect_trajectory_format({"format": "miles-v1"}) == "miles-v1"
    assert detect_trajectory_format({"steps": []}) == "unknown"
    assert detect_trajectory_format(["not", "a", "dict"]) == "unknown"
    assert detect_trajectory_format(None) is None


# -- golden fixtures: every real ATIF file Harbor ships must parse ---------------
@pytest.mark.parametrize("path", GOLDEN, ids=[p.name for p in GOLDEN])
def test_golden_fixture_parses(path):
    doc = _load(path)
    plans = parse_atif(doc)

    turns = [p for p in plans if p.span_type == "turn" and not p.attributes.get("subagent")]
    calls = [p for p in plans if p.span_type == "tool_call"]
    subs = [p for p in plans if p.attributes.get("subagent")]
    assert len(turns) == _steps_recursive(doc)
    assert len(calls) == _calls_recursive(doc)
    assert len(subs) == len(doc.get("subagent_trajectories") or [])

    # tree integrity: every parent_path is a plan that appears EARLIER, so a
    # prefix cut can never orphan a kept child
    seen: set[str] = set()
    for plan in plans:
        assert plan.parent_path is None or plan.parent_path in seen
        seen.add(plan.path)

    # every tool_call carries its identity and joins its observation result
    for plan in calls:
        assert plan.attributes.get("function_name")
        assert plan.name == plan.attributes["function_name"]


def test_golden_content_lands_in_attributes():
    doc = _load(FIXTURES / "hello-world-invalid-json.trajectory.json")
    plans = {p.path: p for p in parse_atif(doc)}
    # step 2 has an unmatched observation (parser-error feedback) -> on the turn
    assert "parsing errors" in plans["turn/2"].attributes["observation"]
    # step 3's bash call joins its terminal output via source_call_id
    call = plans["turn/3/call/0"]
    assert call.attributes["function_name"] == "bash_command"
    assert "New Terminal Output" in call.attributes["result"]
    assert plans["turn/3"].attributes["prompt_tokens"] == 785


def test_subagent_fixture_nests_under_the_delegating_call():
    doc = _load(FIXTURES / "synthetic-subagent.trajectory.json")
    plans = {p.path: p for p in parse_atif(doc)}
    sub_root = plans["turn/2/call/0/sub/sub-a"]
    assert sub_root.parent_path == "turn/2/call/0"
    assert sub_root.attributes["subagent"] is True
    # the subagent's own steps nest under its root with their own numbering
    inner = plans["turn/2/call/0/sub/sub-a/turn/2"]
    assert inner.parent_path == "turn/2/call/0/sub/sub-a"
    inner_call = plans["turn/2/call/0/sub/sub-a/turn/2/call/0"]
    assert inner_call.attributes["function_name"] == "grep"


def test_naive_timestamp_stays_in_attributes_only():
    doc = {
        "schema_version": "ATIF-v1.7",
        "agent": {"name": "a", "version": "1"},
        "steps": [{"step_id": 1, "source": "agent", "message": "x",
                   "timestamp": "2026-07-16T10:00:00"}],  # naive -> not started_at
    }
    (plan,) = parse_atif(doc)
    assert plan.started_at is None
    assert plan.attributes["timestamp"] == "2026-07-16T10:00:00"
    doc["steps"][0]["timestamp"] = "2026-07-16T10:00:00Z"  # aware -> both
    (plan,) = parse_atif(doc)
    assert plan.started_at == "2026-07-16T10:00:00Z"


# -- deterministic ids -----------------------------------------------------------
def test_span_ids_are_deterministic_and_scoped():
    a = span_id_for("run-1", "trial-a", "turn/1")
    assert a == span_id_for("run-1", "trial-a", "turn/1")
    assert a != span_id_for("run-1", "trial-b", "turn/1")
    assert a != span_id_for("run-2", "trial-a", "turn/1")


# -- expansion through capture_trial ---------------------------------------------
def _write_atif_trial(root, fixture: str):
    root.mkdir(parents=True)
    (root / "result.json").write_text(json.dumps({
        "trial_name": "atif-trial__x1",
        "verifier_result": {"reward": 1.0},
    }))
    (root / "trajectory.json").write_text((FIXTURES / fixture).read_text())
    return root


def test_capture_expands_atif_into_the_span_tree(client, app, tmp_path):
    client.fail_open = False
    run = client.run(experiment="e", hypothesis="h", name="r")
    result = capture_trial(
        run, _write_atif_trial(tmp_path / "t", "hello-world-invalid-json.trajectory.json"),
        step_index=600, strict=True,
    )
    report = result["trajectory"]
    assert report["expanded"] is True and report["format"] == "ATIF-v1.7"
    assert report["truncated"] is False
    assert report["final_metrics"]["total_prompt_tokens"] == 2417

    spans = app.spans[run.id]
    rollout = next(s for s in spans if s["span_type"] == "rollout")
    turns = [s for s in spans if s["span_type"] == "turn"]
    calls = [s for s in spans if s["span_type"] == "tool_call"]
    assert len(turns) == 5 and len(calls) == 3
    assert report["spans"] == len(turns) + len(calls)
    # tree hangs off the rollout span, and the join key rides every node
    assert all(s["parent_span_id"] == rollout["id"] for s in turns)
    turn_ids = {s["id"] for s in turns}
    assert all(s["parent_span_id"] in turn_ids for s in calls)
    assert all(s["step_index"] == 600 for s in turns + calls)
    # manifest records the expansion outcome
    manifest = client.list_run_artifacts(run.id, kind=MANIFEST_KIND)[0]
    assert manifest["meta"]["trajectory"]["expanded"] is True
    assert manifest["meta"]["trajectory_format"] == "ATIF-v1.7"


def test_capture_no_expand_flag(client, app, tmp_path):
    client.fail_open = False
    run = client.run(experiment="e", hypothesis="h", name="r")
    result = capture_trial(
        run, _write_atif_trial(tmp_path / "t", "hello-world-timeout.trajectory.json"),
        expand=False, strict=True,
    )
    assert result["trajectory"]["expanded"] is False
    assert app.spans_upserted == 1  # just the rollout


def test_unknown_format_captures_raw_without_expansion(client, app, tmp_path):
    client.fail_open = False
    run = client.run(experiment="e", hypothesis="h", name="r")
    root = tmp_path / "t"
    root.mkdir()
    (root / "trajectory.json").write_text(json.dumps({"format": "osmosis-fork-v0", "events": []}))
    result = capture_trial(run, root, strict=True)
    assert result["trajectory"] == {"format": "osmosis-fork-v0", "expanded": False,
                                    "spans": 0, "truncated": False}
    # raw bytes still uploaded and labeled
    assert any(f["role"] == "trajectory" and f["uploaded"] for f in result["files"])


def test_forks_can_register_their_own_parser(client, app, tmp_path):
    def parse_osmosis(doc):
        return [PlannedSpan(path=f"turn/{i}", span_type="turn", name=e["what"])
                for i, e in enumerate(doc.get("events") or [])]

    register_trajectory_parser("osmosis-fork", parse_osmosis)
    try:
        client.fail_open = False
        run = client.run(experiment="e", hypothesis="h", name="r")
        root = tmp_path / "t"
        root.mkdir()
        (root / "trajectory.json").write_text(json.dumps(
            {"format": "osmosis-fork-v0", "events": [{"what": "rollout step"}]}
        ))
        result = capture_trial(run, root, strict=True)
        assert result["trajectory"]["expanded"] is True
        assert result["trajectory"]["spans"] == 1
    finally:
        atif._PARSERS.pop("osmosis-fork", None)


def test_truncation_is_an_explicit_marker_never_silent(client, app, tmp_path):
    client.fail_open = False
    run = client.run(experiment="e", hypothesis="h", name="r")
    capture_trial(
        run, _write_atif_trial(tmp_path / "t", "hello-world-context-summarization.trajectory.json"),
        max_trajectory_spans=4, strict=True,
    )
    spans = app.spans[run.id]
    marker = next(s for s in spans if s["span_type"] == "marker")
    assert marker["attributes"]["truncated"] is True
    # 10 turns + 7 calls = 17 planned; 4 kept eagerly
    assert marker["attributes"]["remaining"] == 13
    manifest = client.list_run_artifacts(run.id, kind=MANIFEST_KIND)[0]
    assert manifest["meta"]["trajectory"]["truncated"] is True


def test_re_expansion_is_idempotent(client, app, tmp_path):
    client.fail_open = False
    run = client.run(experiment="e", hypothesis="h", name="r")
    doc = _load(FIXTURES / "hello-world-timeout.trajectory.json")
    root = run.span("rollout", name="t")
    first = expand_trajectory(run, doc, root_span_id=root, trial="t__1", max_spans=0, strict=True)
    ids_once = sorted(s["id"] for s in app.spans[run.id] if s["span_type"] != "rollout")
    expand_trajectory(run, doc, root_span_id=root, trial="t__1", max_spans=0, strict=True)
    ids_twice = sorted({s["id"] for s in app.spans[run.id] if s["span_type"] != "rollout"})
    assert first["expanded"] and ids_twice == ids_once  # same UUIDs -> upserts, no duplicates


# -- retroactive expansion via the CLI -------------------------------------------
def test_cli_trial_expand_retroactively(client, app, tmp_path, monkeypatch, capsys):
    import importlib

    cli_main = importlib.import_module("probe.cli.main")
    client.fail_open = False
    run = client.run(experiment="e", hypothesis="h", name="r")
    # capture WITHOUT expansion — the "format had no parser yet" scenario
    result = capture_trial(
        run, _write_atif_trial(tmp_path / "t", "hello-world-invalid-json.trajectory.json"),
        step_index=42, expand=False, strict=True,
    )
    assert app.spans_upserted == 1
    manifest_id = result["manifest"]["id"]

    monkeypatch.setattr(cli_main, "_client", lambda: client)
    cli_main.trial_expand(run.id, manifest_id, max_spans=0)
    out = json.loads(capsys.readouterr().out)
    assert out["expanded"] is True and out["spans"] == 8
    # spans landed under the rollout span captured earlier, step preserved
    spans = app.spans[run.id]
    rollout = next(s for s in spans if s["span_type"] == "rollout")
    turns = [s for s in spans if s["span_type"] == "turn"]
    assert len(turns) == 5
    assert all(s["parent_span_id"] == rollout["id"] for s in turns)
    assert all(s["step_index"] == 42 for s in turns)
