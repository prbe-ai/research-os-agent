"""Tests for the reconciler — the eventual-consistency net under transcript capture.

Each of these pins one of the three evidenced losses from the 2026-08-12 audit:

  gap detection + backfill      resume left a transcript growing with no watcher
  adoption via the session log  a fork's file materialised after the daemon quit
  global drain                  outbox rows whose session never came back

plus the guards that keep the net from becoming a new failure: pre-install
history is not adopted, live daemons keep their own cursors, oversized backfills
are chunked under the gateway cap, and POISON/HALT classification is untouched.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from unittest import mock

import pytest

from tap import outbox, reconcile
from tap.httpclient import Classification, Response
from tap.storage import FileOffset, Storage


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Isolate plugin state AND the transcript tree from the real machine."""
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / "logs").mkdir()
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(plugin))
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("PROBE_BASE_URL", "https://api.invalid")
    monkeypatch.delenv("PROBE_TAP_SOURCE", raising=False)
    yield


@pytest.fixture
def projects(tmp_path) -> Path:
    return tmp_path / "projects"


@pytest.fixture
def plugin(tmp_path) -> Path:
    return tmp_path / "plugin"


@pytest.fixture
def storage(tmp_path) -> Storage:
    s = Storage(tmp_path / "plugin" / "state.db")
    yield s
    s.close()


def _event(i: int, text: str = "hello") -> str:
    return json.dumps({"type": "user", "uuid": f"u{i}", "message": {"role": "user",
                                                                    "content": text}})


def write_transcript(projects: Path, session_id: str, n_events: int, *, text: str = "hello") -> Path:
    d = projects / "-Users-someone-repo"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{session_id}.jsonl"
    with p.open("a", encoding="utf-8") as f:
        for i in range(n_events):
            f.write(_event(i, text) + "\n")
    return p


