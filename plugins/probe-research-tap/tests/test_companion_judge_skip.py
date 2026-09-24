"""The Jev judge's SKIP mode and session-end audit (daemon plan T16b).

SKIP is off unless asked for AND every kind is proven on shadow data; even
then a run event, a directed command or a written file always reaches the
model, and one skip-verdict turn in five is held out. The audit at session
end judges every paragraph against the notes that exist, and only a paragraph
no note records costs a model call.
"""

from __future__ import annotations

import hashlib
import json

from tap import companion_worker as worker_mod

from .test_companion import (  # noqa: F401 - `_state` is the autouse fixture that isolates XDG_STATE_HOME
    PROJECT,
    RUN,
    SID,
    _assistant,
    _claude_lines,
    _live_worker,
    _state,
    _tool_result,
    _tool_use,
)
from .test_companion_conclusions import JudgingApi, _api, _session, _turn

ALL_KINDS = ",".join(k for k in worker_mod.JUDGE_KIND_QUESTIONS if k != "nothing")


class Verdict(JudgingApi):
    """Answers the gate with `gate` and paragraph questions with `para` (or 0.05
    when the paragraph's first words already appear in a listed note)."""

    def __init__(self, *, gate, para=0.9, **kw):
        super().__init__(**kw)
        self.gate, self.para = gate, para

    def judge(self, text, questions, *, notes=None, timeout=30):
        self.judged.append({"text": text, "questions": questions, "notes": list(notes or [])})
        answers = {}
        for qid, question in questions.items():
            if qid in self.gate:
                answers[qid] = self.gate[qid]
            else:
                start = question.split('"', 2)[1][:40]
                answers[qid] = 0.05 if any(start[:25] in n for n in notes or []) else self.para
        return {"answers": answers, "model": "jev", "error": None, "elapsed_ms": 5}


NOTHING = {**{k: 0.1 for k in worker_mod.JUDGE_KIND_QUESTIONS}, "nothing": 0.95}


def _entities():
    base = _api()
    return {"entities": base.entities, "lists": base.lists}


def _chatter(holdout: bool) -> bytes:
    """A turn with nothing to record, padded until its end offset is (or is not)
    one the holdout picks."""
    for pad in range(200):
        body = _claude_lines(_assistant("Let me look around first." + " " * pad))
        digest = hashlib.sha256(f"{SID}:{len(body)}".encode()).digest()
        if (digest[0] % worker_mod.JUDGE_HOLDOUT_EVERY == 0) == holdout:
            return body
    raise AssertionError("no padding hits the wanted holdout")


def _run(tmp_path, monkeypatch, body, *, mode="skip", kinds=ALL_KINDS, gate=NOTHING):
    if mode:
        monkeypatch.setenv(worker_mod.ENV_JUDGE, mode)
    monkeypatch.setenv(worker_mod.ENV_JUDGE_SKIP_KINDS, kinds)
    api = Verdict(gate=gate, **_entities())
    worker, _ = _live_worker(tmp_path, api, body)
    _turn(len(body))
    worker.decide_due(shadow=False)
    return worker, api


def test_shadow_by_default_records_the_verdict_and_never_skips(tmp_path, monkeypatch):
    worker, api = _run(tmp_path, monkeypatch, _chatter(holdout=False), mode=None)
    assert api.completions >= 1  # the model was still asked
    assert [j["purpose"] for j in worker.ledger.judgments()][:1] == ["gate"]
    assert not worker.ledger.get_json("skip_through")


def test_skip_mode_with_every_kind_proven_skips_a_turn_with_nothing_to_record(tmp_path, monkeypatch):
    body = _chatter(holdout=False)
    worker, api = _run(tmp_path, monkeypatch, body)
    assert api.completions == 0  # neither the cycle nor the conclusions pass called the model
    outcomes = [r[0] for r in worker.ledger.conn.execute("SELECT outcome FROM cycles")]
    assert outcomes == [worker_mod.JUDGE_SKIPPED_OUTCOME]
    assert worker.ledger.watermark == len(body)
    assert "skip" in [j["purpose"] for j in worker.ledger.judgments()]


