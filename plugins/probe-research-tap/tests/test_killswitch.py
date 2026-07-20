"""Tests for the client-side killswitch — cache + fail-open + run-loop wiring."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest


@pytest.fixture(autouse=True)
def _isolated_plugin_dir(monkeypatch):
    tmp = tempfile.mkdtemp(prefix="probe-research-tap-killswitch-test-")
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", tmp)
    # _run_loop requires a configured backend host (no hardcoded fallback).
    monkeypatch.setenv("PROBE_BASE_URL", "https://api.invalid")
    yield Path(tmp)


@pytest.fixture(autouse=True)
def _reset_killswitch_cache():
    from tap import killswitch

    killswitch.reset_cache()
    yield
    killswitch.reset_cache()


# ---------------------------------------------------------------------------
# is_ingestion_enabled — happy paths
# ---------------------------------------------------------------------------


def _make_response(*, status: int, body: dict | None, error: str = ""):
    """Build a tap.httpclient.Response for the mocked get_json."""
    from tap import httpclient

    raw = json.dumps(body).encode() if body is not None else b""
    return httpclient.Response(
        status=status,
        body=raw,
        classification=httpclient.classify(status, err=bool(error)),
        error=error,
    )


def test_polls_ingest_sessions_status_endpoint() -> None:
    """The remote killswitch lives at GET /ingest/v1/sessions/status on the
    Research OS backend (not the hosted plugin's /agent-tap path)."""
    from tap import killswitch

    assert killswitch.PATH == "/ingest/v1/sessions/status"

    resp = _make_response(status=200, body={"ingest_enabled": True, "reason": None})
    fake = mock.Mock(return_value=resp)
    with mock.patch("tap.killswitch.httpclient.get_json", fake):
        killswitch.is_ingestion_enabled(token="t", base_url="https://api.example")
    assert fake.call_args[0][0] == "https://api.example/ingest/v1/sessions/status"


def test_returns_enabled_true_when_server_says_enabled() -> None:
    from tap import killswitch

    resp = _make_response(status=200, body={"ingest_enabled": True, "reason": None})
    with mock.patch("tap.killswitch.httpclient.get_json", return_value=resp):
        enabled, reason = killswitch.is_ingestion_enabled(
            token="t", base_url="https://api.prbe.ai"
        )
    assert enabled is True
    assert reason is None


def test_returns_disabled_with_reason_when_server_says_so() -> None:
    from tap import killswitch

    resp = _make_response(
        status=200, body={"ingest_enabled": False, "reason": "maintenance"}
    )
    with mock.patch("tap.killswitch.httpclient.get_json", return_value=resp):
        enabled, reason = killswitch.is_ingestion_enabled(
            token="t", base_url="https://api.prbe.ai"
        )
    assert enabled is False
    assert reason == "maintenance"


def test_caches_within_ttl() -> None:
    """Hot path — daemon polls every tick. Only one HTTP call per 5-min window."""
    from tap import killswitch

    resp = _make_response(status=200, body={"ingest_enabled": True, "reason": None})
    fake = mock.Mock(return_value=resp)
    with mock.patch("tap.killswitch.httpclient.get_json", fake):
        for _ in range(20):
            killswitch.is_ingestion_enabled(token="t", base_url="https://x")
    assert fake.call_count == 1


def test_cache_expires_after_ttl() -> None:
    from tap import killswitch

    resp_a = _make_response(status=200, body={"ingest_enabled": True, "reason": None})
    resp_b = _make_response(
        status=200, body={"ingest_enabled": False, "reason": "flipped"}
    )
    fake = mock.Mock(side_effect=[resp_a, resp_b])

    with (
        mock.patch("tap.killswitch.httpclient.get_json", fake),
        mock.patch("tap.killswitch.time.monotonic") as fake_time,
    ):
        fake_time.return_value = 0.0
        en1, _ = killswitch.is_ingestion_enabled(token="t", base_url="https://x")
        # Jump past the 5-min TTL.
        fake_time.return_value = killswitch.KILLSWITCH_TTL_S + 1
        en2, reason = killswitch.is_ingestion_enabled(token="t", base_url="https://x")

    assert en1 is True
    assert en2 is False
    assert reason == "flipped"
    assert fake.call_count == 2


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


def test_network_error_with_no_cache_fails_open() -> None:
    """No prior poll succeeded + the call fails → assume enabled."""
    from tap import killswitch

    resp = _make_response(status=0, body=None, error="Connection refused")
    with mock.patch("tap.killswitch.httpclient.get_json", return_value=resp):
        enabled, reason = killswitch.is_ingestion_enabled(
            token="t", base_url="https://x"
        )
    assert enabled is True
    assert reason is None


def test_500_error_fails_open() -> None:
    from tap import killswitch

    resp = _make_response(status=500, body={"err": "boom"})
    with mock.patch("tap.killswitch.httpclient.get_json", return_value=resp):
        enabled, _ = killswitch.is_ingestion_enabled(token="t", base_url="https://x")
    assert enabled is True


def test_401_fails_open_not_halt() -> None:
    """A bad device token shouldn't halt ingestion globally — that's the
    job of the webhook drain (which DOES halt on 401). The killswitch poll
    is just an optimization; if our auth is wrong, the webhook will
    surface the issue separately."""
    from tap import killswitch

    resp = _make_response(status=401, body={"err": "unauthorized"})
    with mock.patch("tap.killswitch.httpclient.get_json", return_value=resp):
        enabled, _ = killswitch.is_ingestion_enabled(token="t", base_url="https://x")
    assert enabled is True


def test_failure_uses_fresh_cached_value() -> None:
    """Healthy poll fills cache; later poll fails inside STALE_FALLBACK_LIMIT
    → keep using the cached value rather than fail-opening to a default."""
    from tap import killswitch

    good = _make_response(
        status=200, body={"ingest_enabled": False, "reason": "paused"}
    )
    bad = _make_response(status=0, body=None, error="Connection refused")
    fake = mock.Mock(side_effect=[good, bad])

    with (
        mock.patch("tap.killswitch.httpclient.get_json", fake),
        mock.patch("tap.killswitch.time.monotonic") as fake_time,
    ):
        fake_time.return_value = 0.0
        en1, r1 = killswitch.is_ingestion_enabled(token="t", base_url="https://x")
        # Past TTL but inside STALE_FALLBACK_LIMIT — refetch happens, fails,
        # we should still see the cached "paused" state.
        fake_time.return_value = killswitch.KILLSWITCH_TTL_S + 60
        en2, r2 = killswitch.is_ingestion_enabled(token="t", base_url="https://x")

    assert (en1, r1) == (False, "paused")
    assert (en2, r2) == (False, "paused"), (
        "stale-but-fresh-enough cache should survive a refetch failure"
    )
    assert fake.call_count == 2


def test_failure_with_stale_cache_falls_back_to_open() -> None:
    """Cached value older than STALE_FALLBACK_LIMIT_S + refetch fails →
    we no longer trust the cache. Fail OPEN."""
    from tap import killswitch

    good = _make_response(
        status=200, body={"ingest_enabled": False, "reason": "paused"}
    )
    bad = _make_response(status=0, body=None, error="Connection refused")
    fake = mock.Mock(side_effect=[good, bad])

    with (
        mock.patch("tap.killswitch.httpclient.get_json", fake),
        mock.patch("tap.killswitch.time.monotonic") as fake_time,
    ):
        fake_time.return_value = 0.0
        killswitch.is_ingestion_enabled(token="t", base_url="https://x")
        fake_time.return_value = killswitch.STALE_FALLBACK_LIMIT_S + 1
        en, r = killswitch.is_ingestion_enabled(token="t", base_url="https://x")

    assert en is True
    assert r is None


# ---------------------------------------------------------------------------
# Run-loop integration: daemon skips entire tick when killswitch off
# ---------------------------------------------------------------------------


def _make_watch_config(tmp_path: Path):
    from tap import config as cfg

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text("{}\n")
    return cfg.WatchConfig(
        session_id="ks-test",
        transcript_path=transcript,
        cwd=tmp_path,
        plugin_root=Path("/nonexistent"),
        token="fake-token",
        active_interval_s=60,
        idle_interval_s=300,
    )


def test_run_loop_skips_tick_when_killswitch_off(_isolated_plugin_dir, tmp_path):
    """Killswitch off: daemon should NOT call _tick_read, outbox.enqueue, or
    outbox.drain_once. It just sleeps and continues to the next iteration."""
    from tap import config as cfg, main as tapmain
    from tap.storage import Storage

    config = _make_watch_config(tmp_path)
    storage = Storage(cfg.state_db_path())

    iteration = {"i": 0}

    def fake_killswitch(*, token, base_url):
        i = iteration["i"]
        iteration["i"] = i + 1
        # First two ticks: paused. Third tick: trip the shutdown sentinel
        # so the loop exits cleanly.
        if i >= 2:
            config.shutdown_sentinel.touch()
        return False, "test pause"

    tick_calls = mock.Mock()
    enqueue_calls = mock.Mock()
    drain_calls = mock.Mock(return_value=False)

    try:
        with (
            mock.patch(
                "tap.main.killswitch.is_ingestion_enabled",
                side_effect=fake_killswitch,
            ),
            mock.patch("tap.main._tick_read", side_effect=tick_calls),
            mock.patch("tap.main.outbox.enqueue", side_effect=enqueue_calls),
            mock.patch("tap.main.outbox.drain_once", side_effect=drain_calls),
            mock.patch("tap.main._transcript_has_active_reader", return_value=None),
            mock.patch("tap.main.time.sleep", return_value=None),
        ):
            tapmain._run_loop(config, storage)
    finally:
        storage.close()
        try:
            config.shutdown_sentinel.unlink()
        except FileNotFoundError:
            pass

    assert tick_calls.call_count == 0, "should not tail the transcript when paused"
    assert enqueue_calls.call_count == 0, "should not enqueue when paused"
    assert drain_calls.call_count == 0, "should not drain when paused"


def test_run_loop_resumes_after_killswitch_releases(_isolated_plugin_dir, tmp_path):
    """First tick paused; second tick released. Daemon should run a normal
    tick (tail + enqueue + drain) on the second iteration."""
    from tap import config as cfg, main as tapmain
    from tap.storage import Storage

    config = _make_watch_config(tmp_path)
    storage = Storage(cfg.state_db_path())

    ks_states = iter([(False, "x"), (True, None), (True, None)])

    def fake_killswitch(*, token, base_url):
        try:
            return next(ks_states)
        except StopIteration:
            config.shutdown_sentinel.touch()
            return True, None

    tick_calls = mock.Mock(return_value=([b'{}'], 0, lambda: None))
    enqueue_calls = mock.Mock()
    drain_calls = mock.Mock(return_value=False)

    try:
        with (
            mock.patch(
                "tap.main.killswitch.is_ingestion_enabled",
                side_effect=fake_killswitch,
            ),
            mock.patch("tap.main._tick_read", side_effect=tick_calls),
            mock.patch(
                "tap.main.outbox.build_batch_body", return_value=b'{"x":1}'
            ),
            mock.patch("tap.main.outbox.enqueue", side_effect=enqueue_calls),
            mock.patch("tap.main.outbox.drain_once", side_effect=drain_calls),
            mock.patch("tap.main._transcript_has_active_reader", return_value=None),
            mock.patch("tap.main.time.sleep", return_value=None),
        ):
            tapmain._run_loop(config, storage)
    finally:
        storage.close()
        try:
            config.shutdown_sentinel.unlink()
        except FileNotFoundError:
            pass

    # First tick paused (no work). Subsequent ticks worked.
    assert tick_calls.call_count >= 1, (
        "should tail the transcript on the unpaused ticks"
    )
    assert enqueue_calls.call_count >= 1, "should enqueue once unpaused"