def mark_session_logged(plugin: Path, session_id: str) -> None:
    """Create the daemon log that proves capture was live for this session."""
    (plugin / "logs" / f"{session_id}.log").write_text("started\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# gap detection
# ---------------------------------------------------------------------------


def test_finds_gap_on_tracked_file_with_no_live_daemon(storage, projects, plugin):
    """The resume case: our cursor exists, the file grew past it, nobody is watching."""
    sid = "11111111-1111-4111-8111-111111111111"
    p = write_transcript(projects, sid, 10)
    mark_session_logged(plugin, sid)
    # Cursor recorded partway through, as a stopped daemon would leave it.
    half = p.read_bytes().index(b"\n", len(p.read_bytes()) // 2) + 1
    storage.upsert_offset(FileOffset(str(p), sid, "/repo", 5, int(time.time()), 1, half, half))

    gaps = reconcile.find_gaps(storage, now=int(time.time()))

    assert len(gaps) == 1
    assert gaps[0].session_id == sid
    assert gaps[0].tracked is True
    assert gaps[0].gap_bytes == p.stat().st_size - half


def test_adopts_untracked_file_when_a_session_log_exists(storage, projects, plugin):
    """The fork case: no cursor at all, but the tap ran for this session."""
    sid = "22222222-2222-4222-8222-222222222222"
    p = write_transcript(projects, sid, 10)
    mark_session_logged(plugin, sid)

    gaps = reconcile.find_gaps(storage, now=int(time.time()))

    assert len(gaps) == 1
    assert gaps[0].tracked is False
    assert gaps[0].byte_offset == 0
    assert gaps[0].gap_bytes == p.stat().st_size


def test_ignores_untracked_file_with_no_session_log(storage, projects):
    """Pre-install history is not ours to ship — the guard against a 672MB sweep."""
    write_transcript(projects, "33333333-3333-4333-8333-333333333333", 10)

    assert reconcile.find_gaps(storage, now=int(time.time())) == []


def test_ignores_subagent_sidechain_transcripts(storage, projects, plugin):
    """agent-*.jsonl never gets a SessionStart; capturing it would widen scope."""
    d = projects / "-Users-someone-repo"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "agent-abc123.jsonl"
    p.write_text(_event(0) + "\n", encoding="utf-8")
    (plugin / "logs" / "agent-abc123.log").write_text("x", encoding="utf-8")

    assert reconcile.find_gaps(storage, now=int(time.time())) == []


def test_ignores_files_outside_the_horizon(storage, projects, plugin):
    sid = "44444444-4444-4444-8444-444444444444"
    p = write_transcript(projects, sid, 5)
    mark_session_logged(plugin, sid)
    old = time.time() - 10 * 24 * 3600
    os.utime(p, (old, old))

    assert reconcile.find_gaps(storage, now=int(time.time())) == []


def test_skips_files_owned_by_a_live_daemon(storage, projects, plugin):
    """A running daemon owns its cursor; two writers would double-ship."""
    sid = "55555555-5555-4555-8555-555555555555"
    write_transcript(projects, sid, 5)
    mark_session_logged(plugin, sid)

    with mock.patch("tap.reconcile.has_live_daemon", return_value=True):
        assert reconcile.find_gaps(storage, now=int(time.time())) == []
    # ...and is found again once that daemon is gone.
    assert len(reconcile.find_gaps(storage, now=int(time.time()))) == 1


def test_a_small_fresh_gap_outranks_a_huge_stale_one(storage, projects, plugin):
    """Priority is recency, not size.

    Caught live: an active session that had just lost its daemon held a 4.5KB
    gap and got nothing, because four historical files averaging 1.7MB each
    consumed the whole sweep budget ahead of it. Size-first drains the most
    bytes and starves the only conversation still being written.
    """
    stale = "10101010-1010-4101-8101-101010101010"
    fresh = "20202020-2020-4202-8202-202020202020"
    stale_p = write_transcript(projects, stale, 40, text="q" * 500)  # big, old
    fresh_p = write_transcript(projects, fresh, 2)  # small, current
    mark_session_logged(plugin, stale)
    mark_session_logged(plugin, fresh)
    old = time.time() - 6 * 3600
    os.utime(stale_p, (old, old))

    gaps = reconcile.find_gaps(storage, now=int(time.time()))

    assert gaps[0].session_id == fresh
    assert gaps[0].gap_bytes < gaps[1].gap_bytes  # smaller, and still first
    assert fresh_p.stat().st_size < stale_p.stat().st_size


def test_no_gap_when_cursor_is_current(storage, projects, plugin):
    sid = "66666666-6666-4666-8666-666666666666"
    p = write_transcript(projects, sid, 5)
    mark_session_logged(plugin, sid)
    size = p.stat().st_size
    storage.upsert_offset(FileOffset(str(p), sid, "/repo", 5, int(time.time()), 1, size, size))

    assert reconcile.find_gaps(storage, now=int(time.time())) == []


# ---------------------------------------------------------------------------
# backfill
# ---------------------------------------------------------------------------


def test_backfill_enqueues_from_the_stored_offset_and_advances_it(storage, projects, plugin):
    sid = "77777777-7777-4777-8777-777777777777"
    p = write_transcript(projects, sid, 6)
    mark_session_logged(plugin, sid)
    raw = p.read_bytes()
    cut = raw.index(b"\n", len(raw) // 2) + 1
    lines_before = raw[:cut].count(b"\n")
    storage.upsert_offset(
        FileOffset(str(p), sid, "/repo", lines_before, int(time.time()), 1, cut, cut)
    )

    gap = reconcile.find_gaps(storage, now=int(time.time()))[0]
    written, batches = reconcile.backfill_gap(
        storage, gap, device_id="dev", budget_bytes=reconcile.MAX_BACKFILL_BYTES_PER_SWEEP
    )

    assert written == len(raw) - cut
    assert batches == 1
    after = storage.get_offset(str(p))
    assert after.byte_offset == len(raw)
    assert after.last_line_no == 6
    # Enqueued under the ORIGINAL session, not the sweeping daemon's.
    row = storage.next_due_batch(int(time.time()) + 1, sid)
    assert row is not None
    body = json.loads(row.body)
    assert body["session_id"] == sid
    # Line numbering continues from the cursor rather than restarting.
    assert body["events"][0]["line_no"] == lines_before


def test_backfill_resumes_batch_seq_from_meta(storage, projects, plugin):
    """A backfill must not reuse a seq: the R2 key is <session>:<batch_seq>."""
    sid = "88888888-8888-4888-8888-888888888888"
    write_transcript(projects, sid, 3)
    mark_session_logged(plugin, sid)
    storage.set_meta(f"last_batch_seq:{sid}", "41")

    gap = reconcile.find_gaps(storage, now=int(time.time()))[0]
    reconcile.backfill_gap(storage, gap, device_id="dev", budget_bytes=10 * 1024 * 1024)

    row = storage.next_due_batch(int(time.time()) + 1, sid)
    assert row.batch_seq == 42
    assert storage.get_meta(f"last_batch_seq:{sid}") == "42"


def test_backfill_chunks_oversized_gaps_under_the_body_cap(storage, projects, plugin, monkeypatch):
    """One 2MB batch would come back 413 and be POISON-dropped. Split it."""
    monkeypatch.setattr(reconcile, "MAX_BATCH_BYTES", 2000)
    sid = "99999999-9999-4999-8999-999999999999"
    write_transcript(projects, sid, 20, text="x" * 400)
    mark_session_logged(plugin, sid)

    gap = reconcile.find_gaps(storage, now=int(time.time()))[0]
    _, batches = reconcile.backfill_gap(
        storage, gap, device_id="dev", budget_bytes=10 * 1024 * 1024
    )

    assert batches > 1
    now = int(time.time()) + 1
    seqs, sizes = [], []
    while (row := storage.next_due_batch(now, sid)) is not None:
        seqs.append(row.batch_seq)
        sizes.append(len(row.body))
        storage.mark_success(row.id)
    assert seqs == sorted(set(seqs))  # unique + increasing
    assert all(s < 4000 for s in sizes)


def test_backfill_respects_the_per_sweep_budget_and_resumes_next_sweep(
    storage, projects, plugin, monkeypatch
):
    monkeypatch.setattr(reconcile, "MAX_BATCH_BYTES", 1000)
    sid = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    p = write_transcript(projects, sid, 30, text="y" * 300)
    mark_session_logged(plugin, sid)
    total = p.stat().st_size

    gap = reconcile.find_gaps(storage, now=int(time.time()))[0]
    first, _ = reconcile.backfill_gap(storage, gap, device_id="dev", budget_bytes=1500)
    assert 0 < first < total

    gap2 = reconcile.find_gaps(storage, now=int(time.time()))[0]
    assert gap2.byte_offset == first  # picks up exactly where it stopped
    second, _ = reconcile.backfill_gap(
        storage, gap2, device_id="dev", budget_bytes=10 * 1024 * 1024
    )
    assert first + second == total
    assert reconcile.find_gaps(storage, now=int(time.time())) == []


def test_backfill_leaves_cursor_untouched_when_enqueue_fails(storage, projects, plugin):
    """Same contract as the live tail: a failed enqueue re-reads, never skips."""
    sid = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    p = write_transcript(projects, sid, 5)
    mark_session_logged(plugin, sid)

    gap = reconcile.find_gaps(storage, now=int(time.time()))[0]
    with mock.patch("tap.outbox.enqueue", side_effect=sqlite3.IntegrityError("UNIQUE")):
        written, batches = reconcile.backfill_gap(
            storage, gap, device_id="dev", budget_bytes=10 * 1024 * 1024
        )

    assert (written, batches) == (0, 0)
    assert storage.get_offset(str(p)) is None
    assert len(reconcile.find_gaps(storage, now=int(time.time()))) == 1


def test_backfill_ignores_a_partial_trailing_line(storage, projects, plugin):
    """A line CC is mid-flush on must not be consumed or counted."""
    sid = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    p = write_transcript(projects, sid, 3)
    complete = p.stat().st_size
    with p.open("a", encoding="utf-8") as f:
        f.write('{"type":"user","uuid":"partial"')  # no newline

    mark_session_logged(plugin, sid)
    gap = reconcile.find_gaps(storage, now=int(time.time()))[0]
    reconcile.backfill_gap(storage, gap, device_id="dev", budget_bytes=10 * 1024 * 1024)

    assert storage.get_offset(str(p)).byte_offset == complete


def test_chunk_lines_keeps_an_oversized_single_line_intact():
    big = b"x" * 5000
    groups = reconcile.chunk_lines([b"a", big, b"b"], max_bytes=1000)
    assert [len(g) for g in groups] == [1, 1, 1]
    assert groups[1] == [big]


# ---------------------------------------------------------------------------
# global drain
# ---------------------------------------------------------------------------


def _ok(_url, _body, bearer=None):
    return Response(status=200, body=b"{}", classification=Classification.SUCCESS, error=None)


def test_global_drain_ships_rows_whose_session_never_came_back(storage):
    """The nine-day-stranded rows: no daemon for that session will ever run again."""
    now = int(time.time())
    for i, sid in enumerate(["dead-session-a", "dead-session-b"]):
        storage.enqueue_batch(session_id=sid, batch_seq=i, cwd="/repo",
                              body=b'{"x":1}', created_at=now - 9 * 86400,
                              next_attempt_at=now - 9 * 86400)

    # The old session-scoped path cannot see them: it only looks at its own id.
    assert storage.next_due_batch(now, "some-other-live-session") is None

    with mock.patch("tap.httpclient.post_json", side_effect=_ok):
        drained = reconcile.drain_all_due(storage, token="t", base_url="https://api.invalid")

    assert drained == 2
    assert storage.outbox_row_count() == 0


def test_global_drain_skips_rows_that_are_not_due_yet(storage):
    now = int(time.time())
    storage.enqueue_batch(session_id="s", batch_seq=0, cwd="/r", body=b"{}",
                          created_at=now, next_attempt_at=now + 3600)

    with mock.patch("tap.httpclient.post_json", side_effect=_ok):
        assert reconcile.drain_all_due(storage, token="t", base_url="https://api.invalid") == 0
    assert storage.outbox_row_count() == 1


def test_claiming_a_row_leases_it_away_from_other_drainers(storage):
    """Dropping the session scope reintroduced a race; the lease is what replaces it."""
    now = int(time.time())
    storage.enqueue_batch(session_id="s", batch_seq=0, cwd="/r", body=b"{}",
                          created_at=now, next_attempt_at=now)

    first = storage.next_due_batch_any(now, lease_seconds=120)
    second = storage.next_due_batch_any(now, lease_seconds=120)

    assert first is not None
    assert second is None  # invisible to a concurrent drainer
    # A lease is not a delivery attempt — the backoff curve must not advance.
    assert first.attempt_count == 0
    row = storage._conn.execute("SELECT attempt_count FROM outbox").fetchone()
    assert row[0] == 0
    # It comes back once the lease expires, so a daemon killed mid-POST costs delay only.
    assert storage.next_due_batch_any(now + 121, lease_seconds=120) is not None


def test_global_drain_preserves_poison_classification(storage):
    """A 413/422 body still drops, exactly as in the session-scoped path."""
    now = int(time.time())
    storage.enqueue_batch(session_id="s", batch_seq=0, cwd="/r", body=b"{}",
                          created_at=now, next_attempt_at=now)

    def poison(_u, _b, bearer=None):
        return Response(status=413, body=b"too big",
                        classification=Classification.POISON, error=None)

    with mock.patch("tap.httpclient.post_json", side_effect=poison):
        assert reconcile.drain_all_due(storage, token="t", base_url="https://api.invalid") == 1
    assert storage.outbox_row_count() == 0


def test_global_drain_preserves_halt_semantics(storage):
    """401 still clears the outbox, latches the fingerprint, and raises."""
    now = int(time.time())
    storage.enqueue_batch(session_id="s", batch_seq=0, cwd="/r", body=b"{}",
                          created_at=now, next_attempt_at=now)

    def halt(_u, _b, bearer=None):
        return Response(status=401, body=b"nope",
                        classification=Classification.HALT, error=None)

    with (
        mock.patch("tap.httpclient.post_json", side_effect=halt),
        pytest.raises(outbox.HaltError),
    ):
        reconcile.drain_all_due(storage, token="tok", base_url="https://api.invalid")

    assert storage.outbox_row_count() == 0
    assert storage.get_meta("last_401_at")
    assert storage.get_meta("last_401_token_sha256") == outbox.token_fingerprint("tok")


def test_global_drain_records_backoff_on_a_retryable_failure(storage):
    now = int(time.time())
    storage.enqueue_batch(session_id="s", batch_seq=0, cwd="/r", body=b"{}",
                          created_at=now, next_attempt_at=now)

    def boom(_u, _b, bearer=None):
        return Response(status=503, body=b"", classification=Classification.RETRY,
                        error="http 503")

    with mock.patch("tap.httpclient.post_json", side_effect=boom):
        reconcile.drain_all_due(storage, token="t", base_url="https://api.invalid")

    attempt, next_at = storage._conn.execute(
        "SELECT attempt_count, next_attempt_at FROM outbox"
    ).fetchone()
    assert attempt == 1
    # The real backoff replaces the lease rather than stacking on top of it.
    assert next_at < now + reconcile.OUTBOX_LEASE_SECONDS


# ---------------------------------------------------------------------------
# sweep + lease
# ---------------------------------------------------------------------------


def test_sweep_closes_a_gap_end_to_end(storage, projects, plugin):
    sid = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    p = write_transcript(projects, sid, 8)
    mark_session_logged(plugin, sid)

    with mock.patch("tap.httpclient.post_json", side_effect=_ok):
        res = reconcile.sweep(storage, token="t", base_url="https://api.invalid",
                              device_id="dev")

    assert res.files_backfilled == 1
    assert res.rows_drained >= 1
    assert storage.get_offset(str(p)).byte_offset == p.stat().st_size
    assert storage.outbox_row_count() == 0


def test_only_one_daemon_sweeps_at_a_time(storage, projects, plugin):
    sid = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    write_transcript(projects, sid, 5)
    mark_session_logged(plugin, sid)
    now = int(time.time())
    assert storage.try_claim_lease(reconcile.RECONCILE_LEASE_KEY, now,
                                   reconcile.RECONCILE_LEASE_SECONDS) is True

    res = reconcile.sweep(storage, token="t", base_url="https://api.invalid",
                          device_id="dev", now=now)

    assert res.skipped_no_lease is True
    assert res.files_backfilled == 0


def test_lease_expiry_lets_the_next_sweep_through(storage):
    now = int(time.time())
    assert storage.try_claim_lease("k", now, 60) is True
    assert storage.try_claim_lease("k", now + 10, 60) is False
    assert storage.try_claim_lease("k", now + 61, 60) is True


def test_sweep_survives_a_backfill_failure(storage, projects, plugin):
    """The net must never be the thing that stops capture."""
    sid = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    write_transcript(projects, sid, 5)
    mark_session_logged(plugin, sid)

    with (
        mock.patch("tap.reconcile.backfill_gap", side_effect=RuntimeError("boom")),
        mock.patch("tap.httpclient.post_json", side_effect=_ok),
    ):
        res = reconcile.sweep(storage, token="t", base_url="https://api.invalid",
                              device_id="dev")

    assert res.files_backfilled == 0  # contained, not raised


def test_sweep_caps_files_per_pass(storage, projects, plugin, monkeypatch):
    monkeypatch.setattr(reconcile, "MAX_BACKFILL_FILES_PER_SWEEP", 2)
    for i in range(5):
        sid = f"0000000{i}-0000-4000-8000-000000000000"
        write_transcript(projects, sid, 4)
        mark_session_logged(plugin, sid)

    with mock.patch("tap.httpclient.post_json", side_effect=_ok):
        res = reconcile.sweep(storage, token="t", base_url="https://api.invalid",
                              device_id="dev")

    assert res.gaps_found == 5
    assert res.files_backfilled == 2


# ---------------------------------------------------------------------------
# codex flavour
# ---------------------------------------------------------------------------


def test_codex_session_id_comes_off_the_rollout_filename(monkeypatch):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "codex")
    p = Path("/x/2026/08/12/rollout-2026-08-12T21-18-40-"
             "d51e3272-689c-42fc-b345-f3cad9b2693e.jsonl")
    assert reconcile.session_id_for(p) == "d51e3272-689c-42fc-b345-f3cad9b2693e"


def test_codex_transcript_root_follows_the_sessions_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "codex")
    monkeypatch.setenv("PRBE_CODEX_SESSIONS_DIR", str(tmp_path / "rollouts"))
    assert reconcile.transcript_root() == tmp_path / "rollouts"


def test_codex_gap_detection_uses_date_partitioned_rollouts(monkeypatch, tmp_path):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "codex")
    state = tmp_path / "codex-state"
    (state / "logs").mkdir(parents=True)
    monkeypatch.setenv("PRBE_CODEX_TAP_PLUGIN_DIR", str(state))
    sessions = tmp_path / "rollouts"
    monkeypatch.setenv("PRBE_CODEX_SESSIONS_DIR", str(sessions))
    sid = "d51e3272-689c-42fc-b345-f3cad9b2693e"
    day = sessions / "2026" / "08" / "12"
    day.mkdir(parents=True)
    (day / f"rollout-2026-08-12T21-18-40-{sid}.jsonl").write_text(
        _event(0) + "\n", encoding="utf-8"
    )
    (state / "logs" / f"{sid}.log").write_text("x", encoding="utf-8")

    s = Storage(state / "state.db")
    try:
        gaps = reconcile.find_gaps(s, now=int(time.time()))
    finally:
        s.close()

    assert [g.session_id for g in gaps] == [sid]


