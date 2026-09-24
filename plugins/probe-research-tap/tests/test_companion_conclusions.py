"""The daemon's conclusions pass, turn-end drain, whole-event view, main and file notes.

Background: in a replayed trial the shipped worker recorded 0-1 notes where the
inline agent recorded the decision, the caveat, the project document, a
description, lineage and 31 file notes. These pin the mechanisms that close
that gap; the replay bench (`agent/evals/companion/`) measures the result.
"""

from __future__ import annotations

import contextlib
import json
import re

from tap import companion_api as api_mod
from tap import companion_lease as lease
from tap import companion_ledger as ledger_mod
from tap import companion_observe as observe
from tap import companion_worker as worker_mod

from .test_companion import (  # noqa: F401 - `_state` is the autouse fixture that isolates XDG_STATE_HOME
    PROJECT,
    RUN,
    SID,
    FakeApi,
    _assistant,
    _claude_lines,
    _live_worker,
    _set_state,
    _state,
    _tool_result,
    _tool_use,
)

FILE = "fafafafa-0000-4000-8000-000000000001"


def _turn(offset: int) -> None:
    (lease.sessions_dir() / f"{SID}{worker_mod.TURN_SUFFIX}").write_text(json.dumps({"offset": offset}))


def _api(**extra):
    return FakeApi(
        entities={
            f"/v1/runs/{RUN}": {"id": RUN, "name": "best", "status": "completed", "tags": [], "project_id": PROJECT},
            f"/v1/projects/{PROJECT}": {"id": PROJECT, "name": "digits", "kind": "experiment",
                                        "description": None, "notes": None, "notes_version": 0},
            **extra.pop("entities", {}),
        },
        lists={
            f"/v1/runs/{RUN}/sub-notes": {"sub_notes": [], "limit_count": 20},
            f"/v1/projects/{PROJECT}/sub-notes": {"sub_notes": [], "limit_count": 20},
            f"/v1/runs/{RUN}/artifacts": [
                {"id": FILE, "name": "results/table.md", "kind": "file", "notes": None},
                {"id": "c0de0000-0000-4000-8000-000000000001", "name": "run.py", "kind": "code", "notes": None},
            ],
            **extra.pop("lists", {}),
        },
    )


def _session(decision="The SVM wins: CV 0.9889 vs 0.9603, C=10, gamma=0.001."):
    return _claude_lines(
        _tool_use("c1", f"probe exec --project {PROJECT} -- python run.py"),
        _tool_result("c1", f"CV 0.9889\nprobe run: https://x/runs/{RUN}"),
        _assistant(decision),
    )


def test_render_keeps_commands_whole_and_cuts_only_a_giant_event():
    long_command = "python train.py " + "--flag x " * 2000
    events = observe.parse_lines("claude_code", [(10, json.dumps(_tool_use("c", long_command)).encode())])
    assert long_command in observe.render(events)  # never cut
    giant = observe.Event(offset=5, role=observe.TOOL_RESULT, text="y" * (observe.EVENT_CEILING + 5000))
    out = observe.render([giant])
    assert "chars cut" in out and len(out) < observe.EVENT_CEILING + 500


def test_a_cycle_ends_at_the_last_line_that_fits_the_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod, "RENDER_BUDGET_CHARS", 3000)
    body = _claude_lines(*[_assistant("x" * 1000) for _ in range(6)])
    worker, _ = _live_worker(tmp_path, FakeApi(), body)
    worker.cycle(shadow=False)
    first = worker.ledger.watermark
    assert 0 < first < len(body)  # the rest is the next cycle's, not cut
    worker.cycle(shadow=False)
    assert worker.ledger.watermark > first


