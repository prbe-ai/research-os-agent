"""The daemon's retrieval loop: the model asks to see more instead of guessing.

`{"need": [...]}` answers: `expand` pages through one event (or one section of
the detailed track-work reference), `grep` searches the whole session. Bounded
per cycle (rounds, requests, expanded characters), redacted, context only.
"""

from __future__ import annotations

import json
import time

import pytest

from tap import companion_ledger as ledger_mod
from tap import companion_observe as observe
from tap import companion_worker as worker_mod

from .test_companion import (  # noqa: F401 - `_state` is the autouse fixture that isolates XDG_STATE_HOME
    PROJECT,
    RUN,
    FakeApi,
    _assistant,
    _claude_lines,
    _live_worker,
    _state,
    _tool_result,
    _tool_use,
)

#: Assembled so no credential-shaped literal sits in the source (push protection).
_GH_PAT = "ghp_" + "16C7e42F292c6912E7710c838347Ae178B4a"


class ScriptedApi(FakeApi):
    """Answers with each scripted content in turn, then with proposals."""

    def __init__(self, script, **kw):
        super().__init__(**kw)
        self.script = list(script)
        self.seen: list[list[dict]] = []

    def complete(self, messages, *, max_tokens=None, timeout=None):
        self.completions += 1
        self.seen.append([dict(m) for m in messages])
        content = self.script.pop(0) if self.script else json.dumps({"proposals": self.proposals})
        answer = {"model": "m", "usage": {"input_tokens": 10, "output_tokens": 5}}
        # A dict scripts a whole answer (`finish_reason` too); a string is its content.
        return {**answer, **content} if isinstance(content, dict) else {**answer, "content": content}


def _need(*requests):
    return json.dumps({"need": list(requests)})


def _ids(path):
    lines, _ = observe.read_chunk(path, 0, max_bytes=10**7)
    return [(e, f"{e.offset}:{i}") for e, i in observe.indexed(observe.parse_lines("claude_code", lines))]


def test_a_cut_event_is_paged_through_by_its_id(tmp_path, monkeypatch):
    monkeypatch.setattr(observe, "EVENT_CEILING", 2000)
    table = "".join(f"| cfg-{i:03d} | 0.{9000 + i} |\n" for i in range(3000))  # ~60 KB of rows
    body = _claude_lines(_tool_use("c1", "python sweep.py"), _tool_result("c1", table), _assistant("done"))
    api = ScriptedApi([])
    worker, transcript = _live_worker(tmp_path, api, body)
    shown = observe.render(observe.parse_lines("claude_code", observe.read_chunk(transcript, 0, max_bytes=10**7)[0]))
    cut = next(e for e, eid in _ids(transcript) if e.role == observe.TOOL_RESULT)
    eid = next(eid for e, eid in _ids(transcript) if e is not None and e.offset == cut.offset and e.role == cut.role)
    assert f'{{"expand": "{eid}"}}' in shown  # the cut marker names how to see it
    api.script = [_need({"expand": eid, "page": 1})]
    worker.cycle(shadow=False)
    assert api.completions == 2
    retrieved = api.seen[1][-1]["content"]
    assert retrieved.startswith("RETRIEVED") and f"[{eid} @" in retrieved and "page 1 of 1" in retrieved
    assert "cfg-2999" in retrieved  # the part the view had cut
    assert api.seen[1][-2] == {"role": "assistant", "content": _need({"expand": eid, "page": 1})}


def test_two_events_on_one_line_expand_separately(tmp_path):
    line = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "first block"}, {"type": "text", "text": "second block"}]}}
    body = _claude_lines(line)
    _worker, transcript = _live_worker(tmp_path, FakeApi(), body)
    ids = [eid for _, eid in _ids(transcript)]
    if len(ids) < 2:
        pytest.skip("this harness's parser folds text blocks into one event")
    first = observe.read_event(transcript, "claude_code", ids[0])
    second = observe.read_event(transcript, "claude_code", ids[1])
    assert first.text != second.text


def test_grep_searches_the_whole_session_and_redacts(tmp_path):
    body = _claude_lines(
        _tool_use("c1", "python train.py"),
        _tool_result("c1", f"val_loss 0.41 token={_GH_PAT}"),
        _assistant("the val_loss plateaued at 0.41"),
    )
    api = ScriptedApi([_need({"grep": "VAL_LOSS"})])
    worker, _ = _live_worker(tmp_path, api, body)
    worker.cycle(shadow=False)
    retrieved = api.seen[1][-1]["content"]
    assert 'grep "VAL_LOSS": 2 hit(s)' in retrieved
    assert _GH_PAT not in retrieved and "0.41" in retrieved


