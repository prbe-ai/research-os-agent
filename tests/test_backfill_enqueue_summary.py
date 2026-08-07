"""Reading the ingest summary, and watching the queue drain.

`splitlines()[-1]` is what shipped. `artifact add --from-manifest` prints
PRETTY-PRINTED JSON, so the last line is `}` -- which parses as nothing. Every
manifest reported "could not read the ingest summary (['}'])" and the run said
"0 queued for upload" while all 204 rows sat in the outbox delivering perfectly
well.

That is the worst shape a bug can take here: a REPORTING failure that reads as
total data loss, on the one step where bytes finally move. It is also the second
time this exact call has been wrong -- the first was `--async`, which made the
command fail to parse. Getting the argv right only moved the problem to reading
what came back.
"""

from __future__ import annotations

import json

import pytest

from probe.cli import backfill_run as br


def _pretty(payload: dict) -> str:
    """Output as the CLI really prints it: a version nudge, then indented JSON."""
    return ("probe 0.58.0 -> 0.63.0 available (`probe wizard --action update`)\n"
            + json.dumps(payload, indent=2))


# -- the summary -------------------------------------------------------------


def test_a_pretty_printed_summary_is_read(pytestconfig=None):
    """The regression. The last line here is `}`."""
    out = _pretty({"enqueued": 41, "failures": []})
    assert out.strip().splitlines()[-1] == "}", "fixture no longer reproduces the bug"
    assert br._ingest_summary(out) == {"enqueued": 41, "failures": []}


def test_a_single_line_summary_still_works():
    assert br._ingest_summary(json.dumps({"enqueued": 7}))["enqueued"] == 7


def test_failures_come_back_with_their_line_numbers():
    out = _pretty({"enqueued": 2, "failures": [{"line": 3, "error": "no such file"}]})
    got = br._ingest_summary(out)
    assert got["failures"][0]["line"] == 3


def test_noise_before_the_json_is_tolerated():
    """The version-update nudge prints on stdout, and a future release may add
    something else before it."""
    out = "some banner\nanother line\n" + json.dumps({"enqueued": 5}, indent=1)
    assert br._ingest_summary(out)["enqueued"] == 5


def test_output_with_no_summary_at_all_is_none():
    """A crash must stay distinguishable from a successful ingest."""
    assert br._ingest_summary("Traceback (most recent call last):\nboom") is None
    assert br._ingest_summary("") is None


def test_the_count_reaches_the_report(tmp_path, monkeypatch):
    """End to end through `enqueue_manifests`: the number the run prints is the
    number the command returned."""
    from probe.cli import backfill_ledger as bl

    manifest = tmp_path / "u-1.jsonl"
    manifest.write_text(json.dumps({"path": "a.py"}) + "\n")
    outcome = br.UnitOutcome(
        unit=bl.Unit(unit_id="u-1", project="odyssey", paths=("a.py",)),
        ok=True, manifest=manifest, rows=1,
    )

    class _Done:
        stdout = _pretty({"enqueued": 41, "failures": []})
        stderr = ""

    monkeypatch.setattr("subprocess.run", lambda argv, **kw: _Done())
    enqueued, problems = br.enqueue_manifests(tmp_path, [outcome],
                                              project_of={"u-1": "odyssey"})
    assert enqueued == 41
    assert problems == [], f"a readable summary reported a problem: {problems}"


# -- the drain watcher -------------------------------------------------------


class _Queue:
    """A journal whose pending count falls each time it is read."""

    def __init__(self, counts, failed=0):
        self.counts = list(counts)
        self._failed = failed

    def pending(self):
        n = self.counts.pop(0) if len(self.counts) > 1 else self.counts[0]
        return [None] * n

    def failed(self):
        return [None] * self._failed


@pytest.fixture
def journal(monkeypatch):
    def install(queue):
        monkeypatch.setattr("probe.sdk.journal.Journal", lambda *a, **k: queue)
    return install


def test_watching_ends_when_the_queue_empties(journal):
    journal(_Queue([5, 3, 1, 0]))
    lines = br.watch_outbox(5, stream=None, poll=0)
    assert any("All 5" in ln and "delivered" in ln for ln in lines)


def test_failures_are_reported_rather_than_called_delivered(journal):
    journal(_Queue([4, 0], failed=2))
    lines = br.watch_outbox(4, stream=None, poll=0)
    assert any("2 failed" in ln for ln in lines)
    assert not any("All 4" in ln for ln in lines)


def test_ctrl_c_stops_watching_not_uploading(journal, monkeypatch):
    """The watcher only counts. Saying so matters: someone who interrupts it
    must not think they cancelled their upload."""
    journal(_Queue([9, 9, 9]))

    def boom(_):
        raise KeyboardInterrupt

    monkeypatch.setattr("time.sleep", boom)
    lines = br.watch_outbox(9, stream=None, poll=0)
    assert any("still queued" in ln for ln in lines)
    assert any("keeps going in the background" in ln for ln in lines)


def test_an_already_empty_queue_says_so_immediately(journal):
    journal(_Queue([0]))
    assert br.watch_outbox(3, stream=None, poll=0) == ["All 3 file(s) delivered."]