def test_a_finished_turn_is_read_at_once_then_concluded(tmp_path):
    api = _api()
    body = _session()
    worker, _ = _live_worker(tmp_path, api, body)
    assert not worker.due()  # the transcript just grew: normally the worker waits for quiet
    _turn(len(body))
    assert worker.due()  # ...but a finished turn is read now
    worker.decide_due(shadow=False)
    assert worker.ledger.watermark == len(body)
    assert api.completions == 2  # the cycle, then the conclusions pass
    assert worker.ledger.get_json("conclusions_through") == len(body)
    assert not worker.conclusions_due()  # once per turn


def test_the_conclusions_pass_sees_the_whole_session_and_its_empty_fields(tmp_path):
    api = _api()
    body = _session()
    worker, _ = _live_worker(tmp_path, api, body)
    worker.cycle(shadow=False)  # the normal cycle already read it all
    seen: list[str] = []
    original = api.complete

    def spy(messages, **kw):
        seen.append("\n".join(m["content"] for m in messages))
        return original(messages, **kw)

    api.complete = spy
    worker.conclude()
    prompt = seen[-1]
    assert "THE WHOLE SESSION SO FAR" in prompt and "0.9889 vs 0.9603" in prompt
    facts = json.loads(prompt.split("SESSION FACTS\n", 1)[1].split("\n\nTHE WHOLE SESSION", 1)[0])
    project = next(e for e in facts["ENTITIES"] if e["id"] == PROJECT)
    assert project["description"] == "(empty)" and project["main_document"] == "(empty)"
    assert [f["id"] for f in facts["FILES"]] == [FILE]  # result files only, not code


def test_the_conclusions_pass_may_cite_any_range_the_daemon_owns_but_never_the_agents(tmp_path):
    api = _api()
    agent_part = _claude_lines(_assistant("the agent recorded this part"))
    body = agent_part + _session()
    _set_state("daemon")
    worker, transcript = _live_worker(tmp_path, api, b"")
    transcript.write_bytes(agent_part)
    worker.ledger.set_json("agent_ranges", [[0, len(agent_part)]])
    transcript.write_bytes(body)
    worker.cycle(shadow=False)
    decision_at = len(body)
    api.proposals = [
        {"kind": "note", "main": True, "target": {"type": "project", "id": PROJECT},
         "evidence": {"from": decision_at, "to": decision_at}, "title": "Result",
         "body": "## Result\nSVM, C=10, gamma=0.001: CV 0.9889 vs 0.9603."},
        {"kind": "note", "target": {"type": "project", "id": PROJECT},
         "evidence": {"from": 1, "to": len(agent_part)}, "title": "old", "body": "x"},
    ]
    worker.conclude()
    rows = worker.ledger.conn.execute("SELECT status, reason FROM proposals ORDER BY id").fetchall()
    assert rows[-2] == (ledger_mod.STATUS_PENDING, None)  # an earlier cycle's range: allowed here
    assert rows[-1][0] == ledger_mod.STATUS_HELD and "agent owned" in rows[-1][1]


def test_a_main_document_is_written_only_while_empty(tmp_path):
    api = _api()
    _worker, _ = _live_worker(tmp_path, api, _session())
    decider = worker_mod.Decider(api, known_ids={PROJECT: "project"}, run_starts=set(), run_ends=set(),
                                 cwd=tmp_path, touched=set(), chunk_text="", range_start=0, range_end=10**6)
    raw = {"kind": "note", "main": True, "target": {"type": "project", "id": PROJECT},
           "evidence": {"from": 1, "to": 2}, "title": "Result", "body": "## Result\nx"}
    out = decider.decide(raw)
    assert out["op"][0] == "NOTES"
    import pytest

    with pytest.raises(worker_mod.Held, match="not empty"):
        decider.decide(dict(raw, title="Again"))  # the first proposal filled it


