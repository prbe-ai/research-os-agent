"""The two-pass orchestrator, with the agent faked out.

Every test here is about who is authoritative for what. The walk owns the
count, the agent owns the meaning, the ledger owns which units are done, and
the manifest on disk owns how much a unit actually produced. A backfill lies
exactly when one of those gets to answer another's question.
"""

from __future__ import annotations

import json

import pytest

from probe.cli import backfill as bf
from probe.cli import backfill_evidence as ev
from probe.cli import backfill_ledger as bl
from probe.cli import backfill_plan as bp
from probe.cli import backfill_run as br


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("PROBE_BACKFILL_STATE_DIR", str(d))
    return d


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "drive"
    for rel, text in [
        ("readme.md", "odyssey ablation"),
        ("michael/train.py", "import torch"),
        ("michael/step_10.pt", "w" * 40),
        ("xian/eval.py", "import numpy"),
    ]:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def _unit(uid="u1", project="odyssey", paths=("michael/train.py",)):
    return bl.Unit(unit_id=uid, project=project, paths=tuple(paths))


class _FakeAgent:
    """Stands in for `launch_agent`. Records calls, writes what it is told to."""

    def __init__(self, *, ok=True, manifest_rows=2, tail="", writes=True):
        self.ok, self.rows, self.tail, self.writes = ok, manifest_rows, tail, writes
        self.calls: list[dict] = []

    def __call__(self, folder, prompt, **kw):
        self.calls.append({"prompt": prompt, **kw})
        if self.writes:
            for line in prompt.splitlines():
                if line.strip().startswith("Write JSONL to:"):
                    path = line.split("Write JSONL to:", 1)[1].strip()
                    from pathlib import Path

                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    Path(path).write_text(
                        "\n".join(
                            json.dumps({"path": f"f{i}.py", "notes": "n"})
                            for i in range(self.rows)
                        )
                    )
        return self.ok, self.tail


# -- pass B: a unit ----------------------------------------------------------


def test_a_unit_is_recorded_before_it_starts_and_after_it_stops(folder, state_dir, monkeypatch):
    fake = _FakeAgent()
    monkeypatch.setattr(bf, "launch_agent", fake)
    led = bl.Ledger.for_folder(folder)
    unit = _unit()
    led.record_plan([unit], ["odyssey"])

    out = br.run_unit(folder, unit, agent=bf.Agent.CLAUDE, ledger=led,
                      manifest_dir=state_dir)
    assert out.ok and out.rows == 2
    st = led.read()
    assert st.units["u1"].state is bl.UnitState.DONE
    assert st.units["u1"].attempts == 1, "started was recorded, not just finished"


def test_a_unit_that_writes_no_manifest_is_a_failure_whatever_it_claims(
    folder, state_dir, monkeypatch
):
    """The agent's own account is never the authority. Reporting success having
    produced nothing is the exact failure the reconcile exists to catch."""
    monkeypatch.setattr(bf, "launch_agent", _FakeAgent(ok=True, writes=False))
    led = bl.Ledger.for_folder(folder)
    unit = _unit()
    led.record_plan([unit], ["odyssey"])

    out = br.run_unit(folder, unit, agent=bf.Agent.CLAUDE, ledger=led,
                      manifest_dir=state_dir)
    assert out.ok is False
    assert "no manifest" in out.detail
    assert led.read().units["u1"].state is bl.UnitState.FAILED


def test_a_failed_unit_is_left_outstanding_for_the_resume(folder, state_dir, monkeypatch):
    monkeypatch.setattr(bf, "launch_agent", _FakeAgent(ok=False, tail="boom"))
    led = bl.Ledger.for_folder(folder)
    unit = _unit()
    led.record_plan([unit], ["odyssey"])
    br.run_unit(folder, unit, agent=bf.Agent.CLAUDE, ledger=led, manifest_dir=state_dir)
    assert [r.unit.unit_id for r in led.read().outstanding()] == ["u1"]


def test_a_unit_gets_a_session_and_a_finite_timeout(folder, state_dir, monkeypatch):
    """`timeout=None` means one wedged unit stalls an overnight import and
    nobody finds out until morning."""
    fake = _FakeAgent()
    monkeypatch.setattr(bf, "launch_agent", fake)
    led = bl.Ledger.for_folder(folder)
    unit = _unit()
    led.record_plan([unit], ["odyssey"])
    br.run_unit(folder, unit, agent=bf.Agent.CLAUDE, ledger=led, manifest_dir=state_dir)
    call = fake.calls[0]
    assert call["session_id"]
    assert call["timeout"] == br.UNIT_TIMEOUT_S


