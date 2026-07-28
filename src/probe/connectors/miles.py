"""Zero-commit activation of the Probe miles backend on STOCK miles.

THE backend is :class:`probe.integrations.miles.ProbeBackend` — the shipped
durable integration (atomic on-disk metric queue, single exporter lease,
deferred run creation/repair, redacted config capture, Ray-actor identity
publication). Miles' fork integration registers it natively via
``miles/utils/tracking_utils/probe_utils`` + ``--use-probe`` (the registry
docstring's own recipe). This module is the door for UNFORKED miles:
``BACKEND_REGISTRY`` is a plain module dict and ``TrackingManager.init``
reads flags with ``getattr(args, flag, False)``, so inserting the entry and
setting the flag from your launcher IS the entire integration::

    from probe.connectors.miles import register
    register(args)          # before init_tracking(args)

Reconciliation note (2026-07-27): an earlier duplicate backend class that
lived here was retired in favor of the shipped integration. Its two genuine
deltas were folded into ``probe.integrations.miles`` so every activation door
gets them: the step-counter entry (``train/step`` / ``rollout/step``) is
stripped from logged batches (the counter is the x-axis, not a series), and
the run declares its labeled-point plan (server 0061) from the rollout shape
via :func:`planned_labeled_points`.
"""

from __future__ import annotations

from typing import Any

from ..integrations.miles import ProbeBackend, planned_labeled_points

__all__ = ["FLAG", "ProbeBackend", "planned_labeled_points", "register"]

#: The args attribute TrackingManager checks (``getattr(args, flag, False)``).
FLAG = "use_probe"


def register(args: Any | None = None) -> None:
    """Activate the backend on STOCK miles — call before ``init_tracking(args)``.

    Inserts the shipped :class:`ProbeBackend` into miles' ``BACKEND_REGISTRY``
    and (when ``args`` is given) sets ``args.use_probe`` so the manager
    activates it. Idempotent; raises ImportError only when miles is absent.
    """
    from miles.utils.tracking_utils.base import BACKEND_REGISTRY

    BACKEND_REGISTRY.setdefault("probe", (ProbeBackend, FLAG))
    if args is not None:
        setattr(args, FLAG, True)