def test_publishing_a_main_document_falls_back_to_a_sub_note_if_it_was_filled(tmp_path):
    op = ("NOTES", f"/v1/projects/{PROJECT}", {"title": "Result", "body": "## Result\nx"})
    empty = _api()
    worker_mod.publish(empty, _proposal("note", op))
    assert empty.writes[-1][0] == "PATCH" and empty.writes[-1][2]["base_version"] == 0
    filled = _api(entities={f"/v1/projects/{PROJECT}": {"id": PROJECT, "notes": "someone's", "notes_version": 3}})
    worker_mod.publish(filled, _proposal("note", op))
    assert filled.writes[-1][0] == "POST" and filled.writes[-1][1].endswith("/sub-notes")


def test_a_file_note_needs_a_listed_file_and_never_overwrites(tmp_path):
    api = _api()
    listing = f"/v1/runs/{RUN}/artifacts"
    files = {FILE: {"listing": listing, "row": {"id": FILE, "notes": None}}}
    decider = worker_mod.Decider(api, known_ids={RUN: "run"}, run_starts=set(), run_ends=set(), cwd=tmp_path,
                                 touched=set(), chunk_text="", range_start=0, range_end=10**6, files=files)
    raw = {"kind": "file_note", "target": {"type": "artifact", "id": FILE}, "evidence": {"from": 1, "to": 2},
           "notes": "every config's CV accuracy"}
    out = decider.decide(raw)
    assert out["op"][0] == "FILE_NOTE" and out["payload"] == {"notes": "every config's CV accuracy"}
    import pytest

    with pytest.raises(worker_mod.Held, match="already has notes"):
        decider.decide(raw)
    with pytest.raises(worker_mod.Held, match="not one listed"):
        decider.decide(dict(raw, target={"type": "artifact", "id": "fafafafa-0000-4000-8000-00000000dead"}))
    with pytest.raises(worker_mod.Held, match="only a file note"):
        decider.decide(dict(raw, kind="note", title="t", body="b"))
    # Publishing re-reads: a note written meanwhile is not overwritten.
    written = _api(lists={listing: [{"id": FILE, "notes": "someone's"}]})
    with contextlib.suppress(worker_mod.AlreadyDone):
        worker_mod.publish(written, _proposal("file_note", out["op"]))
    assert not written.writes


def test_the_conclusions_pass_notes_every_listed_file_from_its_own_map(tmp_path):
    api = _api()
    worker, _ = _live_worker(tmp_path, api, _session())
    worker.cycle(shadow=False)
    original = api.complete

    def answer(messages, **kw):
        out = original(messages, **kw)
        out["content"] = json.dumps({"proposals": [], "file_notes": {
            FILE: "every configuration's CV accuracy, as a table",
            "c0de0000-0000-4000-8000-000000000001": "code is never noted here",
            "fafafafa-0000-4000-8000-00000000dead": "not a listed file",
        }})
        return out

    api.complete = answer
    worker.conclude()
    rows = dict(worker.ledger.conn.execute(
        "SELECT target_id, COALESCE(reason, status) FROM proposals WHERE kind = 'file_note'").fetchall())
    assert rows[FILE] == ledger_mod.STATUS_PENDING
    assert "not one listed" in rows["c0de0000-0000-4000-8000-000000000001"]
    assert "not one listed" in rows["fafafafa-0000-4000-8000-00000000dead"]


