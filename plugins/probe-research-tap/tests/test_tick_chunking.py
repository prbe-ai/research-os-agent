"""The live tail splits an oversized tick instead of losing it.

Bug being fixed: `_enqueue_read` built ONE body from the whole read, cursor to
EOF, with no size limit. The gateway caps bodies at 2MB
(`app/ingestion/sessions_router.py::MAX_BODY_BYTES`); anything over came back
413, `httpclient.classify` calls every non-401 4xx POISON, and `drain_once`
DROPS a poison row. So a single large tick — the first tick against a
transcript that already had history, a catch-up after the 300s idle cadence, a
daemon that started late — silently lost every event in it, permanently.

`reconcile.chunk_lines` had solved this for the backfill path against the same
cap since 0.3.0. The live tail was the one caller that never used it.

These tests drive `_enqueue_read` directly rather than `_run_loop`: the unit
under test is the split + cursor accounting, and the loop only supplies it.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_plugin_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="probe-research-tap-chunk-test-")
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", tmp)
    monkeypatch.setenv("PROBE_BASE_URL", "https://api.invalid")
    yield Path(tmp)


def _make_storage():
    from tap import config as cfg
    from tap.storage import Storage

    return Storage(cfg.state_db_path())


def _config(tmp_path: Path, transcript: Path):
    from tap import config as cfg

    return cfg.WatchConfig(
        session_id="sess-chunk",
        transcript_path=transcript,
        cwd=tmp_path,
        plugin_root=None,
        token="t",
        active_interval_s=60,
        idle_interval_s=300,
    )


def _event_line(i: int, filler: int) -> bytes:
    """One realistic user-turn event of roughly `filler` bytes."""
    return (
        json.dumps(
            {
                "type": "user",
                "uuid": f"u{i}",
                "message": {"role": "user", "content": "x" * filler},
            },
            separators=(",", ":"),
        ).encode()
        + b"\n"
    )


def _write_transcript(path: Path, count: int, filler: int) -> None:
    path.write_bytes(b"".join(_event_line(i, filler) for i in range(count)))


# ---------------------------------------------------------------------------
# The split itself
# ---------------------------------------------------------------------------


def test_oversized_tick_becomes_many_batches_each_under_the_cap(tmp_path: Path) -> None:
    """Before the fix this produced ONE row far over the gateway's 2MB cap."""
    from tap import reconcile
    from tap.main import _enqueue_read, _tick_read

    # ~6MB of raw lines: three chunks at the 1MB budget, and well past the
    # gateway cap if it were shipped whole.
    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, count=60, filler=100_000)
    assert transcript.stat().st_size > 3 * reconcile.MAX_BATCH_BYTES

    storage = _make_storage()
    try:
        c = _config(tmp_path, transcript)
        next_seq = _enqueue_read(c, storage, "dev", "last_batch_seq:sess-chunk", 0, _tick_read(c, storage))

        rows = []
        while (row := storage.next_due_batch(10**12, "sess-chunk")) is not None:
            rows.append(row)
            storage.mark_success(row.id)

        assert len(rows) > 1, "an oversized tick must be split, not shipped whole"
        assert next_seq == len(rows)
        assert [r.batch_seq for r in rows] == list(range(len(rows)))

        # MAX_BODY_BYTES in app/ingestion/sessions_router.py. Every body must
        # clear it, which is the whole point of the split.
        gateway_cap = 2_000_000
        for r in rows:
            assert len(r.body) < gateway_cap, f"batch {r.batch_seq} would be 413'd"
    finally:
        storage.close()


def test_line_numbers_stay_continuous_across_chunks(tmp_path: Path) -> None:
    """Chunk N+1 must resume numbering where chunk N stopped, not restart."""
    from tap.main import _enqueue_read, _tick_read

    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, count=60, filler=100_000)

    storage = _make_storage()
    try:
        c = _config(tmp_path, transcript)
        _enqueue_read(c, storage, "dev", "last_batch_seq:sess-chunk", 0, _tick_read(c, storage))

        seen: list[int] = []
        while (row := storage.next_due_batch(10**12, "sess-chunk")) is not None:
            seen.extend(e["line_no"] for e in json.loads(row.body)["events"])
            storage.mark_success(row.id)

        assert seen == list(range(60))
    finally:
        storage.close()