def test_the_recorded_session_is_the_one_that_ran(folder, state_dir, monkeypatch):
    fake = _FakeAgent()
    monkeypatch.setattr(bf, "launch_agent", fake)
    led = bl.Ledger.for_folder(folder)
    unit = _unit()
    led.record_plan([unit], ["odyssey"])
    br.run_unit(folder, unit, agent=bf.Agent.CLAUDE, ledger=led, manifest_dir=state_dir)
    assert led.read().units["u1"].session_id == fake.calls[0]["session_id"]


def test_a_resumed_unit_adopts_its_previous_session(folder, state_dir, monkeypatch):
    fake = _FakeAgent()
    monkeypatch.setattr(bf, "launch_agent", fake)
    led = bl.Ledger.for_folder(folder)
    unit = _unit()
    led.record_plan([unit], ["odyssey"])
    br.run_unit(folder, unit, agent=bf.Agent.CLAUDE, ledger=led,
                manifest_dir=state_dir, resume="sess-earlier")
    assert fake.calls[0]["resume"] == "sess-earlier"


def test_the_unit_prompt_names_only_that_unit_s_files(folder, state_dir, monkeypatch):
    fake = _FakeAgent()
    monkeypatch.setattr(bf, "launch_agent", fake)
    led = bl.Ledger.for_folder(folder)
    unit = _unit(paths=("michael/train.py",))
    led.record_plan([unit], ["odyssey"])
    br.run_unit(folder, unit, agent=bf.Agent.CLAUDE, ledger=led, manifest_dir=state_dir)
    prompt = fake.calls[0]["prompt"]
    assert "michael/train.py" in prompt
    assert "xian/eval.py" not in prompt


def test_a_torn_manifest_line_costs_one_row_not_the_manifest(tmp_path):
    m = tmp_path / "m.jsonl"
    m.write_text('{"path":"a.py"}\n{"path":"b.py"}\n{"path":"c.p')
    assert br._manifest_rows(m) == 2


def test_manifest_rows_without_a_path_do_not_count(tmp_path):
    m = tmp_path / "m.jsonl"
    m.write_text('{"path":"a.py"}\n{"notes":"orphan"}\n')
    assert br._manifest_rows(m) == 1


# -- pass B: many units ------------------------------------------------------


def test_units_run_bounded_and_every_one_reports(folder, state_dir, monkeypatch):
    fake = _FakeAgent()
    monkeypatch.setattr(bf, "launch_agent", fake)
    led = bl.Ledger.for_folder(folder)
    units = [_unit(f"u{i}") for i in range(5)]
    led.record_plan(units, ["odyssey"])
    out = br.run_units(folder, units, agent=bf.Agent.CLAUDE, ledger=led,
                       manifest_dir=state_dir, concurrency=2)
    assert len(out) == 5 and all(o.ok for o in out)
    assert led.read().progress() == (5, 5)


def test_no_units_is_not_an_error(folder, state_dir, monkeypatch):
    led = bl.Ledger.for_folder(folder)
    assert br.run_units(folder, [], agent=bf.Agent.CLAUDE, ledger=led,
                        manifest_dir=state_dir) == []


def test_each_unit_writes_its_own_manifest(folder, state_dir, monkeypatch):
    monkeypatch.setattr(bf, "launch_agent", _FakeAgent())
    led = bl.Ledger.for_folder(folder)
    units = [_unit("ua"), _unit("ub")]
    led.record_plan(units, ["odyssey"])
    out = br.run_units(folder, units, agent=bf.Agent.CLAUDE, ledger=led,
                       manifest_dir=state_dir)
    assert {o.manifest.name for o in out} == {"ua.jsonl", "ub.jsonl"}


# -- projects up front -------------------------------------------------------


class _FakeClient:
    def __init__(self, fail: set[str] | None = None):
        self.made: list[str] = []
        self.fail = fail or set()

    def ensure_project(self, slug, name=None, description=None):
        if slug in self.fail:
            raise RuntimeError("nope")
        self.made.append(slug)
        return {"id": "u-" + slug, "slug": slug}


def test_every_project_is_created_before_any_unit_runs():
    """Up front removes the create race by construction rather than by locking."""
    client = _FakeClient()
    plan = bp.Plan(
        projects=[bp.ProjectSpec(slug="odyssey", name="Odyssey", description="d")],
        assignments=[],
    )
    made, problems = br.ensure_projects(client, plan, {"a": "odyssey", "b": "odyssey"})
    assert made == ["odyssey"] and problems == []
    assert client.made == ["odyssey"], "one call per project, not per file"


def test_a_project_that_cannot_be_created_is_reported_not_swallowed():
    client = _FakeClient(fail={"esm3"})
    plan = bp.Plan(projects=[], assignments=[])
    made, problems = br.ensure_projects(client, plan, {"a": "odyssey", "b": "esm3"})
    assert made == ["odyssey"]
    assert problems and "esm3" in problems[0]


def test_a_project_with_no_spec_still_gets_created_under_its_slug():
    client = _FakeClient()
    made, problems = br.ensure_projects(client, bp.Plan(projects=[], assignments=[]),
                                        {"a": "orphan"})
    assert made == ["orphan"] and not problems