def test_runs_a_sweep_script_printed_are_admitted_only_inside_the_sessions_experiment(tmp_path):
    swept = "5eeb0000-0000-4000-8000-000000000001"
    foreign = "f0e10000-0000-4000-8000-000000000001"
    swept_file = "fafafafa-0000-4000-8000-000000000002"
    api = _api(
        entities={
            f"/v1/runs/{swept}": {"id": swept, "name": "svm-C1", "project_id": PROJECT, "tags": [],
                                  "created_at": "2100-01-01T00:00:00Z"},
            f"/v1/runs/{foreign}": {"id": foreign, "name": "theirs", "project_id": "0the0000-0000-4000-8000-0000000000aa"},
        },
        lists={f"/v1/runs/{swept}/artifacts": [{"id": swept_file, "name": "results/runs/svm-C1.json",
                                                "kind": "file", "notes": None}]},
    )
    body = _session() + _claude_lines(
        _tool_use("c2", "python sweep.py"),
        _tool_result("c2", f"svm-C1: CV 0.97\nprobe run: https://x/runs/{swept}\nsee also runs/{foreign}"),
    )
    worker, _ = _live_worker(tmp_path, api, body)
    worker.cycle(shadow=False)
    mentioned = worker._session_view(len(body)).mentioned
    assert {swept, foreign} <= mentioned
    entities, files, shown, admitted, _ = worker._session_entities(mentioned)
    assert admitted == {swept: "run"}  # the foreign run is in someone else's project
    assert swept_file in files and any(f["id"] == swept_file for f in shown)
    assert all(e["id"] != foreign for e in entities)


def test_a_project_the_session_only_read_admits_no_runs(tmp_path):
    other = "0ccc0000-0000-4000-8000-000000000001"
    theirs = "7ee10000-0000-4000-8000-000000000001"
    api = _api(entities={f"/v1/projects/{other}": {"id": other, "name": "teammate's", "kind": "experiment"},
                         f"/v1/runs/{theirs}": {"id": theirs, "name": "theirs", "project_id": other,
                                                "created_at": "2100-01-01T00:00:00Z"}})
    worker, _ = _live_worker(tmp_path, api, _session())
    worker.cycle(shadow=False)
    worker.ledger.remember_ids({other: (None, 5, "mcp__plugin_probe-research_probe-research__entity")})
    admitted = worker._session_entities({theirs}).admitted
    assert admitted == {}  # read through a Probe tool: its runs are not the session's
    worker.ledger.conn.execute("UPDATE seen_ids SET context = ? WHERE entity_id = ?",
                               (f"probe exec --project {other} -- python sweep.py", other))
    admitted = worker._session_entities({theirs}).admitted
    assert admitted == {theirs: "run"}  # launched into by a probe command: the session's


def test_a_call_answered_in_the_next_chunk_still_grounds_its_run(tmp_path):
    import json as _json

    call = _json.dumps(_tool_use("c9", f"probe exec --project {PROJECT} -- python run.py")).encode()
    result = _json.dumps(_tool_result("c9", f"probe run: https://x/runs/{RUN}")).encode()
    first = observe.observe("claude_code", [(10, call)])
    assert RUN not in first.ids and "c9" in first.open_calls
    second = observe.observe("claude_code", [(20, result)], open_calls=first.open_calls)
    assert RUN in second.ids and not second.open_calls


def test_the_file_note_evidence_is_the_last_byte_the_daemon_owns(tmp_path):
    worker, _ = _live_worker(tmp_path, FakeApi(), _session())
    worker.ledger.set_json("agent_ranges", [[0, 100], [500, 900]])
    assert worker._owned_end(1000) == 1000
    assert worker._owned_end(700) == 500  # the agent took it back at 500
    assert worker._owned_end(50) == 0  # nothing owned: every file note is held


def test_finish_concludes_even_without_a_turn_signal(tmp_path, monkeypatch):
    api = _api()
    worker, _ = _live_worker(tmp_path, api, _session())
    monkeypatch.setattr(worker_mod.Worker, "ensure_api", lambda self: True)
    worker.finish()
    assert api.completions == 2  # the last cycle and the conclusions pass


def _proposal(kind, op):
    return ledger_mod.Proposal(id=1, idem_key="pc1-x", kind=kind, target_type="project", target_id=PROJECT,
                               payload={}, evidence={"_op": list(op)}, status="pending", reason=None, attempts=0)


