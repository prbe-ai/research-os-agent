"""The daemon stays inside the gateway's limits and never wedges on a refusal.

Review of the conclusions pass (2026-09-23): nothing bounded the whole request
(a long session built 468k-1.3M characters against the gateway's 400,000), a
refused pass retried every cooldown forever, and a pass ran on every turn. These
pin the bounds, the skip-on-the-record, the debounce, and the file-note write's
precondition.
"""

from __future__ import annotations

import json

import pytest

from tap import companion_api as api_mod
from tap import companion_ledger as ledger_mod
from tap import companion_worker as worker_mod

from .test_companion import (  # noqa: F401 - `_state` is the autouse fixture that isolates XDG_STATE_HOME
    PROJECT,
    RUN,
    SID,
    FakeApi,
    _assistant,
    _claude_lines,
    _live_worker,
    _state,
    _tool_result,
    _tool_use,
    _user,
)
from .test_companion_conclusions import FILE, _api, _session


class Sizing(FakeApi):
    """Records the size of every request, and can answer with a fixed status."""

    def __init__(self, *, refuse=None, content=None, **kw):
        super().__init__(**kw)
        self.sizes: list[int] = []
        self.refuse, self.content = refuse, content

    def complete(self, messages, **kw):
        self.sizes.append(sum(len(m["content"]) for m in messages))
        if self.refuse is not None:
            raise self.refuse
        out = super().complete(messages, **kw)
        if self.content is not None:
            out["content"] = self.content
        return out


def _entities():
    base = _api()
    return {"entities": base.entities, "lists": base.lists}