# ---------------------------------------------------------------------------
# transcript_root / session_id_for — registry-driven across all three sources
# ---------------------------------------------------------------------------

_PI_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "pi"


def test_transcript_root_resolves_the_default_session_root_for_all_three_sources(
    monkeypatch,
):
    """No override set: each source's un-overridden root comes from
    default_session_root on its registry row (tap/sources.py), not a literal
    path duplicated in reconcile.py."""
    monkeypatch.delenv("PROBE_RESEARCH_TAP_PROJECTS_DIR", raising=False)
    monkeypatch.delenv("PRBE_CODEX_SESSIONS_DIR", raising=False)

    monkeypatch.setenv("PROBE_TAP_SOURCE", "claude_code")
    assert reconcile.transcript_root() == Path.home() / ".claude" / "projects"

    monkeypatch.setenv("PROBE_TAP_SOURCE", "codex")
    assert reconcile.transcript_root() == Path.home() / ".codex" / "sessions"

    monkeypatch.setenv("PROBE_TAP_SOURCE", "pi")
    assert reconcile.transcript_root() == Path.home() / ".pi" / "agent" / "sessions"


def test_claude_code_session_id_is_the_whole_stem(monkeypatch):
    monkeypatch.setenv("PROBE_TAP_SOURCE", "claude_code")
    p = Path("/x/.claude/projects/-Users-me-proj/"
             "d51e3272-689c-42fc-b345-f3cad9b2693e.jsonl")
    assert reconcile.session_id_for(p) == "d51e3272-689c-42fc-b345-f3cad9b2693e"


