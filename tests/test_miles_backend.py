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

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from probe.connectors.miles import FLAG, ProbeBackend, planned_labeled_points, register
from probe.integrations import miles as integrations_miles
from probe.sdk.capture import stable_span_id
from tests.test_miles_integration import FakeClient, FakeRun, _args


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    FakeClient.instances.clear()
    for name in ("MILES_RUN_ID", "PROBE_RUN_ID", "RESEARCH_OS_RUN_ID", "PROBE_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    # The per-sample hook keeps one producer handle per queue root and a
    # warn-once latch; both are process-global, so isolate them per test.
    monkeypatch.setattr(integrations_miles, "_HOOK_STATES", {})
    monkeypatch.setattr(integrations_miles, "_hook_warned", False)


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
        assert planned_labeled_points(args) == 4000 * 512 * 3
        args.n_samples_per_prompt = 8
        assert planned_labeled_points(args) == 4000 * 512 * 8 * 3

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
            # Two Miles points (reward + response length) and one correlated
            # Harbor verifier reward can be published for every sample.
            assert run_kwargs["labeled_point_budget"] == 100 * 16 * 2 * 3
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


class TestQueueCoordinates:
    """v2 queue records: any producer can emit coordinate-stamped points."""

    def _tracker(self, monkeypatch, tmp_path, **arg_overrides):
        monkeypatch.setattr(
            integrations_miles, "_load_sdk", lambda: (FakeClient, FakeRun)
        )
        tracker = integrations_miles.ProbeTracker()
        tracker.init(_args(tmp_path, **arg_overrides), primary=True)
        return tracker

    def test_dimensions_and_labels_ride_the_queue_to_run_log(
        self, monkeypatch, tmp_path
    ):
        tracker = self._tracker(monkeypatch, tmp_path)
        tracker.log(
            {"train/step": 7, "rollout/reward": 0.85},
            step=7,
            step_key="train/step",
            dimensions={"rank": 3},
            labels={"sample": 12},
        )
        tracker.finish()
        (client,) = FakeClient.instances
        ((metrics, kwargs),) = [
            entry for entry in client.created_run.logs if "rollout/reward" in entry[0]
        ]
        assert metrics == {"rollout/reward": 0.85}  # step counter still stripped
        assert kwargs["dimensions"] == {"rank": 3}
        assert kwargs["labels"] == {"sample": 12}

    def test_v1_records_without_coordinates_still_drain(self, monkeypatch, tmp_path):
        tracker = self._tracker(monkeypatch, tmp_path)
        # A record written by a pre-coordinate producer: v1 schema, no fields.
        path = tracker._queue.enqueue_metrics(
            {"train/actor-loss": 1.0},
            run_id=tracker._run_id,
            external_id=tracker._external_id,
            step=1,
            kind="model",
        )
        import json as _json

        record = _json.loads(path.read_text())
        record["schema_version"] = "miles.probe.metrics/v1"
        path.write_text(_json.dumps(record))
        tracker.finish()
        (client,) = FakeClient.instances
        delivered = [m for m, _ in client.created_run.logs]
        assert {"train/actor-loss": 1.0} in delivered  # drained at empty coordinate
        ((_, kwargs),) = [e for e in client.created_run.logs if "train/actor-loss" in e[0]]
        assert kwargs.get("dimensions") is None and kwargs.get("labels") is None

    def test_non_dict_coordinates_are_ignored_not_fatal(self, monkeypatch, tmp_path):
        tracker = self._tracker(monkeypatch, tmp_path)
        tracker.log(
            {"train/actor-loss": 1.0},
            step=0,
            dimensions="rank-3",  # a confused caller must not poison the queue
            labels=7,
        )
        tracker.finish()
        (client,) = FakeClient.instances
        ((_, kwargs),) = client.created_run.logs
        assert kwargs.get("dimensions") is None and kwargs.get("labels") is None


def _sample(
    index=0,
    group=None,
    reward=1.0,
    response_length=10,
    effective=None,
    metadata=None,
):
    """A miles Sample stand-in: the hook reads index / group_index / reward /
    effective_response_length / response_length via getattr, nothing else."""
    sample = SimpleNamespace(
        index=index,
        group_index=group,
        reward=reward,
        response_length=response_length,
        metadata=metadata or {},
    )
    if effective is not None:
        sample.effective_response_length = effective
    return sample


def _queue_records(args):
    """All durable records under the args-resolved queue root, enqueue order."""
    root = Path(args.probe_queue_dir)
    return [
        json.loads(path.read_text())
        for path in sorted((root / "pending").glob("*.json"))
    ]


class TestPerSampleRolloutHook:
    """The zero-fork per-sample rail behind miles'
    ``--custom-rollout-log-function-path probe.connectors.miles.per_sample_rollout_log``."""

    def test_hook_is_reachable_at_the_documented_path(self):
        from probe.connectors.miles import per_sample_rollout_log

        assert per_sample_rollout_log is integrations_miles.per_sample_rollout_log

    def test_hook_enqueues_per_sample_records_and_returns_false(self, tmp_path):
        # No tracker in this process (a bare RolloutManager): the flag alone
        # activates the rail and the records wait on the durable queue.
        args = _args(tmp_path, use_probe=True)
        samples = [
            _sample(index=0, group=0, reward=1.0, response_length=10, effective=8),
            _sample(index=1, group=0, reward=0.0, response_length=20),
        ]
        assert (
            integrations_miles.per_sample_rollout_log(7, args, samples, {}, 3.5)
            is False
        )
        records = _queue_records(args)
        assert len(records) == 2
        for record in records:
            assert record["schema_version"] == integrations_miles.QUEUE_SCHEMA_VERSION
            assert record["run_id"] is None  # no tracker ran: deferred to the drain
            assert record["kind"] == "model"
            assert record["step"] == 7  # no train-step mapping -> rollout id IS the step
            assert record["producer_id"].startswith("rollout_hook:")
            assert "dimensions" not in record  # labels only, no series axes
        assert records[0]["metrics"] == {
            "rollout/reward": 1.0,
            "rollout/response_length": 8.0,  # effective (loss-masked) wins
        }
        assert records[0]["labels"] == {"sample": 0, "group": 0}
        assert records[1]["metrics"] == {
            "rollout/reward": 0.0,
            "rollout/response_length": 20.0,
        }
        assert [r["producer_sequence"] for r in records] == [1, 2]

    def test_hook_shares_the_trackers_queue_and_drains_to_run_log(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(
            integrations_miles, "_load_sdk", lambda: (FakeClient, FakeRun)
        )
        args = _args(tmp_path)
        tracker = integrations_miles.ProbeTracker()
        tracker.init(args, primary=True)  # resolves the queue, publishes identity
        try:
            samples = [
                _sample(index=0, group=0, reward=1.0, response_length=10, effective=8),
                _sample(index=1, group=0, reward=0.0, response_length=20),
            ]
            assert (
                integrations_miles.per_sample_rollout_log(7, args, samples, {}, 3.5)
                is False
            )
        finally:
            tracker.finish()
        (client,) = FakeClient.instances
        per_sample = [e for e in client.created_run.logs if e[1].get("labels")]
        assert len(per_sample) == 2  # the live exporter drained the hook's records
        metrics, kwargs = per_sample[0]
        assert metrics == {"rollout/reward": 1.0, "rollout/response_length": 8.0}
        assert kwargs["labels"] == {"sample": 0, "group": 0}
        assert kwargs["step"] == 7 and kwargs["kind"] == "model"
        (_, kwargs_1) = per_sample[1]
        assert kwargs_1["labels"] == {"sample": 1, "group": 0}

    def test_hook_anchors_sample_to_harbor_rollout_span(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            integrations_miles, "_load_sdk", lambda: (FakeClient, FakeRun)
        )
        args = _args(tmp_path)
        tracker = integrations_miles.ProbeTracker()
        tracker.init(args, primary=True)
        run_id = tracker._run_id
        external_key = "probe:v1:harbor:rollout:trial-123:stepless:0"
        try:
            sample = _sample(
                index=4,
                group=2,
                metadata={
                    "run_id": run_id,
                    "trial_id": "trial-123",
                    "external_key": external_key,
                },
            )
            integrations_miles.per_sample_rollout_log(7, args, [sample], {}, 3.5)
        finally:
            tracker.finish()

        expected_span_id = stable_span_id(run_id, external_key)
        (client,) = FakeClient.instances
        ((_, kwargs),) = [
            entry for entry in client.created_run.logs if entry[1].get("labels")
        ]
        assert kwargs["step"] == 7
        assert kwargs["labels"] == {"sample": 4, "group": 2}
        assert kwargs["span_id"] == expected_span_id

    def test_hook_uses_returned_run_id_when_actor_args_lack_it(self, tmp_path):
        args = _args(tmp_path, use_probe=True, probe_run_id=None)
        external_key = "probe:v1:harbor:rollout:trial-456:stepless:0"
        sample = _sample(
            metadata={
                "run_id": "probe-run-from-harbor",
                "external_key": external_key,
            }
        )
        integrations_miles.per_sample_rollout_log(3, args, [sample], {}, 1.0)

        (record,) = _queue_records(args)
        assert record["run_id"] == "probe-run-from-harbor"
        assert record["span_id"] == stable_span_id(
            "probe-run-from-harbor", external_key
        )

    def test_hook_without_harbor_metadata_keeps_unanchored_behavior(self, tmp_path):
        args = _args(tmp_path, use_probe=True)
        integrations_miles.per_sample_rollout_log(
            3, args, [_sample(metadata={"trial_id": "trial-only"})], {}, 1.0
        )

        (record,) = _queue_records(args)
        assert "span_id" not in record

    def test_hook_computes_the_train_step_like_miles(self, tmp_path):
        args = _args(
            tmp_path,
            use_probe=True,
            wandb_always_use_train_step=True,
            rollout_batch_size=16,
            n_samples_per_prompt=2,
            global_batch_size=8,
        )
        assert (
            integrations_miles.per_sample_rollout_log(5, args, [_sample()], {}, 1.0)
            is False
        )
        (record,) = _queue_records(args)
        assert record["step"] == 5 * 16 * 2 // 8  # compute_rollout_step's arithmetic
        assert record["run_id"] is None  # no tracker ran: deferred to the drain

    def test_hook_is_fail_open_across_sample_shapes(self, tmp_path):
        args = _args(tmp_path, use_probe=True)
        samples = [
            _sample(index=0, reward=None, response_length=5),  # no reward scalar
            _sample(index=1, reward={"reward": 0.5, "cat": "x"}),  # dict reward
            _sample(index=2, reward={"cat": "x"}),  # dict without a numeric reward
            _sample(index=None, reward=1.0),  # id-less: no point identity -> skipped
        ]
        assert (
            integrations_miles.per_sample_rollout_log(1, args, samples, {}, 1.0)
            is False
        )
        by_sample = {r["labels"]["sample"]: r for r in _queue_records(args)}
        assert set(by_sample) == {0, 1, 2}
        assert "rollout/reward" not in by_sample[0]["metrics"]
        assert by_sample[0]["metrics"]["rollout/response_length"] == 5.0
        assert by_sample[1]["metrics"]["rollout/reward"] == 0.5
        assert "rollout/reward" not in by_sample[2]["metrics"]
        assert "group" not in by_sample[0]["labels"]  # group_index None stays off

    def test_hook_never_raises_even_when_the_queue_cannot_open(self, tmp_path):
        blocker = tmp_path / "blocked"
        blocker.write_text("a FILE where the queue directory must go")
        args = SimpleNamespace(
            use_probe=True, probe_queue_dir=str(blocker), probe_external_id="x"
        )
        assert (
            integrations_miles.per_sample_rollout_log(1, args, [_sample()], {}, 1.0)
            is False
        )

    def test_hook_unconfigured_is_a_silent_noop(self, tmp_path):
        # No tracker resolved a queue, no use_probe flag, no PROBE_TOKEN.
        args = SimpleNamespace(save=str(tmp_path / "save"))
        assert (
            integrations_miles.per_sample_rollout_log(1, args, [_sample()], {}, 1.0)
            is False
        )
        assert not (tmp_path / "save").exists()  # the queue was never created