def _long_session(turns=40, chars=10_800):
    events = [_user("Compare an RBF SVM with logistic regression on digits.")]
    events += [_tool_use("c0", f"probe exec --project {PROJECT} -- python run.py"),
               _tool_result("c0", f"probe run: https://x/runs/{RUN}")]
    events += [_assistant(f"result {i}: acc 0.9{i:02d} vs 0.8. " + "detail " * (chars // 7)) for i in range(turns)]
    return _claude_lines(*events)


def test_the_conclusions_request_fits_the_gateway_however_long_the_session(tmp_path):
    api = Sizing(**_entities())
    worker, _ = _live_worker(tmp_path, api, _long_session())
    worker.conclude()
    assert api.sizes and max(api.sizes) <= worker_mod.REQUEST_CHARS_LIMIT
    prompt = "\n".join(m["content"] for m in api.messages)
    assert "Compare an RBF SVM" in prompt  # the researcher's goal is always kept
    assert "result 39:" in prompt  # the newest words are kept
    assert "older or larger events are not shown" in prompt  # and what was left out is named


@pytest.mark.parametrize("refusal", [api_mod.Rejected(422, None, "too large"),
                                     worker_mod.UnusableAnswer(502, None, "still asking")])
def test_a_conclusions_pass_the_gateway_refuses_is_skipped_on_the_record_not_retried(tmp_path, refusal):
    api = Sizing(refuse=refusal, **_entities())
    worker, _ = _live_worker(tmp_path, api, _session())
    size = worker._transcript_size()
    assert worker.conclude().startswith("skipped")
    assert worker.ledger.get_json("conclusions_through") == size  # the next turn's pass sees it all again
    outcome = worker.ledger.conn.execute("SELECT outcome FROM cycles ORDER BY id DESC").fetchone()[0]
    assert outcome.startswith("conclusions skipped")


def test_a_gateway_that_cannot_serve_the_worker_at_all_is_not_a_skip(tmp_path):
    api = Sizing(refuse=api_mod.Rejected(404, None, "no such route"), **_entities())
    worker, _ = _live_worker(tmp_path, api, _session())
    with pytest.raises(worker_mod.GatewayUnusable):
        worker.conclude()
    assert not worker.ledger.get_json("conclusions_through")


def test_the_pass_runs_at_most_once_per_gap_and_after_it_without_a_new_turn(tmp_path, monkeypatch):
    api = Sizing(**_entities())
    body = _session()
    worker, transcript = _live_worker(tmp_path, api, body)
    (worker_mod.lease.sessions_dir() / f"{SID}{worker_mod.TURN_SUFFIX}").write_text(json.dumps({"offset": len(body)}))
    now = [1_000_000.0]
    monkeypatch.setattr(worker_mod.time, "time", lambda: now[0])
    worker.decide_due(shadow=False)
    assert worker.ledger.get_json("conclusions_through") == len(body)
    more = body + _claude_lines(_assistant("A second result: 0.97 on the held-out split."))
    transcript.write_bytes(more)
    (worker_mod.lease.sessions_dir() / f"{SID}{worker_mod.TURN_SUFFIX}").write_text(json.dumps({"offset": len(more)}))
    worker.cycle(shadow=False)
    now[0] += 60
    assert not worker.conclusions_due()  # a minute later: deferred
    now[0] += worker_mod.CONCLUSIONS_MIN_GAP_SECONDS
    assert worker.conclusions_due()  # the gap is over: due without another turn


def test_a_turn_the_agent_owned_is_not_concluded(tmp_path):
    api = Sizing(**_entities())
    body = _session()
    worker, _ = _live_worker(tmp_path, api, body)
    worker.ledger.set_json("agent_ranges", [[0, len(body)]])
    assert worker.conclude() == "nothing new the daemon owns"
    assert api.sizes == []


def test_a_first_cycle_after_handover_trims_its_earlier_context_to_fit(tmp_path, monkeypatch):
    api = Sizing(**_entities())
    earlier = _claude_lines(*[_assistant("context " * 1200) for _ in range(12)])
    worker, transcript = _live_worker(tmp_path, api, earlier)
    worker.ledger.set_watermark(len(earlier))
    transcript.write_bytes(earlier + _session())
    first = worker_mod.build_messages(chunk_text="", prior_context="", known_ids={}, decided=[], feedback=[],
                                      run_starts=[], cwd="/w")
    monkeypatch.setattr(worker_mod, "REQUEST_CHARS_LIMIT", sum(len(m["content"]) for m in first) + 4000)
    worker.cycle(shadow=False)
    assert api.sizes and api.sizes[-1] <= worker_mod.REQUEST_CHARS_LIMIT


def test_a_file_notes_only_answer_is_usable(tmp_path):
    api = Sizing(content=json.dumps({"file_notes": {FILE: "every configuration's CV accuracy"}}), **_entities())
    worker, _ = _live_worker(tmp_path, api, _session())
    worker.cycle(shadow=False)
    assert worker.conclude().endswith("decided")
    row = worker.ledger.conn.execute("SELECT status FROM proposals WHERE kind = 'file_note'").fetchone()
    assert row == (ledger_mod.STATUS_PENDING,)


def test_a_main_document_proposed_for_a_run_becomes_a_sub_note(tmp_path):
    api = _api()
    decider = worker_mod.Decider(api, known_ids={RUN: "run"}, run_starts=set(), run_ends=set(), cwd=tmp_path,
                                 touched=set(), chunk_text="", range_start=0, range_end=10**6)
    out = decider.decide({"kind": "note", "main": True, "target": {"type": "run", "id": RUN},
                          "evidence": {"from": 1, "to": 2}, "title": "Result", "body": "x" * 9000})
    assert out["op"][0] == "POST" and out["op"][1].endswith("/sub-notes")
    assert len(out["payload"]["body"]) == worker_mod.NOTE_BODY_CHARS  # a run's carriers hold 4,000


def _file_note(listing):
    op = ("FILE_NOTE", f"/v1/artifacts/{FILE}", {"notes": "the results table", "listing": f"/v1/runs/{RUN}/artifacts"})
    return ledger_mod.Proposal(id=1, idem_key="pc1-y", kind="file_note", target_type="artifact", target_id=FILE,
                               payload={}, evidence={"_op": list(op)}, status="pending", reason=None, attempts=0), \
        FakeApi(lists={f"/v1/runs/{RUN}/artifacts": listing})


def test_a_file_note_write_carries_its_precondition_and_key(tmp_path):
    proposal, api = _file_note({"items": [{"id": FILE, "notes": None, "notes_version": 0}]})  # a dict listing
    worker_mod.publish(api, proposal)
    assert api.writes == [("PATCH", f"/v1/artifacts/{FILE}",
                           {"notes": "the results table", "base_version": 0, "op_key": "pc1-y"}, "pc1-y")]


def test_a_file_note_written_meanwhile_or_gone_is_never_overwritten(tmp_path):
    proposal, api = _file_note([{"id": FILE, "notes": "someone's", "notes_version": 2}])
    with pytest.raises(worker_mod.AlreadyDone):
        worker_mod.publish(api, proposal)
    proposal, api = _file_note([])
    with pytest.raises(api_mod.Rejected):
        worker_mod.publish(api, proposal)

    class Conflict(FakeApi):
        def request(self, method, path, body=None, **kw):
            raise api_mod.Rejected(409, None, "notes moved")

    proposal, _ = _file_note([])
    racing = Conflict(lists={f"/v1/runs/{RUN}/artifacts": [{"id": FILE, "notes": None, "notes_version": 0}]})
    with pytest.raises(worker_mod.AlreadyDone):  # written between our read and our write
        worker_mod.publish(racing, proposal)
    assert not api.writes


def test_finish_publishes_what_is_decided_even_when_the_final_pass_fails(tmp_path, monkeypatch):
    api = _api()
    worker, transcript = _live_worker(tmp_path, api, _session())
    api.proposals = [{"kind": "note", "target": {"type": "run", "id": RUN}, "title": "SVM wins",
                      "body": "CV 0.9889 vs 0.9603", "evidence": {"from": 1, "to": transcript.stat().st_size}}]
    worker.cycle(shadow=False)
    monkeypatch.setattr(worker_mod.Worker, "ensure_api", lambda self: True)
    monkeypatch.setattr(worker_mod.Worker, "conclude", lambda self, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    worker.finish()
    assert any(w[0] == "POST" and w[1] == f"/v1/runs/{RUN}/sub-notes" for w in api.writes)


def test_the_judge_is_asked_about_paragraphs_it_is_shown_however_long_the_turn(tmp_path):
    from .test_companion_conclusions import JudgingApi

    api = JudgingApi(**_entities())
    words = [_assistant(f"Finding {i}: the SVM is ahead by 0.0{i} on fold {i}, a result worth a note. " * 3)
             for i in range(20)]
    body = _session() + _claude_lines(_tool_use("c9", "cat big.log"), _tool_result("c9", "z" * 120_000), *words)
    worker, _ = _live_worker(tmp_path, api, body)
    worker.cycle(shadow=False)
    worker.cycle(shadow=False)
    worker.conclude()
    targets = [j for j in api.judged if j["questions"] and all(q.startswith("para_") for q in j["questions"])]
    assert targets
    for call in targets:
        for question in call["questions"].values():
            start = question.split('"', 2)[1][:60]
            assert start in " ".join(call["text"].split())


def test_only_files_the_session_produced_upload_never_older_ones(tmp_path):
    import os

    work = tmp_path / "w"
    work.mkdir()
    (work / "fresh.csv").write_text("a,b\n1,2\n")
    (work / "old.pdf").write_bytes(b"%PDF-1.4 " + bytes(range(200)))
    os.utime(work / "old.pdf", (1_000_000_000, 1_000_000_000))  # 2001: before any session
    api = FakeApi(entities={f"/v1/runs/{RUN}": {"id": RUN}}, lists={f"/v1/runs/{RUN}/artifacts": []})
    decider = worker_mod.Decider(api, known_ids={RUN: "run"}, run_starts=set(), run_ends=set(), cwd=work,
                                 touched=set(), chunk_text="", range_start=0, range_end=10**6,
                                 produced_text="wrote fresh.csv and old.pdf", started_at=1_700_000_000.0)
    raw = {"kind": "artifact", "target": {"type": "run", "id": RUN}, "evidence": {"from": 1, "to": 2}}
    assert decider.decide(dict(raw, path="fresh.csv"))["op"][0] == "UPLOAD"
    with pytest.raises(worker_mod.Held, match="predates this session"):
        decider.decide(dict(raw, path="old.pdf"))


def test_a_run_a_script_printed_is_admitted_only_if_created_during_the_session(tmp_path):
    ours, older = "5eeb0000-0000-4000-8000-00000000000a", "5eeb0000-0000-4000-8000-00000000000b"
    base = _api()
    api = FakeApi(entities={**base.entities,
                            f"/v1/runs/{ours}": {"id": ours, "project_id": PROJECT, "created_at": "2100-01-01T00:00:00Z"},
                            f"/v1/runs/{older}": {"id": older, "project_id": PROJECT, "created_at": "2001-01-01T00:00:00Z"}},
                  lists={**base.lists, f"/v1/runs/{ours}/artifacts": [], f"/v1/runs/{older}/artifacts": []})
    worker, _ = _live_worker(tmp_path, api, _session())
    worker.cycle(shadow=False)
    admitted = worker._session_entities({ours, older}).admitted
    assert admitted == {ours: "run"}  # a teammate's run from before the session is context, not a target


def test_every_paid_round_counts_against_the_device_ceiling_even_when_a_later_one_fails(tmp_path):
    from .test_companion_retrieval import ScriptedApi, _need

    api = ScriptedApi([_need({"grep": "x"})] * (worker_mod.RETRIEVAL_ROUNDS + 2))
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("x")))
    before = worker.spend.spent_today()
    with pytest.raises(worker_mod.UnusableAnswer):
        worker.cycle(shadow=False)
    assert worker.spend.spent_today() - before == 15 * (worker_mod.RETRIEVAL_ROUNDS + 2)


def test_a_main_document_our_own_lost_response_already_wrote_is_not_duplicated(tmp_path):
    op = ("NOTES", f"/v1/projects/{PROJECT}", {"title": "Result", "body": "## Result\nx"})
    landed = _api(entities={f"/v1/projects/{PROJECT}": {"id": PROJECT, "notes": "## Result\nx", "notes_version": 1}})
    proposal = ledger_mod.Proposal(id=1, idem_key="pc1-z", kind="note", target_type="project", target_id=PROJECT,
                                   payload={}, evidence={"_op": list(op)}, status="pending", reason=None, attempts=1)
    with pytest.raises(worker_mod.AlreadyDone):
        worker_mod.publish(landed, proposal)
    assert not landed.writes


def test_a_huge_file_handed_to_an_edit_tool_is_cut_with_its_expand_id():
    from tap import companion_observe as observe

    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "w1", "name": "Write",
         "input": {"file_path": "/w/big.json", "content": "x" * (observe.EVENT_CEILING * 4)}}]}}).encode()
    shown = observe.render(observe.parse_lines("claude_code", [(len(line) + 1, line)]))
    assert len(shown) < observe.EVENT_CEILING + 500 and f'{{"expand": "{len(line) + 1}:0"}}' in shown