def test_codex_session_id_extracts_the_uuid_suffix(monkeypatch):
    # Same assertion as test_codex_session_id_comes_off_the_rollout_filename
    # above, kept here too so the three-source comparison lives in one place.
    monkeypatch.setenv("PROBE_TAP_SOURCE", "codex")
    p = Path("/x/2026/08/12/rollout-2026-08-12T21-18-40-"
             "d51e3272-689c-42fc-b345-f3cad9b2693e.jsonl")
    assert reconcile.session_id_for(p) == "d51e3272-689c-42fc-b345-f3cad9b2693e"


def test_pi_session_id_extracts_the_uuid_suffix_of_real_fixture_filenames(monkeypatch):
    """pi's filenames are <timestamp>_<uuid>.jsonl — like Codex's
    rollout-<ts>-<uuid>.jsonl, a longer stem with the uuid only at the tail,
    unlike Claude Code's <session_id>.jsonl. Checked against real fixture
    files (tests/fixtures/pi/), not synthetic ones: each file's own header
    `id` is asserted to match what session_id_for() recovers from its name.
    """
    monkeypatch.setenv("PROBE_TAP_SOURCE", "pi")
    fixtures = sorted(_PI_FIXTURES_DIR.glob("*.jsonl"))
    assert fixtures, f"no pi fixtures found under {_PI_FIXTURES_DIR}"
    for path in fixtures:
        header = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
        assert reconcile.session_id_for(path) == header["id"]