def test_a_reference_section_comes_back_whole(tmp_path):
    api = ScriptedApi([_need({"expand": "reference#notes"})])
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("a result")))
    worker.cycle(shadow=False)
    assert "[reference#notes]\n## 6. NOTES" in api.seen[1][-1]["content"]
    assert '"reference#notes"' in api.seen[0][0]["content"]  # the headings are listed up front


def test_the_loop_is_bounded_and_then_asks_once_without_retrieval(tmp_path):
    ask = _need({"grep": "x"}, {"grep": "y"}, {"grep": "z"})
    api = ScriptedApi([ask] * 4)  # still asking after the last round; then proposals
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("x y z")))
    assert worker.cycle(shadow=False) != "skipped"
    assert api.completions == worker_mod.RETRIEVAL_ROUNDS + 2
    final = api.seen[-1]
    assert len(final) == 2 and final[-1]["content"].endswith(worker_mod.NO_RETRIEVAL_NOTE)
    # Six requests in all: the seventh is refused in the reply, not run.
    assert "request limit reached" not in api.seen[2][-1]["content"]
    assert "request limit reached" in api.seen[3][-1]["content"]


def test_an_empty_follow_up_is_asked_again_without_retrieval_not_lost(tmp_path):
    api = ScriptedApi([_need({"grep": "digits"}), ""])  # what the gateway's model did on a large range
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("the SVM wins on digits")))
    assert worker.cycle(shadow=False) != "skipped"
    assert api.completions == 3 and api.seen[-1][-1]["content"].endswith(worker_mod.NO_RETRIEVAL_NOTE)
    assert worker.ledger.watermark > 0


def test_a_model_that_still_asks_after_retrieval_is_withdrawn_is_a_failed_cycle(tmp_path):
    api = ScriptedApi([_need({"grep": "x"})] * (worker_mod.RETRIEVAL_ROUNDS + 2))
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("x")))
    with pytest.raises(worker_mod.UnusableAnswer):
        worker.cycle(shadow=False)
    assert worker.ledger.watermark == 0  # the range is retried, never skipped on the first failure


def test_retrieval_never_pushes_a_request_over_the_gateway_limit(tmp_path, monkeypatch):
    class Tight(ScriptedApi):
        def complete(self, messages, **kw):
            if not self.seen:  # leave 1,000 characters of room over the first request
                monkeypatch.setattr(worker_mod, "REQUEST_CHARS_LIMIT", sum(len(m["content"]) for m in messages) + 1000)
            return super().complete(messages, **kw)

    api = Tight([_need({"expand": "reference#run"})])
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("a result")))
    worker.cycle(shadow=False)
    assert sum(len(m["content"]) for m in api.seen[1]) <= worker_mod.REQUEST_CHARS_LIMIT
    assert "cut to fit the request limit" in api.seen[1][-1]["content"]


def test_evidence_from_a_retrieved_event_outside_the_cycle_is_held(tmp_path):
    first = _claude_lines(_assistant("an earlier result: 0.97"))
    later = _claude_lines(_tool_use("c1", f"probe exec --project {PROJECT} -- python run.py"),
                          _tool_result("c1", f"probe run: https://x/runs/{RUN}"))
    api = ScriptedApi([])
    worker, transcript = _live_worker(tmp_path, api, first)
    worker.cycle(shadow=False)
    transcript.write_bytes(first + later)
    api.script = [_need({"grep": "0.97"})]
    api.proposals = [{"kind": "note", "target": {"type": "run", "id": RUN}, "title": "t", "body": "0.97",
                      "evidence": {"from": len(first), "to": len(first)}}]
    worker.cycle(shadow=False)
    row = worker.ledger.conn.execute("SELECT status, reason FROM proposals ORDER BY id DESC").fetchone()
    assert row[0] == ledger_mod.STATUS_HELD and "outside the new transcript" in row[1]


def test_a_grep_window_that_starts_inside_a_secret_still_hides_it(tmp_path):
    # The value is assembled so no credential-shaped literal sits in the source.
    value = "Xk9vQ2mL" + "p7RtZ4wY8nB3cF6hJ1sD5gK0"
    body = _claude_lines(_tool_use("c1", "env"),
                         _tool_result("c1", f"export DB_PASSWORD={value}\n" + "x" * 120 + " train.py done"))
    _worker, transcript = _live_worker(tmp_path, FakeApi(), body)
    hits = observe.grep(transcript, "claude_code", "train.py", len(body))
    assert hits and all(value[4:] not in text and value[:8] not in text for _, text in hits)


def test_the_gateway_is_sent_the_same_redacted_bytes_the_trace_keeps(tmp_path):
    api = ScriptedApi([])
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("ok")))
    messages = [{"role": "user", "content": f"token={_GH_PAT}"}]
    worker._complete(messages, time.monotonic() + 100, shadow=True, cycle_id=None)
    assert _GH_PAT not in api.seen[-1][0]["content"]