def test_small_tick_still_ships_as_one_batch(tmp_path: Path) -> None:
    """The split must not fragment ordinary ticks."""
    from tap.main import _enqueue_read, _tick_read

    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, count=5, filler=50)

    storage = _make_storage()
    try:
        c = _config(tmp_path, transcript)
        next_seq = _enqueue_read(c, storage, "dev", "last_batch_seq:sess-chunk", 0, _tick_read(c, storage))
        assert next_seq == 1

        row = storage.next_due_batch(10**12, "sess-chunk")
        assert row is not None
        assert len(json.loads(row.body)["events"]) == 5
    finally:
        storage.close()


# ---------------------------------------------------------------------------
# Cursor accounting — the half that makes a partial failure safe
# ---------------------------------------------------------------------------


def test_full_success_advances_cursor_to_eof(tmp_path: Path) -> None:
    from tap.main import _enqueue_read, _tick_read

    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, count=60, filler=100_000)

    storage = _make_storage()
    try:
        c = _config(tmp_path, transcript)
        _enqueue_read(c, storage, "dev", "last_batch_seq:sess-chunk", 0, _tick_read(c, storage))

        off = storage.get_offset(str(transcript))
        assert off is not None
        assert off.byte_offset == transcript.stat().st_size
        assert off.last_line_no == 60

        # A second tick over an unchanged file has nothing left to ship.
        lines, _base, _commit = _tick_read(c, storage)
        assert lines == []
    finally:
        storage.close()


def test_failed_chunk_commits_only_what_enqueued_and_never_duplicates(
    tmp_path: Path, monkeypatch
) -> None:
    """A mid-sequence enqueue failure must leave a PARTIAL cursor.

    Committing nothing would re-read the whole tick next time and re-ship the
    groups already queued under fresh sequence numbers — duplicated events.
    Committing everything would lose the unshipped tail. Only the partial
    commit is correct.
    """
    from tap import outbox
    from tap.main import _enqueue_read, _tick_read

    transcript = tmp_path / "t.jsonl"
    _write_transcript(transcript, count=60, filler=100_000)

    storage = _make_storage()
    try:
        c = _config(tmp_path, transcript)
        real_enqueue = outbox.enqueue
        calls = {"n": 0}

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("sqlite is having a day")
            return real_enqueue(**kwargs)

        monkeypatch.setattr(outbox, "enqueue", flaky)
        _enqueue_read(c, storage, "dev", "last_batch_seq:sess-chunk", 0, _tick_read(c, storage))
        monkeypatch.setattr(outbox, "enqueue", real_enqueue)

        off = storage.get_offset(str(transcript))
        assert off is not None
        assert 0 < off.byte_offset < transcript.stat().st_size, "cursor must be PARTIAL"

        first = storage.next_due_batch(10**12, "sess-chunk")
        assert first is not None
        shipped = [e["line_no"] for e in json.loads(first.body)["events"]]
        storage.mark_success(first.id)
        assert storage.next_due_batch(10**12, "sess-chunk") is None

        # The retry picks up exactly where the cursor stopped: no line is
        # shipped twice, and none is skipped.
        _enqueue_read(c, storage, "dev", "last_batch_seq:sess-chunk", 1, _tick_read(c, storage))
        retried: list[int] = []
        while (row := storage.next_due_batch(10**12, "sess-chunk")) is not None:
            retried.extend(e["line_no"] for e in json.loads(row.body)["events"])
            storage.mark_success(row.id)

        assert set(shipped).isdisjoint(retried), "an enqueued group was re-shipped"
        assert sorted(shipped + retried) == list(range(60))
    finally:
        storage.close()


def test_malformed_lines_are_dropped_but_still_move_the_cursor(tmp_path: Path) -> None:
    """Validation moved into _enqueue_read; it must behave as it did before."""
    from tap.main import _enqueue_read, _tick_read

    transcript = tmp_path / "t.jsonl"
    transcript.write_bytes(
        _event_line(0, 50) + b"{not json at all\n" + _event_line(2, 50)
    )

    storage = _make_storage()
    try:
        c = _config(tmp_path, transcript)
        _enqueue_read(c, storage, "dev", "last_batch_seq:sess-chunk", 0, _tick_read(c, storage))

        row = storage.next_due_batch(10**12, "sess-chunk")
        assert row is not None
        events = json.loads(row.body)["events"]
        assert len(events) == 2, "the malformed line must not ship"

        off = storage.get_offset(str(transcript))
        assert off is not None
        assert off.byte_offset == transcript.stat().st_size
    finally:
        storage.close()