def test_pi_gap_detection_uses_shape_based_discovery(monkeypatch, tmp_path):
    """find_gaps() for the pi source goes through pi_discovery, not a raw
    root.rglob("*.jsonl") — proven two ways: (1) a session living under a
    configured PROBE_PI_SESSION_ROOTS root (not the hardcoded default) is
    still found, and (2) a decoy file that LOOKS like a pi session by
    filename (a valid <timestamp>_<uuid>.jsonl name, so session_id_for()
    would happily extract a session id from it) but is NOT one by content is
    excluded — something filename/glob-based enumeration cannot do.
    """
    monkeypatch.setenv("PROBE_TAP_SOURCE", "pi")
    state = tmp_path / "pi-state"
    (state / "logs").mkdir(parents=True)
    monkeypatch.setenv("PROBE_PI_TAP_PLUGIN_DIR", str(state))
    sessions_root = tmp_path / "custom-pi-root"
    sessions_root.mkdir()
    monkeypatch.setenv("PROBE_PI_SESSION_ROOTS", str(sessions_root))

    fixture = sorted(_PI_FIXTURES_DIR.glob("*.jsonl"))[0]
    real_session = sessions_root / fixture.name
    shutil.copy(fixture, real_session)
    header = json.loads(real_session.read_text(encoding="utf-8").splitlines()[0])
    real_sid = header["id"]
    (state / "logs" / f"{real_sid}.log").write_text("x", encoding="utf-8")

    # Same filename SHAPE as a pi session (<timestamp>_<uuid>.jsonl, uuid
    # trailing) but a Codex-style header — session_id_for() extracts a
    # session id from the name alone, so this only gets excluded if
    # find_gaps() actually verifies file content via pi_discovery.
    decoy_sid = "22222222-2222-4222-8222-222222222222"
    decoy = sessions_root / f"2026-01-01T00-00-00-000Z_{decoy_sid}.jsonl"
    decoy.write_text(
        json.dumps({"timestamp": "t", "type": "session_meta",
                    "payload": {"id": decoy_sid}}) + "\n",
        encoding="utf-8",
    )
    (state / "logs" / f"{decoy_sid}.log").write_text("x", encoding="utf-8")
    assert reconcile.session_id_for(decoy) == decoy_sid  # would be eligible by name alone

    s = Storage(state / "state.db")
    try:
        gaps = reconcile.find_gaps(s, now=int(time.time()))
    finally:
        s.close()

    assert [g.session_id for g in gaps] == [real_sid]