def test_the_shipped_hook_writes_the_turn_offset_only_in_daemon(tmp_path):
    import os
    import subprocess
    from pathlib import Path

    hook = Path(worker_mod.__file__).resolve().parents[1] / "hooks" / "turn-end.sh"
    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(b"x" * 321)
    env = dict(os.environ, XDG_STATE_HOME=str(tmp_path / "state"))
    for state, expect in (("daemon", True), ("full", False)):
        _set_state(state)
        turn = lease.sessions_dir() / f"{SID}{worker_mod.TURN_SUFFIX}"
        turn.unlink(missing_ok=True)
        out = subprocess.run(["bash", str(hook)], input=json.dumps({"session_id": SID,
                             "transcript_path": str(transcript)}), capture_output=True, text=True, env=env)
        assert out.returncode == 0 and out.stdout == ""  # a Stop hook's stdout could block the agent
        assert turn.exists() is expect
        if expect:
            assert json.loads(turn.read_text())["offset"] == 321
    hooks = json.loads((hook.parent / "hooks.json").read_text())
    assert "turn-end.sh" in json.dumps(hooks["hooks"]["Stop"])


def test_api_errors_in_the_conclusions_pass_are_recorded(tmp_path):
    class Down(FakeApi):
        def complete(self, messages, **kw):
            raise api_mod.Retryable(503, None, "down")

    worker, _ = _live_worker(tmp_path, Down(), _session())
    with contextlib.suppress(api_mod.Retryable):
        worker.conclude()
    outcome = worker.ledger.conn.execute("SELECT outcome FROM cycles ORDER BY id DESC").fetchone()[0]
    assert outcome.startswith("conclusions failed")
    assert not worker.ledger.get_json("conclusions_through")  # retried at the next chance


def test_every_model_round_is_traced_exactly(tmp_path):
    api = _api()
    worker, _ = _live_worker(tmp_path, api, _session())
    worker.cycle(shadow=False)
    cycle = worker.ledger.conn.execute("SELECT MAX(id) FROM cycles").fetchone()[0]
    rounds = worker.ledger.traces(cycle)
    assert len(rounds) == 1 and rounds[0]["error"] is None
    sent = "\n".join(m["content"] for m in rounds[0]["request"])
    assert "NEW TRANSCRIPT" in sent and "0.9889 vs 0.9603" in sent
    assert "proposals" in rounds[0]["response"]["content"]


def test_the_conclusions_pass_carries_the_detailed_reference_and_a_cycle_does_not():
    kw = dict(chunk_text="x", prior_context="", known_ids={}, decided=[], feedback=[], run_starts=[], cwd="/w")
    concluding = worker_mod.build_messages(**kw, conclusions=True)[0]["content"]
    cycling = worker_mod.build_messages(**kw)[0]["content"]
    assert "# Recording reference (track-work, detailed)" in concluding
    assert "## 6. NOTES" in concluding
    assert "Recording reference" not in cycling


def test_quoted_text_and_heredoc_bodies_are_never_probe_commands():
    """A sweep that greps its logs for the text a probe command prints is not a
    probe command: the old regex read `(probe run)` in a quoted grep pattern as
    one, and dropped the sweep's output, run ids and files (daemon trial)."""
    P = "probe"
    cases = {
    f'cd x && for C in 1; do python run.py > l.log; grep -E "^(wrote|svm-|{P} run)|Error" l.log; done': [],
    f"{P} exec --project 11111111-1111-4111-8111-111111111111 -- python run.py": ["exec"],
    f"uv run --with {P}-research {P} run start --name a": ["run"],
    f"bash -lc 'cd /w && {P} notes create --body \"x | y\" --directed'": ["notes"],
    f"cat <<EOF > m.txt\n{P} run end abc\nEOF\npython go.py": [],
    f"time {P} run end 11111111-1111-4111-8111-111111111111 && echo done": ["run"],
    f"echo '{P} run start' | wc": [],
    f"echo {P} run end 11111111-1111-4111-8111-111111111111": [],  # an argument, not a command
    f"x=$((1<<2))\n{P} notes push n.md": ["notes"],  # a shift opens no heredoc
    f"python -c 'print(1<<2)'\n{P} run start --name b": ["run"],
    f"bash --norc -c '{P} exec --project p -- python r.py'": ["exec"],
    f"for c in 1 2; do {P} exec --project p -- python r.py; done": ["exec"],
    f"if true; then {P} run start --name c; fi": ["run"],
    f"env X=1 {P} run start --name d": ["run"],
    f"X=1 {P} run start --name e": ["run"],
    f"cat <<\\EOF\n{P} run end abc\nEOF": [],  # a backslash-quoted delimiter's body is text
    }
    for cmd, want in cases.items():
        assert [w[0] for w in observe._probe_segments(cmd) if w] == want, cmd


