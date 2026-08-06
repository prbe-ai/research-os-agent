"""Wiring the collector into the SDK: default-on at run(), invisible to the
resume machinery, drop-not-spool through the single write funnel.

Contracts (each maps to a locked review decision):
- run() starts a collector by default; PROBE_HW=0 / run(hw=False) disable;
  only the node-local leader starts one (LOCAL_RANK heuristics);
  finish() stops it.
- The resume guard is KIND-SCOPED: hardware's epoch steps (~29.7M) neither
  trip it nor arm it. A receipt whose last_step is implausibly high came
  from a server that predates the hw-exclusion fix — warn and skip arming
  (warn-never-gate) rather than poison every training log call.
- write(durable=False) never touches the journal: hardware is best-effort,
  the monitor's bounded buffer is its only retry, spool space belongs to
  training metrics.
- Inventory mints a minimal execution record (hardware= field) only when the
  run has no env_ref yet; a real snapshot's record is never clobbered.
"""

from __future__ import annotations


import pytest

import probe.hw.integration as hw_integration
from probe import errors
from tests.conftest import open_run


class RecorderMonitor:
    """Stands in for HwMonitor: records lifecycle, runs no threads."""

    instances: list = []

    def __init__(self, *a, **kw):
        self.started = False
        self.finished = False
        self.kwargs = kw
        RecorderMonitor.instances.append(self)

    def start(self):
        self.started = True

    def finish(self):
        self.finished = True


@pytest.fixture(autouse=True)
def _hw_test_env(monkeypatch):
    """Deterministic election + no real monitor threads in these tests."""
    RecorderMonitor.instances = []
    monkeypatch.setattr(hw_integration, "HwMonitor", RecorderMonitor)
    monkeypatch.setenv("LOCAL_RANK", "0")
    monkeypatch.delenv("PROBE_HW", raising=False)
    yield


def test_run_starts_hw_monitor_by_default_and_finish_stops(client, app):
    run = open_run(client, experiment="hw-e2e")
    monitor = run._hw_monitor
    assert monitor is not None and monitor.started

    run.finish()
    assert monitor.finished
    assert run._hw_monitor is None  # handle releases its collector on close


def test_probe_hw_env_kill_switch(client, app, monkeypatch):
    monkeypatch.setenv("PROBE_HW", "0")
    run = open_run(client, experiment="hw-e2e")
    assert run._hw_monitor is None
    run.finish()


def test_run_hw_false_disables(client, app):
    run = open_run(client, experiment="hw-e2e", hw=False)
    assert run._hw_monitor is None
    run.finish()


def test_non_leader_rank_starts_nothing(client, app, monkeypatch):
    monkeypatch.setenv("LOCAL_RANK", "3")
    run = open_run(client, experiment="hw-e2e")
    assert run._hw_monitor is None
    run.finish()


# -- resume machinery -------------------------------------------------------


def test_resume_guard_is_kind_scoped(client, app):
    run = open_run(client, experiment="hw-e2e", hw=False)
    run.arm_resume_guard(100)

    with pytest.raises(errors.ValidationError):
        run.log({"loss": 1.0}, step=50)  # training kind: guarded

    # Hardware's epoch steps are invisible to the guard in BOTH directions:
    # a huge step doesn't have to clear the training floor…
    run.log_hw({"gpu_temp": 40.0}, step=29_700_000)
    # …and a small hw step doesn't trip it either (different clock entirely).
    run.log_hw({"gpu_temp": 41.0}, step=50)
    run.finish()


def test_suspect_receipt_warns_and_skips_arming(client, app):
    """A last_step in hardware's epoch range means the server predates the
    receipt hw-exclusion: arming would refuse every training step. Fail open
    with one warning (warn-never-gate)."""
    run = open_run(client, experiment="hw-e2e", hw=False)
    with pytest.warns(UserWarning, match="hardware"):
        handle = client.attach_run(
            {"id": run.id}, heartbeat=False, resume_from_step=29_700_123
        )
    assert handle._resume_from_step is None  # not armed
    handle.log({"loss": 1.0}, step=5)  # would raise if armed
    run.finish()


# -- transport durability ---------------------------------------------------


def test_write_durable_false_never_journals(client, app):
    run = open_run(client, experiment="hw-e2e", hw=False)
    client.async_writes = True
    try:
        client.write(
            "POST",
            f"/v1/runs/{run.id}/metrics",
            {"points": [{"key": "hw/cpu/utilization", "value": 1.0, "kind": "hardware", "step_index": 29_700_000}]},
            durable=False,
        )
        # Async mode journals EVERYTHING durable — durable=False must bypass
        # the journal and go straight to the wire.
        assert list(client.journal.pending()) == []
    finally:
        client.async_writes = False
        run.finish()


def test_write_durable_false_raises_on_failure_without_journaling(client, app, monkeypatch):
    """The monitor's bounded buffer is hardware's ONLY retry: the funnel must
    surface the failure (so the buffer can hold the points) and must never
    journal them (spool space belongs to training metrics)."""

    def boom(*a, **kw):
        raise errors.RosError("outage")

    monkeypatch.setattr(client.transport, "request", boom)
    with pytest.raises(errors.RosError):
        client.write("POST", "/v1/x", {"points": []}, durable=False)
    assert list(client.journal.pending()) == []


# -- inventory --------------------------------------------------------------


def test_inventory_mints_minimal_record_when_env_ref_absent(client, app):
    run = open_run(client, experiment="hw-e2e", hw=False)
    calls = []
    orig_write = client.write

    def spy(method, path, body=None, **kw):
        calls.append((method, path, body))
        return orig_write(method, path, body, **kw)

    client.write = spy
    try:
        hw_integration.publish_inventory(
            client, run_id=run.id, env_ref=None, inventory={"gpu_count": 2}
        )
    finally:
        client.write = orig_write

    posts = [c for c in calls if c[1] == "/v1/execution-records"]
    assert len(posts) == 1
    assert posts[0][2]["hardware"] == {"gpu_count": 2}
    run.finish()


def test_inventory_skips_when_snapshot_already_pinned(client, app):
    run = open_run(client, experiment="hw-e2e", hw=False)
    calls = []
    client_write = client.write
    client.write = lambda *a, **kw: calls.append(a) or client_write(*a, **kw)
    try:
        hw_integration.publish_inventory(
            client, run_id=run.id, env_ref="abc123", inventory={"gpu_count": 2}
        )
    finally:
        client.write = client_write

    assert not any("/v1/execution-records" in str(c) for c in calls)
    run.finish()