# -- the approval screen -----------------------------------------------------


def test_the_approval_screen_leads_with_the_least_certain_files(folder):
    evidence = ev.gather(folder)
    paths = bp.relative_paths(evidence)
    plan = bp.Plan(
        projects=[bp.ProjectSpec(slug="odyssey")],
        assignments=[bp.Assignment(path=p, project="odyssey") for p in paths],
        unsure=[paths[0]],
    )
    assigned, disc = bp.resolve(evidence, plan)
    text = "\n".join(br.describe_plan(evidence, plan, assigned, disc))
    assert "Least certain" in text and paths[0] in text


def test_low_confidence_rows_join_the_least_certain_list(folder):
    evidence = ev.gather(folder)
    paths = bp.relative_paths(evidence)
    plan = bp.Plan(
        projects=[bp.ProjectSpec(slug="odyssey")],
        assignments=[
            bp.Assignment(path=p, project="odyssey",
                          confidence="low" if p == paths[1] else "high")
            for p in paths
        ],
    )
    assigned, disc = bp.resolve(evidence, plan)
    text = "\n".join(br.describe_plan(evidence, plan, assigned, disc))
    assert paths[1] in text


def test_the_approval_screen_counts_files_per_project(folder):
    evidence = ev.gather(folder)
    paths = bp.relative_paths(evidence)
    plan = bp.Plan(
        projects=[],
        assignments=[
            bp.Assignment(path=p, project="odyssey" if "michael" in p else "esm3")
            for p in paths
        ],
    )
    assigned, disc = bp.resolve(evidence, plan)
    text = "\n".join(br.describe_plan(evidence, plan, assigned, disc))
    assert "odyssey" in text and "esm3" in text
    assert f"{len(assigned):,} of {evidence.total_files:,} files placed" in text


def test_a_discrepancy_is_shown_on_the_approval_screen(folder):
    """The reviewer must see what the classifier could not place before saying
    yes, not afterwards in a log."""
    evidence = ev.gather(folder)
    paths = bp.relative_paths(evidence)
    plan = bp.Plan(
        projects=[],
        assignments=[bp.Assignment(path=p, project="odyssey") for p in paths[:1]],
    )
    assigned, disc = bp.resolve(evidence, plan)
    text = "\n".join(br.describe_plan(evidence, plan, assigned, disc))
    assert "never assigned" in text


# -- pass A ------------------------------------------------------------------


def test_classify_is_told_when_its_evidence_was_truncated(folder, monkeypatch):
    fake = _FakeAgent(ok=True, writes=False, tail="")
    monkeypatch.setattr(bf, "launch_agent", fake)
    evidence = ev.gather(folder)
    evidence.sample_budget_hit = True
    br.classify(folder, evidence, agent=bf.Agent.CLAUDE, existing=[])
    assert "sample budget was reached" in fake.calls[0]["prompt"]


def test_classify_gets_its_own_larger_deadline(folder, monkeypatch):
    fake = _FakeAgent(ok=True, writes=False)
    monkeypatch.setattr(bf, "launch_agent", fake)
    br.classify(folder, ev.gather(folder), agent=bf.Agent.CLAUDE, existing=[])
    assert fake.calls[0]["timeout"] == br.CLASSIFY_TIMEOUT_S
    assert br.CLASSIFY_TIMEOUT_S > br.UNIT_TIMEOUT_S


def test_a_failed_classify_returns_no_plan(folder, monkeypatch):
    monkeypatch.setattr(bf, "launch_agent", _FakeAgent(ok=False, writes=False, tail="died"))
    plan, tail = br.classify(folder, ev.gather(folder), agent=bf.Agent.CLAUDE, existing=[])
    assert plan is None and tail == "died"


def test_classify_parses_a_plan_out_of_the_stream(folder, monkeypatch):
    payload = {"projects": ["odyssey"],
               "assignments": [{"path": "readme.md", "project": "odyssey"}]}
    envelope = json.dumps({"type": "result", "result": f"ok\n{json.dumps(payload)}"})
    monkeypatch.setattr(bf, "launch_agent",
                        _FakeAgent(ok=True, writes=False, tail=envelope))
    plan, _ = br.classify(folder, ev.gather(folder), agent=bf.Agent.CLAUDE, existing=[])
    assert plan is not None and plan.assignments[0].project == "odyssey"


def test_classify_shows_the_agent_what_already_exists(folder, monkeypatch):
    fake = _FakeAgent(ok=True, writes=False)
    monkeypatch.setattr(bf, "launch_agent", fake)
    br.classify(folder, ev.gather(folder), agent=bf.Agent.CLAUDE,
                existing=["odyssey-infill-v3"])
    assert "odyssey-infill-v3" in fake.calls[0]["prompt"]