def test_a_chunk_of_reads_only_skips_the_model_call_but_is_recorded(tmp_path):
    api = _api()
    body = _claude_lines(_tool_use("r1", "cat results/table.md"), _tool_result("r1", "| c | acc |"),
                         _tool_use("r2", "ls results"), _tool_result("r2", "table.md"))
    worker, _ = _live_worker(tmp_path, api, body)
    assert worker.cycle(shadow=False) == "skipped"
    assert api.completions == 0 and worker.ledger.watermark == len(body)
    outcome = worker.ledger.conn.execute("SELECT outcome FROM cycles ORDER BY id DESC").fetchone()[0]
    assert outcome == worker_mod.SKIPPED_OUTCOME
    # A sentence from the agent is worth a call.
    # Its own state: the same SID under the same XDG_STATE_HOME would reopen the
    # first worker's ledger at its watermark and read only the appended line.
    (tmp_path / "b").mkdir()
    import os

    os.environ["XDG_STATE_HOME"] = str(tmp_path / "b" / "state")
    worker2, _transcript = _live_worker(tmp_path / "b", _api(), body + _claude_lines(_assistant("acc 0.98")))
    assert worker2.ledger.watermark == 0
    assert worker2.cycle(shadow=False) != "skipped"  # reads and a sentence, in one chunk


class JudgingApi(FakeApi):
    """A gateway with the judge route: every paragraph question fires at 0.9."""

    def __init__(self, *, answer=None, raises=None, **kw):
        super().__init__(**kw)
        self.judged: list[dict] = []
        self.answer, self.raises = answer, raises
        self.prompts: list[str] = []

    def judge(self, text, questions, *, notes=None, timeout=30):
        self.judged.append({"text": text, "questions": questions, "notes": list(notes or [])})
        if self.raises:
            raise self.raises
        if self.answer is not None:
            return self.answer
        assert all(re.fullmatch(r"[a-z_]{1,32}", q) for q in questions), questions  # the route's id rule
        assert all(len(q) <= 400 for q in questions.values()) and len(questions) <= 12
        assert all(len(n) <= 1000 for n in notes or [])
        # The route's combined budget (app/companion/router.py:judge_budget_chars).
        budget = int((24_000 - 35 * len(questions)) * 3.77)
        assert len(text) + sum(map(len, notes or [])) + sum(map(len, questions.values())) <= budget
        return {"answers": {q: 0.9 if q.startswith("para_") else 0.2 for q in questions}, "model": "jev", "error": None,
                "elapsed_ms": 40}

    def complete(self, messages, **kw):
        self.prompts.append("\n".join(m["content"] for m in messages))
        return super().complete(messages, **kw)


def _judging(tmp_path, api):
    words = ("The SVM wins with C=10 and gamma=0.001: CV 0.9889 against 0.9603 for logistic regression, "
             "on one split only.")
    body = _session(words)
    worker, _ = _live_worker(tmp_path, api, body)
    worker.cycle(shadow=False)
    _turn(len(body))  # the turn ended: the judge's per-kind answers are taken now
    import time as _time

    worker._judge_finished_turn(_time.monotonic() + worker_mod.CYCLE_WALL_SECONDS)
    return worker, body, words