def test_the_conclusions_pass_is_shown_the_notes_that_already_exist(tmp_path):
    api = Sizing(entities={**_api().entities,
                           f"/v1/projects/{PROJECT}": {"id": PROJECT, "name": "digits", "kind": "experiment",
                                                       "notes": "## Result\nSVM wins", "notes_version": 1}},
                 lists={**_api().lists, f"/v1/projects/{PROJECT}/sub-notes": {
                     "sub_notes": [{"title": "companion: PCA verdict"}], "limit_count": 20}})
    worker, _ = _live_worker(tmp_path, api, _session())
    worker.cycle(shadow=False)
    worker.conclude()
    prompt = "\n".join(m["content"] for m in api.messages)
    assert "NOTES ALREADY ON THESE ENTITIES" in prompt and "companion: PCA verdict" in prompt


def test_file_notes_have_their_own_cap_and_never_crowd_out_a_note(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod, "MAX_FILE_NOTES_PER_SESSION", 1)
    monkeypatch.setattr(worker_mod, "MAX_WRITES_PER_SESSION", 1)
    worker, _ = _live_worker(tmp_path, FakeApi(), _session())
    cycle = worker.ledger.start_cycle(byte_start=0, byte_end=1, events=0)

    def item(kind, tid):
        return {"kind": kind, "target_type": "artifact" if kind == "file_note" else "run", "target_id": tid,
                "payload": {"n": tid}, "evidence": {"from": 1, "to": 1}, "op": ("POST", "/x", {})}

    worker._persist(cycle, [item("file_note", "f1"), item("file_note", "f2"), item("note", "r1")], shadow=False,
                    tokens=(0, 0), feedback_ids=[], watermark=None)
    rows = dict(worker.ledger.conn.execute("SELECT target_id, COALESCE(reason, status) FROM proposals"))
    assert rows == {"f1": "pending", "f2": "session file-note cap reached", "r1": "pending"}


def test_a_command_run_under_env_is_work_not_a_read():
    from tap import companion_observe as observe

    assert observe.is_work_command("env CUDA_VISIBLE_DEVICES=0 python train.py")
    assert observe.is_work_command("X=1 python train.py")
    assert not observe.is_work_command("env")
    assert not observe.is_work_command("cat results.md")