# ---------------------------------------------------------------------------
# liveness probe is read-only
# ---------------------------------------------------------------------------


def test_has_live_daemon_never_signals_or_unlinks(tmp_path):
    """An unexplained teardown SIGTERMs + unlinks these exact files. Stay read-only."""
    sid = "12121212-1212-4121-8121-121212121212"
    pid_file = Path(f"/tmp/probe-research-tap-watcher-{sid}.pid")
    pid_file.write_text(str(os.getpid()), encoding="utf-8")
    try:
        with mock.patch("os.kill") as killer:
            assert reconcile.has_live_daemon(sid) is True
            killer.assert_called_once_with(os.getpid(), 0)  # signal 0 only
        assert pid_file.exists()
    finally:
        pid_file.unlink(missing_ok=True)


def test_has_live_daemon_false_for_a_stale_pid_file(tmp_path):
    sid = "13131313-1313-4131-8131-131313131313"
    pid_file = Path(f"/tmp/probe-research-tap-watcher-{sid}.pid")
    pid_file.write_text("999999", encoding="utf-8")
    try:
        assert reconcile.has_live_daemon(sid) is False
    finally:
        pid_file.unlink(missing_ok=True)


def test_has_live_daemon_false_when_no_pid_file():
    assert reconcile.has_live_daemon("no-such-session-at-all") is False