def test_the_judge_records_every_kind_and_flags_paragraphs_for_the_conclusions_pass(tmp_path):
    api = JudgingApi(**{k: v for k, v in _api().__dict__.items() if k in ("entities", "lists")})
    worker, _body, words = _judging(tmp_path, api)
    worker.conclude()
    purposes = [j["purpose"] for j in worker.ledger.judgments()]
    assert purposes == ["gate", "targets"]
    assert set(api.judged[0]["questions"]) == set(worker_mod.JUDGE_KIND_QUESTIONS)
    assert "0.9889" in api.judged[1]["questions"]["para_a"]
    assert "PARAGRAPHS A JUDGE FLAGGED" in api.prompts[-1] and words in api.prompts[-1]
    assert api.completions == 2  # SHADOW: the judge never replaces a model call


def test_a_workspace_without_the_judge_stops_asking_and_still_concludes(tmp_path):
    api = JudgingApi(raises=api_mod.Forbidden(403, None, "the companion judge is not enabled for this workspace"),
                     **{k: v for k, v in _api().__dict__.items() if k in ("entities", "lists")})
    worker, _body, _ = _judging(tmp_path, api)
    worker.conclude()
    assert len(api.judged) == 1 and worker.ledger.get_json("judge_off")
    assert api.completions == 2 and "PARAGRAPHS A JUDGE FLAGGED" not in api.prompts[-1]
    worker.ledger.set_json("conclusions_through", 0)
    worker.conclude()
    assert len(api.judged) == 1  # not asked again this session


def test_a_judge_failure_is_recorded_and_flags_nothing(tmp_path):
    api = JudgingApi(answer={"answers": {}, "model": None, "error": "breaker_open", "elapsed_ms": None},
                     **{k: v for k, v in _api().__dict__.items() if k in ("entities", "lists")})
    worker, _, _ = _judging(tmp_path, api)
    worker.conclude()
    assert [j["error"] for j in worker.ledger.judgments()] == ["breaker_open", "breaker_open"]
    assert "PARAGRAPHS A JUDGE FLAGGED" not in api.prompts[-1] and api.completions == 2


def test_the_judge_is_off_by_env_and_absent_without_the_route(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_mod.ENV_JUDGE, "off")
    api = JudgingApi(**{k: v for k, v in _api().__dict__.items() if k in ("entities", "lists")})
    worker, _, _ = _judging(tmp_path, api)
    worker.conclude()
    assert api.judged == []
    monkeypatch.delenv(worker_mod.ENV_JUDGE)
    plain, _ = _live_worker(tmp_path / "x", _api(), b"") if (tmp_path / "x").mkdir() is None else (None, None)
    assert not plain._judge_on()  # the replay bench's API has no judge: nothing is asked


import pytest as _pytest  # noqa: E402


@_pytest.mark.parametrize("payload", [
    "not json at all",
    json.dumps({"session_id": SID}),  # no transcript path (Codex may send none)
    json.dumps({"session_id": "../../x", "transcript_path": "/tmp/t.jsonl"}),
    json.dumps({"session_id": SID, "transcript_path": "/nonexistent/t.jsonl"}),
])
def test_the_stop_hook_is_silent_and_writes_nothing_on_every_refusal(tmp_path, payload):
    import os
    import subprocess
    from pathlib import Path

    hook = Path(worker_mod.__file__).resolve().parents[1] / "hooks" / "turn-end.sh"
    _set_state("daemon")
    env = dict(os.environ, XDG_STATE_HOME=os.environ["XDG_STATE_HOME"])
    out = subprocess.run(["bash", str(hook)], input=payload, capture_output=True, text=True, env=env)
    assert out.returncode == 0 and out.stdout == "" and out.stderr == ""
    assert not list(lease.sessions_dir().glob("*.turn"))
