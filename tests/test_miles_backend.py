"""connectors.miles: the zero-commit door onto the SHIPPED miles integration.

Reconciliation coverage (2026-07-27): the duplicate backend class this module
once carried is gone — ``register()`` must wire miles' registry to
``probe.integrations.miles.ProbeBackend`` — and the two deltas folded into
the shipped integration are pinned here: the step-counter entry never becomes
a series, and the run spec declares the labeled-point plan from the rollout
shape. The harness (FakeClient/FakeRun via ``_load_sdk``) comes from
test_miles_integration so both files exercise the same seam.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import pytest

from probe.connectors.miles import FLAG, ProbeBackend, planned_labeled_points, register
from probe.integrations import miles as integrations_miles
from tests.test_miles_integration import FakeClient, FakeRun, _args


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    FakeClient.instances.clear()
    for name in ("MILES_RUN_ID", "PROBE_RUN_ID", "RESEARCH_OS_RUN_ID", "PROBE_TOKEN"):
        monkeypatch.delenv(name, raising=False)


class TestRegister:
    def _stub_miles(self, monkeypatch) -> dict:
        registry: dict = {}
        base = types.ModuleType("miles.utils.tracking_utils.base")
        base.BACKEND_REGISTRY = registry
        for name in ("miles", "miles.utils", "miles.utils.tracking_utils"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, "miles.utils.tracking_utils.base", base)
        return registry

    def test_register_wires_the_shipped_backend(self, monkeypatch):
        registry = self._stub_miles(monkeypatch)
        args = SimpleNamespace()
        register(args)
        assert registry["probe"] == (ProbeBackend, FLAG)
        assert ProbeBackend is integrations_miles.ProbeBackend  # no duplicate class
        assert getattr(args, FLAG) is True
        register(args)  # idempotent
        assert registry["probe"] == (ProbeBackend, FLAG)

    def test_register_without_miles_raises_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "miles", None)
        with pytest.raises(ImportError):
            register()


class TestBudgetPlan:
    def test_planned_from_rollout_shape(self):
        args = SimpleNamespace(
            num_rollout=4000, rollout_batch_size=512, n_samples_per_prompt=1
        )
        assert planned_labeled_points(args) == 4000 * 512
        args.n_samples_per_prompt = 8
        assert planned_labeled_points(args) == 4000 * 512 * 8

    def test_clamped_to_ceiling_and_absent_when_unknown(self):
        big = SimpleNamespace(
            num_rollout=10**9, rollout_batch_size=512, n_samples_per_prompt=1
        )
        assert planned_labeled_points(big) == 100_000_000  # server ceiling mirror
        assert planned_labeled_points(SimpleNamespace()) is None
        assert (
            planned_labeled_points(SimpleNamespace(num_rollout=0, rollout_batch_size=512))
            is None
        )


class TestFoldedDeltas:
    """The two #93 deltas, now living in the SHIPPED integration."""

    def _tracker(self, monkeypatch, tmp_path, **arg_overrides):
        monkeypatch.setattr(
            integrations_miles, "_load_sdk", lambda: (FakeClient, FakeRun)
        )
        tracker = integrations_miles.ProbeTracker()
        tracker.init(_args(tmp_path, **arg_overrides), primary=True)
        return tracker

    def test_run_spec_declares_the_labeled_point_plan(self, monkeypatch, tmp_path):
        tracker = self._tracker(
            monkeypatch, tmp_path,
            num_rollout=100, rollout_batch_size=16, n_samples_per_prompt=2,
        )
        try:
            (client,) = FakeClient.instances
            (run_kwargs,) = client.run_calls
            assert run_kwargs["labeled_point_budget"] == 100 * 16 * 2
        finally:
            tracker.finish()

    def test_run_spec_omits_budget_when_plan_unknown(self, monkeypatch, tmp_path):
        tracker = self._tracker(monkeypatch, tmp_path)
        try:
            (client,) = FakeClient.instances
            (run_kwargs,) = client.run_calls
            assert "labeled_point_budget" not in run_kwargs
        finally:
            tracker.finish()

    def test_step_counter_is_stripped_from_logged_batches(self, monkeypatch, tmp_path):
        tracker = self._tracker(monkeypatch, tmp_path)
        tracker.log(
            {"train/step": 7, "train/actor-loss": 1.25}, step=7, step_key="train/step"
        )
        tracker.log(
            {"rollout/step": 3, "rollout/response_len/mean": 400.0},
            step=3,
            step_key="rollout/step",
        )
        tracker.finish()
        (client,) = FakeClient.instances
        delivered = {
            key for metrics, _ in client.created_run.logs for key in metrics
        }
        assert delivered == {"train/actor-loss", "rollout/response_len/mean"}