def test_one_unproven_kind_keeps_the_judge_in_shadow(tmp_path, monkeypatch):
    worker, api = _run(tmp_path, monkeypatch, _chatter(holdout=False), kinds="note,describe")
    assert api.completions >= 1 and not worker.ledger.get_json("skip_through")


def test_a_judge_that_sees_anything_to_record_never_skips(tmp_path, monkeypatch):
    _worker, api = _run(tmp_path, monkeypatch, _chatter(holdout=False), gate={**NOTHING, "note": 0.7})
    assert api.completions >= 1


def test_the_holdout_keeps_calling_the_model(tmp_path, monkeypatch):
    worker, api = _run(tmp_path, monkeypatch, _chatter(holdout=True))
    assert api.completions >= 1
    assert "holdout" in [j["purpose"] for j in worker.ledger.judgments()]
    assert not worker.ledger.get_json("skip_through")


def test_a_run_event_or_a_produced_file_always_reaches_the_model(tmp_path, monkeypatch):
    body = _claude_lines(_tool_use("c1", "python train.py"), _tool_result("c1", "wrote results/table.md"),
                         _assistant("Done."))
    _worker, api = _run(tmp_path, monkeypatch, body)
    assert api.completions >= 1  # the judge said nothing, but a file was produced


def test_the_audit_targets_a_missed_headline_and_not_what_a_note_already_records(tmp_path, monkeypatch):
    words = "The SVM wins with C=10 and gamma=0.001: CV 0.9889 against 0.9603 for logistic regression."
    missed = Verdict(gate=NOTHING, **_entities())
    worker, transcript = _live_worker(tmp_path, missed, _session(words))
    worker.cycle(shadow=False)
    worker.ledger.set_json("conclusions_through", transcript.stat().st_size)  # the passes already ran
    before = missed.completions
    out = worker.audit(wall=120)
    assert out.startswith("1 target(s)") and missed.completions == before + 1
    assert "PARAGRAPHS A JUDGE FLAGGED" in "\n".join(m["content"] for m in missed.messages)
    # A note the agent wrote itself (directed) already says it: nothing is asked of the model.
    recorded = Verdict(gate=NOTHING, entities={**_api().entities,
                       f"/v1/projects/{PROJECT}": {"id": PROJECT, "name": "digits", "kind": "experiment",
                                                   "notes": words, "notes_version": 1}}, lists=_api().lists)
    (tmp_path / "b").mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "b" / "state"))
    worker2, transcript2 = _live_worker(tmp_path / "b", recorded, _session(words))
    worker2.cycle(shadow=False)
    worker2.ledger.set_json("conclusions_through", transcript2.stat().st_size)
    before = recorded.completions
    assert worker2.audit(wall=120) == "nothing missing" and recorded.completions == before


def test_the_audit_is_opt_in_and_then_replaces_the_final_pass(tmp_path, monkeypatch):
    monkeypatch.setenv(worker_mod.ENV_JUDGE_AUDIT, "on")
    api = Verdict(gate=NOTHING, **_entities())
    worker, _ = _live_worker(tmp_path, api, _session("A plain status line, nothing decided here at all."))
    worker.cycle(shadow=False)
    monkeypatch.setattr(worker_mod.Worker, "ensure_api", lambda self: True)
    seen = []
    monkeypatch.setattr(worker_mod.Worker, "audit", lambda self, wall: seen.append(wall) or "nothing missing")
    worker.finish()
    assert seen and seen[0] > 0


def test_the_skip_verdict_is_recorded_with_its_answers_for_the_report(tmp_path, monkeypatch):
    worker, _ = _run(tmp_path, monkeypatch, _chatter(holdout=False))
    skip = [j for j in worker.ledger.judgments() if j["purpose"] == "skip"]
    assert skip and skip[0]["answers"]["nothing"] == 0.95 and json.dumps(skip[0]["answers"])


# -- review regressions: a skip covers ONE empty turn, never the backlog before it --

CONTENT = {**NOTHING, "note": 0.95, "nothing": 0.05}
WORDS = "The SVM wins: CV 0.9889 vs 0.9603 for logistic regression, so we keep C=10 and gamma=0.001 as the config."


