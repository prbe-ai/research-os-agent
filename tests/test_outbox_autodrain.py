"""Parity F1/F7 (docs/2026-08-04-outbox-miles-parity.md): async SDK writes
kick the detached outbox worker, and an async client that could never deliver
fails at construction instead of hours later in a drainer log.

The worker loop itself is covered by test_outbox.py; these tests pin WHO kicks
it and when, via a monkeypatched ``maybe_spawn`` spy.
"""

from __future__ import annotations

import pytest

from probe.sdk import errors
from probe.sdk.client import Client
from probe.sdk.config import Settings
from probe.sdk.journal import Journal

from tests.conftest import make_client


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(tmp_path / "config" / "probe.json"))
    monkeypatch.delenv("PROBE_TOKEN", raising=False)
    monkeypatch.delenv("PROBE_ASYNC", raising=False)


@pytest.fixture
def kicks(monkeypatch):
    calls: list[str] = []

    def spy(directory=None):
        calls.append(str(directory))
        return True

    monkeypatch.setattr("probe.sdk.outbox_worker.maybe_spawn", spy)
    return calls


def _settings(**overrides) -> Settings:
    fields = {
        "base_url": "http://test",
        "token": "ros_pat_deadbeef",
        "ingest_token": None,
        "hmac_secret": None,
    }
    fields.update(overrides)
    return Settings(**fields)


def _async_client(tmp_path, **kw) -> Client:
    kw.setdefault("settings", _settings())
    kw.setdefault(
        "journal",
        Journal(tmp_path / "outbox", context={"name": None, "base_url": "http://test"}),
    )
    return Client(async_writes=True, **kw)


def test_async_write_kicks_the_worker_but_throttled(tmp_path, kicks):
    with _async_client(tmp_path) as client:
        journal_dir = str(client.journal.dir)
        client.write("POST", "/v1/runs/r1/metrics", {"points": []})
        client.write("POST", "/v1/runs/r1/metrics", {"points": []})
    # Two back-to-back writes, ONE kick: maybe_spawn is O(1) but this path
    # runs per logged point in a training loop.
    assert kicks == [journal_dir]
    assert len(Journal(tmp_path / "outbox").pending()) == 2


def test_kick_repeats_once_the_throttle_window_passes(tmp_path, kicks):
    with _async_client(tmp_path) as client:
        client._drainer_kick_interval = 0.0
        client.write("POST", "/v1/runs/r1/metrics", {"points": []})
        client.write("POST", "/v1/runs/r1/metrics", {"points": []})
    assert len(kicks) == 2


def test_custom_transport_never_spawns(app, tmp_path, kicks):
    # A detached worker resolves its own transport from config -- it could
    # never replay through this injected fake, so auto-drain must stay off.
    client = make_client(app, tmp_spool=tmp_path / "outbox", async_writes=True)
    client.write("POST", "/v1/runs/r1/metrics", {"points": []})
    assert kicks == []
    assert len(client.journal.pending()) == 1


def test_auto_drain_false_never_spawns(tmp_path, kicks):
    with _async_client(tmp_path, auto_drain=False) as client:
        client.write("POST", "/v1/runs/r1/metrics", {"points": []})
    assert kicks == []


def test_sync_mode_never_spawns_even_when_fail_open_journals(tmp_path, kicks, monkeypatch):
    client = Client(settings=_settings(), journal=Journal(tmp_path / "outbox"))

    def boom(*a, **kw):
        raise errors.TransportError("network down")

    monkeypatch.setattr(client.transport, "request", boom)
    with client:
        assert client.write("POST", "/v1/runs/r1/metrics", {"points": []}) is None
    assert len(Journal(tmp_path / "outbox").pending()) == 1
    assert kicks == []


def test_async_without_credentials_refuses_at_construction(tmp_path):
    with pytest.raises(errors.ValidationError) as excinfo:
        Client(
            async_writes=True,
            settings=_settings(token=None),
            journal=Journal(tmp_path / "outbox"),
        )
    assert "probe login" in str(excinfo.value)
    assert "auto_drain=False" in str(excinfo.value)


def test_auto_drain_false_skips_the_credential_gate(tmp_path, kicks):
    with _async_client(tmp_path, settings=_settings(token=None), auto_drain=False) as client:
        client.write("POST", "/v1/runs/r1/metrics", {"points": []})
    assert len(Journal(tmp_path / "outbox").pending()) == 1
    assert kicks == []


def test_custom_transport_skips_the_credential_gate(app, tmp_path):
    # make_client injects a transport; even with credentials stripped the gate
    # must not fire -- whoever owns the transport owns delivery.
    client = make_client(app, tmp_spool=tmp_path / "outbox", async_writes=True)
    client.settings.token = None
    client.settings.ingest_token = None
    assert Client(
        async_writes=True,
        settings=_settings(token=None),
        transport=client.transport,
        journal=Journal(tmp_path / "outbox2"),
    )


def test_cli_shim_is_the_sdk_worker():
    from probe.cli import outbox_worker as cli_worker
    from probe.sdk import outbox_worker as sdk_worker

    assert cli_worker.maybe_spawn is sdk_worker.maybe_spawn
    assert cli_worker.run is sdk_worker.run
