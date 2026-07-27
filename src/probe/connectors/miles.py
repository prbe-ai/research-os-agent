"""Miles TrackingManager backend — the wandb-shaped door into Probe.

Miles fans every metric through ``TrackingManager``
(``miles/utils/tracking_utils``): backends implement ``init/log/finish`` and
are activated from ``BACKEND_REGISTRY`` — a plain module dict checked against
``args`` flags at init time. Two ways in, one class:

  zero-commit (stock miles) — in your launcher, before ``init_tracking(args)``::

      from probe.connectors.miles import register
      register(args)   # adds the backend to the registry + sets args.use_probe

  upstream (fork polish) — register ``("probe", (ProbeTrackingBackend,
  "use_probe"))`` in ``BACKEND_REGISTRY`` and add ``--use-probe`` in
  ``arguments.py``, exactly the recipe the registry's own docstring prescribes
  for new backends.

The mapping is deliberately wandb-parity. Every ``TrackingManager.log()``
carries a ``step_key`` (``"train/step"`` / ``"rollout/step"``) plus its step
value, so Miles' two independent counters land as ``step_index`` on their own
keys; the step-key entry itself is stripped (the tensorboard backend does the
same); and values arrive AFTER Miles' DP-rank reduction — what wandb sees
today. Per-rank and per-sample capture is the capture-at-source arc (the
custom rollout function + ``capture_trial``), not this backend.

Fail-open is absolute: a tracking backend must never cost a training step. A
client that cannot be built disables the backend with one warning; non-finite
values are dropped per-point (the server rejects them; wandb accepts NaN
silently — parity of outcome: the rest of the step still logs); ``log`` and
``finish`` never raise.

Config comes from the SDK's standard env resolution (``PROBE_BASE_URL`` /
``PROBE_TOKEN``), plus optional args fields:

  ``probe_experiment``   experiment slug (falls back to ``wandb_project``,
                         then ``"miles"``); created if absent
  ``probe_run_name``     run name (falls back to ``wandb_run_name`` /
                         ``wandb_group``, then the SDK default)

The run declares its labeled-point plan (server 0061) when the args carry the
rollout shape: ``num_rollout x rollout_batch_size x n_samples_per_prompt``,
clamped to the server ceiling — so later per-sample capture against the same
run never trips the default budget mid-training.
"""

from __future__ import annotations

import contextlib
import math
import warnings
from typing import Any

from ..sdk import defaults
from ..sdk.client import Client
from ..sdk.errors import ConflictError

#: The args attribute TrackingManager checks (``getattr(args, flag, False)``).
FLAG = "use_probe"

#: Mirrors the server's MAX_LABELED_POINT_BUDGET_CEILING; the server owns the
#: real bound — this only keeps a huge rollout plan from failing run creation.
_BUDGET_CEILING = 100_000_000


def _get(args: Any, name: str, default: Any = None) -> Any:
    return getattr(args, name, default)


def _as_float(value: Any) -> float | None:
    """Numbers and 0-d tensors become floats; everything else is skipped."""
    if hasattr(value, "item"):  # torch/numpy scalar without importing either
        try:
            value = value.item()
        except Exception:  # noqa: BLE001 — non-scalar tensor etc.
            return None
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def planned_labeled_points(args: Any) -> int | None:
    """The run's per-sample volume, straight from the rollout plan."""
    num_rollout = _get(args, "num_rollout")
    batch = _get(args, "rollout_batch_size")
    per_prompt = _get(args, "n_samples_per_prompt") or 1
    try:
        planned = int(num_rollout) * int(batch) * int(per_prompt)
    except (TypeError, ValueError):
        return None
    if planned <= 0:
        return None
    return min(planned, _BUDGET_CEILING)


def _json_safe_config(args: Any) -> dict[str, Any]:
    """The scalar slice of args — the reproducibility record, minus objects."""
    try:
        items = vars(args).items()
    except TypeError:
        return {}
    config: dict[str, Any] = {}
    for key, value in items:
        if isinstance(value, (str, int, float, bool)) or value is None:
            config[key] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(v, (str, int, float, bool)) or v is None for v in value
        ):
            config[key] = list(value)
    return config


class ProbeTrackingBackend:
    """Duck-typed miles ``TrackingBackend`` (init/log/finish).

    Deliberately does NOT import miles: ``TrackingManager`` never isinstance-
    checks, so the duck interface keeps this module importable (and testable)
    without miles installed. Only :func:`register` touches miles.
    """

    def __init__(self) -> None:
        self._client: Client | None = None
        self._run: Any = None
        self._warned = False

    # -- TrackingBackend interface -------------------------------------------
    def init(self, args: Any, *, primary: bool = True, **kwargs: Any) -> None:
        if not primary or self._run is not None:
            # Secondary inits (miles uses them for rollout-side wandb sessions)
            # must not mint a second run; the primary trainer owns THE run.
            return
        try:
            client = Client(fail_open=True)
            experiment = str(
                _get(args, "probe_experiment")
                or _get(args, "wandb_project")
                or "miles"
            )
            name = (
                _get(args, "probe_run_name")
                or _get(args, "wandb_run_name")
                or _get(args, "wandb_group")
                or defaults.default_run_name()
            )
            try:
                client.create_experiment(
                    experiment, experiment, hypothesis="miles training run"
                )
            except ConflictError:
                pass  # get-or-create: the slug already exists, which is fine
            self._run = client.run(
                experiment=experiment,
                name=str(name),
                config=_json_safe_config(args),
                tags=["miles"],
                labeled_point_budget=planned_labeled_points(args),
            )
            self._client = client
        except Exception as exc:  # noqa: BLE001 — a tracker must not kill training
            self._run = None
            warnings.warn(
                f"probe tracking backend disabled (init failed, fail-open): {exc}",
                stacklevel=2,
            )

    def log(
        self,
        metrics: dict[str, Any],
        step: int | None = None,
        *,
        step_key: str | None = None,
        **kwargs: Any,
    ) -> None:
        if self._run is None:
            return
        clean: dict[str, float] = {}
        for key, value in metrics.items():
            if key == step_key:
                continue  # the counter itself is the x-axis, not a series
            number = _as_float(value)
            if number is None or not math.isfinite(number):
                continue  # drop the point, keep the step (wandb-outcome parity)
            clean[key] = number
        if not clean:
            return
        try:
            self._run.log(clean, step=int(step) if step is not None else None)
        except Exception as exc:  # noqa: BLE001
            if not self._warned:
                self._warned = True
                warnings.warn(
                    f"probe tracking log failed (fail-open, further failures "
                    f"silent): {exc}",
                    stacklevel=2,
                )

    def finish(self) -> None:
        run, self._run = self._run, None
        client, self._client = self._client, None
        try:
            # TrackingManager.finish guards too, but fail-open is OUR contract.
            with contextlib.suppress(Exception):
                if run is not None:
                    run.finish()
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close()


def register(args: Any | None = None) -> None:
    """Activate the backend on STOCK miles — call before ``init_tracking(args)``.

    ``BACKEND_REGISTRY`` is a plain module dict and ``TrackingManager.init``
    reads flags with ``getattr(args, flag, False)``, so inserting the entry and
    setting the flag is the entire integration; no miles commit required.
    Idempotent, and raises ImportError only when miles itself is absent.
    """
    from miles.utils.tracking_utils.base import BACKEND_REGISTRY

    BACKEND_REGISTRY.setdefault("probe", (ProbeTrackingBackend, FLAG))
    if args is not None:
        setattr(args, FLAG, True)
