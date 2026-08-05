"""Parity F3/F3a (docs/2026-08-04-outbox-miles-parity.md): bounded finish.

The default finish() stays a HARD barrier. With flush_timeout (or
PROBE_FINISH_TIMEOUT_SEC), a run that cannot drain in time queues its
terminal status BEHIND its pending ops -- FIFO is the correctness guarantee:
the run can never read terminal ahead of its data -- stamped with the F3a
accounting (real end time, pending count, writing session), plus a
best-effort "draining" beacon so the dashboard shows intent instead of a
phantom "running".
"""

from __future__ import annotations

import pytest

from probe.sdk import errors
from probe.sdk.journal import Journal
from probe.sdk.run import Run

from tests.conftest import make_client
from tests.test_outbox import seeded_run


@pytest.fixture(autouse=True)
def _isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(tmp_path / "config" / "probe.json"))
    monkeypatch.delenv("PROBE_TOKEN", raising=False)
    monkeypatch.delenv("PROBE_ASYNC", raising=False)
    monkeypatch.delenv("PROBE_FINISH_TIMEOUT_SEC", raising=False)


def _setup(app, tmp_path):
    run_id = seeded_run(app, tmp_path)
    client = make_client(app, tmp_spool=tmp_path / "outbox", async_writes=True)
    return run_id, client, Run(client, {"id": run_id})


def _fail_metrics(client, mp, exc=None):
    """Metrics POSTs fail; reads and PATCHes still reach the fake app -- the
    slow-drain shape, where the beacon CAN land."""
    original = client.transport.request
    exc = exc or errors.TransportError("net down")

    def selective(method, path, *a, **kw):
        if method == "POST" and path.endswith("/metrics"):
            raise exc
        return original(method, path, *a, **kw)

    mp.setattr(client.transport, "request", selective)


def _log(client, run_id, step=1):
    client.write(
        "POST",
        f"/v1/runs/{run_id}/metrics",
        {"points": [{"key": "loss", "kind": "model", "value": 1.0, "step_index": step}]},
    )


def test_default_finish_is_still_a_hard_barrier(app, tmp_path, monkeypatch):
    run_id, client, run = _setup(app, tmp_path)
    _fail_metrics(client, monkeypatch)
    _log(client, run_id)
    with pytest.raises(errors.RosError) as excinfo:
        run.finish()
    assert "not closed" in str(excinfo.value)
    assert app.runs[run_id]["status"] != "completed"
    client.close()


def test_bounded_finish_queues_terminal_status_behind_pending_ops(app, tmp_path, monkeypatch):
    run_id, client, run = _setup(app, tmp_path)
    _fail_metrics(client, monkeypatch)
    _log(client, run_id)

    report = run.finish(summary={"acc": 0.9}, flush_timeout=0.1)

    assert report["finish_queued"] is True and report["remaining"] == 2
    ops = [op for _, op in client.journal.pending()]
    assert [op["method"] for op in ops] == ["POST", "PATCH"], (
        "the close must sit BEHIND the run's data -- the ordering IS the barrier"
    )
    close = ops[-1]
    assert close["run_ref"] == run_id
    accounting = close["body"]["summary"]["probe_finish"]
    assert accounting["deferred"] is True
    assert accounting["pending_at_exit"] == 1
    assert accounting["session_id"] == run.session_id
    assert close["body"]["summary"]["acc"] == 0.9
    # Nothing flipped server-side; the beacon marked intent instead.
    assert app.runs[run_id]["status"] != "completed"
    assert "draining" in app.runs[run_id]["tags"]
    client.close()


def test_deferred_finish_lands_in_order_after_recovery(app, tmp_path):
    run_id, client, run = _setup(app, tmp_path)
    mp = pytest.MonkeyPatch()
    _fail_metrics(client, mp)
    _log(client, run_id)
    run.finish(flush_timeout=0.1)

    mp.undo()  # the network comes back
    assert client.flush() == 2
    assert client.journal.pending() == []
    row = app.runs[run_id]
    assert row["status"] == "completed"
    assert row["summary"]["probe_finish"]["deferred"] is True
    assert "draining" not in row["tags"], "the queued close restores the tags"
    assert app.metric_points_posted[run_id], "data landed BEFORE the flip"
    client.close()


def test_bounded_finish_that_drains_in_time_closes_normally(app, tmp_path):
    run_id, client, run = _setup(app, tmp_path)
    _log(client, run_id)

    run.finish(flush_timeout=5.0)

    ops = [op for _, op in client.journal.pending()]
    # The metric drained inside the deadline; the close is the ordinary
    # async-mode journaled PATCH with NO deferred accounting.
    assert [op["method"] for op in ops] == ["PATCH"]
    assert "probe_finish" not in (ops[0]["body"].get("summary") or {})
    assert client.flush() == 1
    assert app.runs[run_id]["status"] == "completed"
    client.close()


def test_bounded_finish_still_raises_on_dead_letters(app, tmp_path, monkeypatch):
    run_id, client, run = _setup(app, tmp_path)
    _fail_metrics(client, monkeypatch, exc=errors.ValidationError("bad", status=422))
    _log(client, run_id)
    with pytest.raises(errors.RosError) as excinfo:
        run.finish(flush_timeout=0.1)
    assert "dead-lettered" in str(excinfo.value)
    assert app.runs[run_id]["status"] != "completed"
    client.close()


def test_beacon_failure_is_silent_when_the_network_is_fully_down(app, tmp_path, monkeypatch):
    run_id, client, run = _setup(app, tmp_path)
    _log(client, run_id)

    def down(*a, **kw):
        raise errors.TransportError("network unreachable")

    monkeypatch.setattr(client.transport, "request", down)
    report = run.finish(flush_timeout=0.1)

    assert report["finish_queued"] is True
    close = [op for _, op in client.journal.pending()][-1]
    assert "tags" not in close["body"], "no beacon -> nothing to restore"
    assert "draining" not in (app.runs[run_id].get("tags") or [])
    client.close()


def test_env_timeout_switches_finish_to_bounded(app, tmp_path, monkeypatch):
    run_id, client, run = _setup(app, tmp_path)
    _fail_metrics(client, monkeypatch)
    _log(client, run_id)
    monkeypatch.setenv("PROBE_FINISH_TIMEOUT_SEC", "0.1")

    report = run.finish()

    assert report["finish_queued"] is True
    assert app.runs[run_id]["status"] != "completed"
    client.close()