#: What the gateway returns when the model's reasoning used up the output limit.
CUT_OFF = {"content": '{"proposals": [{"kind": "no', "finish_reason": "length"}


def test_an_answer_cut_off_at_the_output_limit_is_asked_again_at_once(tmp_path):
    api = ScriptedApi([CUT_OFF])
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("the SVM wins on digits")))
    assert "skipped" not in (worker.cycle(shadow=False) or "")
    assert api.completions == 2 and api.seen[-1][-1]["content"].endswith(worker_mod.CUT_SHORT_NOTE)
    assert not api.seen[0][-1]["content"].endswith(worker_mod.CUT_SHORT_NOTE)
    assert worker.ledger.watermark > 0
    # Both calls are paid for and counted.
    tokens = worker.ledger.conn.execute("SELECT input_tokens, output_tokens FROM cycles").fetchall()[-1]
    assert tuple(tokens) == (20, 10)


def test_a_second_cut_off_answer_is_not_chased(tmp_path):
    api = ScriptedApi([CUT_OFF, CUT_OFF])
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("the SVM wins on digits")))
    with pytest.raises(worker_mod.UnusableAnswer, match="length"):
        worker.cycle(shadow=False)
    assert api.completions == 2 and worker.ledger.watermark == 0  # failed, retried on a later cycle


def test_one_cut_off_retry_per_answer_even_across_retrieval_rounds(tmp_path):
    # The first round is cut off and re-asked; the re-ask asks for retrieval; the
    # next round is cut off again and is NOT re-asked.
    api = ScriptedApi([CUT_OFF, _need({"grep": "digits"}), CUT_OFF])
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("the SVM wins on digits")))
    with pytest.raises(worker_mod.UnusableAnswer, match="length"):
        worker.cycle(shadow=False)
    assert api.completions == 3


def test_the_final_answer_without_retrieval_is_re_asked_too(tmp_path):
    ask = _need({"grep": "x"})
    api = ScriptedApi([ask] * (worker_mod.RETRIEVAL_ROUNDS + 1) + [CUT_OFF])
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("x")))
    assert "skipped" not in (worker.cycle(shadow=False) or "")
    assert api.completions == worker_mod.RETRIEVAL_ROUNDS + 3
    last = api.seen[-1][-1]["content"]
    assert last.endswith(worker_mod.NO_RETRIEVAL_NOTE + worker_mod.CUT_SHORT_NOTE)


class FailingReAskApi(ScriptedApi):
    """The first answer is cut off; every attempt at the re-ask times out."""

    def complete(self, messages, *, max_tokens=None, timeout=None):
        if messages[-1]["content"].endswith(worker_mod.CUT_SHORT_NOTE):
            self.completions += 1
            raise worker_mod.api_mod.Retryable(0, None, "timed out")
        return super().complete(messages, max_tokens=max_tokens, timeout=timeout)


def test_a_re_ask_that_fails_leaves_the_cut_off_answer_as_before(tmp_path):
    # Never a gateway failure (which costs the lease after two): the pass fails
    # as an unusable answer, exactly as it did before the re-ask existed.
    api = FailingReAskApi([CUT_OFF])
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("the SVM wins on digits")))
    with pytest.raises(worker_mod.UnusableAnswer, match="length"):
        worker.cycle(shadow=False)
    assert api.completions == 1 + worker_mod.GATEWAY_ATTEMPTS  # the cut-off one, then each attempt at the re-ask


def test_no_re_ask_without_time_for_another_call_as_long(tmp_path, monkeypatch):
    # The cut-off call took 50 s of a 70 s pass: a re-ask would get a 20 s timeout
    # it cannot finish in, and would still be paid for. It is never started.
    clock = {"skew": 0.0}
    real = worker_mod.time.monotonic
    monkeypatch.setattr(worker_mod.time, "monotonic", lambda: real() + clock["skew"])

    class SlowApi(ScriptedApi):
        def complete(self, messages, *, max_tokens=None, timeout=None):
            clock["skew"] += 50
            return super().complete(messages, max_tokens=max_tokens, timeout=timeout)

    api = SlowApi([CUT_OFF])
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("the SVM wins on digits")))
    with pytest.raises(worker_mod.UnusableAnswer, match="length"):
        worker.cycle(shadow=False, wall=70)
    assert api.completions == 1


def test_an_answer_whole_at_the_output_limit_is_used_as_is(tmp_path):
    whole = {"content": json.dumps({"proposals": []}), "finish_reason": "length"}
    api = ScriptedApi([whole])
    worker, _ = _live_worker(tmp_path, api, _claude_lines(_assistant("the SVM wins on digits")))
    worker.cycle(shadow=False)
    assert api.completions == 1 and worker.ledger.watermark > 0