# --- the horizon gates adoption, not tracked files ---------------------------


def _old(path: Path, *, days: int = 30) -> None:
    """Age a transcript far past the reconcile horizon."""
    stamp = time.time() - days * 86400
    os.utime(path, (stamp, stamp))


def test_a_tracked_gap_older_than_the_horizon_still_uploads(projects, storage):
    """THE HORIZON FIX. The window gates ADOPTION, not files we already hold a
    cursor for: a machine off for a week used to strand its tracked gaps
    forever."""
    old_file = write_transcript(projects, "gap-6666", 5)
    _old(old_file, days=7)
    storage.upsert_offset(
        FileOffset(
            path=str(old_file),
            session_id="gap-6666",
            cwd=str(old_file.parent),
            last_line_no=1,
            last_seen_at=1,
            inode=0,
            size=1,
            byte_offset=1,
        )
    )

    gaps = reconcile.find_gaps(storage, now=int(time.time()))

    assert [g.session_id for g in gaps] == ["gap-6666"]

    # ...while an UNTRACKED old file with a session log stays outside the
    # window: the horizon still bounds what an ordinary sweep newly adopts.
    untracked = write_transcript(projects, "gap-7777", 5)
    _old(untracked, days=7)
    plugin_dir = Path(os.environ["PROBE_RESEARCH_TAP_PLUGIN_DIR"])
    mark_session_logged(plugin_dir, "gap-7777")
    gaps = reconcile.find_gaps(storage, now=int(time.time()))
    assert [g.session_id for g in gaps] == ["gap-6666"]


def test_a_cursor_whose_file_changed_identity_is_skipped_not_misread(projects, storage):
    """With tracked files horizon-exempt, the inode is the guard against a
    path that changed identity under its cursor — reading a DIFFERENT file
    from the old offset ships unrelated bytes under the old session."""
    path = write_transcript(projects, "swap-8888", 5)
    _old(path, days=7)  # past the horizon: the exact case the window used to bound
    real_inode = path.stat().st_ino
    storage.upsert_offset(
        FileOffset(
            path=str(path),
            session_id="swap-8888",
            cwd=str(path.parent),
            last_line_no=1,
            last_seen_at=1,
            inode=real_inode + 1,  # the cursor remembers a different file
            size=1,
            byte_offset=1,
        )
    )

    assert reconcile.find_gaps(storage, now=int(time.time())) == []

    # The matching inode is still an ordinary gap.
    storage.upsert_offset(
        FileOffset(
            path=str(path),
            session_id="swap-8888",
            cwd=str(path.parent),
            last_line_no=1,
            last_seen_at=1,
            inode=real_inode,
            size=1,
            byte_offset=1,
        )
    )
    assert [g.session_id for g in reconcile.find_gaps(storage, now=int(time.time()))] == [
        "swap-8888"
    ]