class ByText(JudgingApi):
    """The gate says CONTENT for text holding the decision, NOTHING otherwise; the
    first `fail` model calls fail (a gateway blip)."""

    def __init__(self, fail=0, **kw):
        super().__init__(**kw)
        self.fail = fail

    def judge(self, text, questions, *, notes=None, timeout=30):
        self.judged.append({"text": text, "questions": questions, "notes": list(notes or [])})
        if "nothing" in questions:
            return {"answers": dict(CONTENT if "SVM" in text else NOTHING), "model": "jev", "error": None}
        return {"answers": {q: 0.9 for q in questions}, "model": "jev", "error": None}

    def complete(self, messages, **kw):
        if self.fail:
            self.fail -= 1
            from tap import companion_api as api_mod

            raise api_mod.Retryable(503, None, "gateway blip")
        return super().complete(messages, **kw)


def _thanks(prefix: bytes) -> bytes:
    from .test_companion import _user

    for pad in range(300):
        body = prefix + _claude_lines(_user("thanks"), _assistant("You're welcome." + " " * pad))
        if hashlib.sha256(f"{SID}:{len(body)}".encode()).digest()[0] % worker_mod.JUDGE_HOLDOUT_EVERY:
            return body
    raise AssertionError("no padding avoids the holdout")


def _skip_env(monkeypatch):
    monkeypatch.setenv(worker_mod.ENV_JUDGE, "skip")
    monkeypatch.setenv(worker_mod.ENV_JUDGE_SKIP_KINDS, ALL_KINDS)


def test_an_empty_turn_never_skips_an_earlier_turn_still_unread(tmp_path, monkeypatch):
    import pytest

    from tap import companion_api as api_mod

    _skip_env(monkeypatch)
    monkeypatch.setattr(worker_mod.time, "sleep", lambda s: None)
    api = ByText(fail=worker_mod.GATEWAY_ATTEMPTS, **_entities())
    first = _claude_lines(_assistant(WORDS))
    worker, transcript = _live_worker(tmp_path, api, first)
    _turn(len(first))
    with pytest.raises(api_mod.Retryable):
        worker.decide_due(shadow=False)  # the decision turn's model call fails: still unread
    body = _thanks(first)
    transcript.write_bytes(body)
    _turn(len(body))
    before = api.completions
    worker.decide_due(shadow=False)
    assert api.completions > before  # the decision turn reached the model
    assert worker.ledger.watermark == len(body)


def test_a_decision_turn_keeps_its_conclusions_pass_when_an_empty_turn_follows(tmp_path, monkeypatch):
    import time as _time

    _skip_env(monkeypatch)
    api = ByText(**_entities())
    first = _claude_lines(_assistant(WORDS))
    worker, transcript = _live_worker(tmp_path, api, first)
    worker.ledger.set_json("conclusions_at", _time.time())  # a pass ran a moment ago: the next waits
    _turn(len(first))
    worker.decide_due(shadow=False)
    body = _thanks(first)
    transcript.write_bytes(body)
    _turn(len(body))
    worker.decide_due(shadow=False)
    worker.ledger.set_json("conclusions_at", 0)  # the gap has passed
    before = api.completions
    out = worker.decide_due(shadow=False)
    assert "nothing to record" not in (out or "") and api.completions > before


def test_skip_ranges_left_from_before_are_ignored_once_skip_is_off(tmp_path, monkeypatch):
    body = _chatter(holdout=False)
    worker, api = _run(tmp_path, monkeypatch, body, mode="shadow")
    worker.ledger.set_json("skip_ranges", [[0, len(body)]])  # as if SKIP had been on earlier
    assert not worker._skipped(0, len(body))


def test_without_the_switch_session_end_runs_the_plain_final_pass_and_no_audit(tmp_path, monkeypatch):
    api = Verdict(gate=NOTHING, **_entities())
    worker, _ = _live_worker(tmp_path, api, _session("The SVM wins, CV 0.9889 vs 0.9603."))
    worker.cycle(shadow=False)
    monkeypatch.setattr(worker_mod.Worker, "ensure_api", lambda self: True)
    audits = []
    monkeypatch.setattr(worker_mod.Worker, "audit", lambda self, wall: audits.append(wall) or "x")
    before = api.completions
    worker.finish()
    assert audits == [] and api.completions == before + 1  # the conclusions pass, once
