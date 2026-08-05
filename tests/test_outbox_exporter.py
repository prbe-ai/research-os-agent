"""Parity F2 (docs/2026-08-04-outbox-miles-parity.md): the in-process outbox
exporter -- fork-free delivery that rides the client's own transport.

Timing-sensitive assertions poll with deadlines rather than sleeping fixed
amounts. Intervals are LONG (30s) where wake-on-enqueue must prove itself and
short (0.05s) where the interval poll is the subject.
"""

from __future__ import annotations

import fcntl
import time

import pytest

from probe.sdk import errors
from probe.sdk.client import Client
from probe.sdk.config import Settings
from probe.sdk.journal import DrainReport, Journal
from probe.sdk.outbox_worker import _lease_path

from tests.conftest import make_client
from tests.test_outbox import seeded_run


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(tmp_path / "config" / "probe.json"))
    monkeypatch.delenv("PROBE_TOKEN", raising=False)
    monkeypatch.delenv("PROBE_ASYNC", raising=False)
    monkeypatch.delenv("PROBE_EXPORT_INTERVAL_SEC", raising=False)


def _wait(predicate, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _point(step: int = 1) -> dict:
    return {"points": [{"key": "loss", "kind": "model", "value": 1.0, "step_index": step}]}


def test_wake_on_enqueue_delivers_without_waiting_an_interval(app, tmp_path):
    run_id = seeded_run(app, tmp_path)
    client = make_client(
        app, tmp_spool=tmp_path / "outbox", async_writes=True, drain_interval=30.0
    )
    client.write("POST", f"/v1/runs/{run_id}/metrics", _point())
    # 30s interval, ~instant delivery: only the wake can explain it.
    assert _wait(lambda: len(client.journal.pending()) == 0)
    client.close()


def test_exporter_defers_to_a_held_worker_lease_then_takes_over(app, tmp_path):
    run_id = seeded_run(app, tmp_path)
    client = make_client(
        app, tmp_spool=tmp_path / "outbox", async_writes=True, drain_interval=0.05
    )
    client.journal._ensure()
    holder = open(_lease_path(client.journal), "a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        client.write("POST", f"/v1/runs/{run_id}/metrics", _point())
        time.sleep(0.4)  # many intervals; the foreign lease must hold it off
        assert len(client.journal.pending()) == 1
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()
    assert _wait(lambda: len(client.journal.pending()) == 0)
    client.close()


def test_auth_block_stops_the_exporter_with_ops_intact(app, tmp_path, monkeypatch):
    client = make_client(
        app, tmp_spool=tmp_path / "outbox", async_writes=True, drain_interval=0.05
    )

    def revoked(*a, **kw):
        raise errors.AuthError("expired", status=401)

    monkeypatch.setattr(client.transport, "request", revoked)
    client.write("POST", "/v1/runs/r-1/metrics", _point())
    assert _wait(lambda: client._exporter is not None and not client._exporter.alive)
    assert len(client.journal.pending()) == 1  # queued untouched (T2-A)
    assert Journal.read_status(client.journal.dir)["auth_blocked_since"]
    client.close()


def test_close_joins_the_exporter(app, tmp_path):
    run_id = seeded_run(app, tmp_path)
    client = make_client(
        app, tmp_spool=tmp_path / "outbox", async_writes=True, drain_interval=0.05
    )
    client.write("POST", f"/v1/runs/{run_id}/metrics", _point())
    assert _wait(lambda: len(client.journal.pending()) == 0)
    exporter = client._exporter
    client.close()
    assert not exporter.alive


def test_maybe_spawn_defers_to_a_live_exporter(app, tmp_path, monkeypatch):
    from probe.sdk import outbox_worker

    client = make_client(
        app, tmp_spool=tmp_path / "outbox", async_writes=True, drain_interval=0.05
    )

    def down(*a, **kw):
        raise errors.TransportError("net down")

    monkeypatch.setattr(client.transport, "request", down)
    client.write("POST", "/v1/runs/r-1/metrics", _point())
    # Transient failures leave the thread alive and the lease held...
    assert _wait(
        lambda: client._exporter is not None and client._exporter._lease_handle is not None
    )
    # ...so the fork path must see an owned journal and decline.
    assert outbox_worker.maybe_spawn(str(client.journal.dir)) is False
    client.close()


def test_env_interval_activates_the_exporter(app, tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_EXPORT_INTERVAL_SEC", "0.05")
    run_id = seeded_run(app, tmp_path)
    client = make_client(app, tmp_spool=tmp_path / "outbox", async_writes=True)
    client.write("POST", f"/v1/runs/{run_id}/metrics", _point())
    assert client._exporter is not None
    assert _wait(lambda: len(client.journal.pending()) == 0)
    client.close()


def test_interval_honors_the_miles_floor(app, tmp_path):
    client = make_client(
        app, tmp_spool=tmp_path / "outbox", async_writes=True, drain_interval=0.0
    )
    client.write("POST", "/v1/runs/r-1/metrics", _point())
    assert client._exporter.interval == 0.05
    client.close()


def test_exporter_supersedes_the_worker_kick(tmp_path, monkeypatch):
    kicks: list = []
    monkeypatch.setattr(
        "probe.sdk.outbox_worker.maybe_spawn", lambda d=None: kicks.append(d) or True
    )
    drains: list = []
    monkeypatch.setattr(
        "probe.sdk.exporter.drain", lambda j, **kw: drains.append(1) or DrainReport()
    )
    # Default transport, so the worker WOULD have been kicked -- drain_interval
    # must reroute delivery to the in-process exporter instead.
    client = Client(
        async_writes=True,
        drain_interval=0.05,
        settings=Settings(
            base_url="http://test", token="ros_pat_x", ingest_token=None, hmac_secret=None
        ),
        journal=Journal(tmp_path / "outbox", context={"name": None, "base_url": "http://test"}),
    )
    client.write("POST", "/v1/runs/r-1/metrics", _point())
    assert _wait(lambda: len(drains) > 0)
    assert kicks == []
    client.close()


def test_default_transport_exporter_still_needs_credentials(tmp_path):
    with pytest.raises(errors.ValidationError):
        Client(
            async_writes=True,
            auto_drain=False,
            drain_interval=1.0,
            settings=Settings(
                base_url="http://test", token=None, ingest_token=None, hmac_secret=None
            ),
            journal=Journal(tmp_path / "outbox"),
        )
