"""ProbeTrackingBackend (connectors.miles) — the wandb-shaped miles door.

Miles is stubbed at exactly its integration seam: ``register()`` touches only
``miles.utils.tracking_utils.base.BACKEND_REGISTRY`` (a plain dict) and the
``args`` flag, so a stub module IS the real contract. Backend behavior runs
against the FakeApp transport — real wire bodies, no network.
"""

from __future__ import annotations

import json
import math
import sys
import types
from types import SimpleNamespace

import pytest

from probe.connectors import miles as miles_backend
from probe.connectors.miles import (
    FLAG,
    ProbeTrackingBackend,
    planned_labeled_points,
    register,
)


def _args(**overrides) -> SimpleNamespace:
    base = {
        "wandb_project": "grpo-nebius",
        "wandb_run_name": "qwen3-8b-tau",
        "num_rollout": 4000,
        "rollout_batch_size": 512,
        "n_samples_per_prompt": 1,
        "lr": 1e-6,
        "colocate": False,
        "actor_num_nodes": 8,
        "hf_checkpoint": "/ckpts/qwen3-8b",
        "_private_obj": object(),  # non-JSON-safe: must be dropped from config
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _wired_backend(app, client, monkeypatch) -> ProbeTrackingBackend:
    """A backend whose Client() resolves to the FakeApp-backed client."""
    monkeypatch.setattr(miles_backend, "Client", lambda **_: client)
    return ProbeTrackingBackend()


def _posted(app, suffix: str) -> list[dict]:
    return [
        json.loads(req.content)
        for req in app.requests
        if req.method == "POST" and req.url.path.endswith(suffix)
    ]


class TestRegister:
    def _stub_miles(self, monkeypatch) -> dict:
        registry: dict = {}
        base = types.ModuleType("miles.utils.tracking_utils.base")
        base.BACKEND_REGISTRY = registry
        for name in (
            "miles",
            "miles.utils",
            "miles.utils.tracking_utils",
        ):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        monkeypatch.setitem(sys.modules, "miles.utils.tracking_utils.base", base)
        return registry

    def test_register_inserts_backend_and_sets_flag(self, monkeypatch):
        registry = self._stub_miles(monkeypatch)
        args = SimpleNamespace()
        register(args)
        assert registry["probe"] == (ProbeTrackingBackend, FLAG)
        assert getattr(args, FLAG) is True
        register(args)  # idempotent
        assert registry["probe"] == (ProbeTrackingBackend, FLAG)

    def test_register_without_miles_raises_import_error(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "miles", None)
        with pytest.raises(ImportError):
            register()


class TestBudgetPlan:
    def test_planned_from_rollout_shape(self):
        assert planned_labeled_points(_args()) == 4000 * 512
        assert planned_labeled_points(_args(n_samples_per_prompt=8)) == 4000 * 512 * 8

    def test_clamped_to_ceiling_and_absent_when_unknown(self):
        assert (
            planned_labeled_points(_args(num_rollout=10**9)) == 100_000_000
        )  # server ceiling
        assert planned_labeled_points(SimpleNamespace()) is None
        assert planned_labeled_points(_args(num_rollout=None)) is None
        assert planned_labeled_points(_args(num_rollout=0)) is None


class TestBackendLifecycle:
    def test_init_creates_experiment_and_run_with_plan(self, app, client, monkeypatch):
        backend = _wired_backend(app, client, monkeypatch)
        backend.init(_args())
        assert backend._run is not None
        run_bodies = _posted(app, "/runs")
        assert len(run_bodies) == 1
        body = run_bodies[0]
        assert body["name"] == "qwen3-8b-tau"
        assert body["labeled_point_budget"] == 4000 * 512
        # The scalar slice of args is the config; objects are dropped.
        assert body["config"]["lr"] == 1e-6
        assert body["config"]["actor_num_nodes"] == 8
        assert "_private_obj" not in body["config"]
        assert body["tags"] == ["miles"]

    def test_secondary_init_never_mints_a_second_run(self, app, client, monkeypatch):
        backend = _wired_backend(app, client, monkeypatch)
        backend.init(_args(), primary=False)  # rollout-side session
        assert backend._run is None
        backend.init(_args())
        backend.init(_args())  # double primary init: still one run
        assert len(_posted(app, "/runs")) == 1

    def test_two_cadences_map_to_step_index_and_step_key_is_stripped(
        self, app, client, monkeypatch
    ):
        backend = _wired_backend(app, client, monkeypatch)
        backend.init(_args())
        backend.log(
            {"train/step": 7, "train/actor-loss": 1.25, "train/actor-grad_norm": 0.5},
            step=7,
            step_key="train/step",
        )
        backend.log(
            {"rollout/step": 3, "rollout/response_len/mean": 512.0},
            step=3,
            step_key="rollout/step",
        )
        batches = _posted(app, "/metrics")
        assert len(batches) == 2
        train = {p["key"]: p for p in batches[0]["points"]}
        assert set(train) == {"train/actor-loss", "train/actor-grad_norm"}
        assert all(p["step_index"] == 7 for p in train.values())
        rollout = {p["key"]: p for p in batches[1]["points"]}
        assert set(rollout) == {"rollout/response_len/mean"}
        assert rollout["rollout/response_len/mean"]["step_index"] == 3

    def test_values_are_coerced_and_non_finite_dropped_per_point(
        self, app, client, monkeypatch
    ):
        class FakeTensor:
            def __init__(self, v):
                self._v = v

            def item(self):
                return self._v

        backend = _wired_backend(app, client, monkeypatch)
        backend.init(_args())
        backend.log(
            {
                "train/step": 1,
                "train/actor-loss": FakeTensor(0.75),  # tensor -> float
                "train/actor-ppo_kl": float("nan"),  # dropped, not batch-fatal
                "rollout/error_cat/timeout": math.inf,  # dropped
                "note": "a string",  # dropped
            },
            step=1,
            step_key="train/step",
        )
        (batch,) = _posted(app, "/metrics")
        assert [(p["key"], p["value"]) for p in batch["points"]] == [
            ("train/actor-loss", 0.75)
        ]

    def test_log_before_init_and_empty_batches_are_noops(self, app, client, monkeypatch):
        backend = _wired_backend(app, client, monkeypatch)
        backend.log({"train/actor-loss": 1.0}, step=0, step_key=None)  # pre-init
        backend.init(_args())
        backend.log({"train/step": 2}, step=2, step_key="train/step")  # only the counter
        assert _posted(app, "/metrics") == []

    def test_init_failure_disables_fail_open_and_log_never_raises(
        self, app, client, monkeypatch
    ):
        monkeypatch.setattr(
            miles_backend, "Client", lambda **_: (_ for _ in ()).throw(RuntimeError("no env"))
        )
        backend = ProbeTrackingBackend()
        with pytest.warns(UserWarning, match="fail-open"):
            backend.init(_args())
        backend.log({"train/actor-loss": 1.0}, step=0, step_key=None)  # must not raise
        backend.finish()  # must not raise

    def test_finish_finishes_the_run(self, app, client, monkeypatch):
        backend = _wired_backend(app, client, monkeypatch)
        backend.init(_args())
        run_id = backend._run.id
        backend.log({"train/actor-loss": 1.0}, step=0, step_key=None)
        backend.finish()
        row = app.runs[run_id]
        assert row["status"] == "completed"
        assert row["ended_at"] is not None
        backend.finish()  # idempotent: run/client already handed off
