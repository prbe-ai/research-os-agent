"""Typed Probe Research SDK.

The SDK is the single implementation surface. The CLI, MCP read adapter, future
hooks, Python experiments, and passive platform integrations all build on it.

Exports load lazily (PEP 562 ``__getattr__``): naming a class here imports exactly
the one submodule that defines it, and nothing else. So ``import probe.sdk.durable``
(or any other stdlib-only leaf) never drags in ``client`` -- and therefore never
drags in ``httpx`` -- which is what lets a distributed Miles actor spill metric
batches to disk without importing the network stack. Type checkers and IDEs still
see the real names through the ``TYPE_CHECKING`` block below.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

# Public name -> the submodule that defines it. Keep in step with ``__all__``.
_LAZY: dict[str, str] = {
    "CaptureLedger": "capture",
    "CaptureState": "capture",
    "stable_external_key": "capture",
    "stable_span_id": "capture",
    "Aligned": "analysis",
    "Comparison": "analysis",
    "Client": "client",
    "Reader": "reader",
    "Reference": "reader",
    "Settings": "config",
    "resolve": "config",
    "EventsReadClient": "events",
    "active_run": "fluent",
    "finish": "fluent",
    "init": "fluent",
    "log": "fluent",
    "log_artifact": "fluent",
    "log_hw": "fluent",
    "span": "fluent",
    "Run": "run",
    "SpanHandle": "run",
    "UnitContext": "unit_context",
}


def __getattr__(name: str) -> object:
    submodule = _LAZY.get(name)
    if submodule is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(f".{submodule}", __name__), name)
    globals()[name] = value  # cache: the lazy import runs once per name
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))


if TYPE_CHECKING:  # eager names for type checkers / IDEs; never executed at runtime
    from .analysis import Aligned, Comparison
    from .capture import (
        CaptureLedger,
        CaptureState,
        stable_external_key,
        stable_span_id,
    )
    from .client import Client
    from .config import Settings, resolve
    from .events import EventsReadClient
    from .reader import Reader, Reference
    from .fluent import active_run, finish, init, log, log_artifact, log_hw, span
    from .run import Run, SpanHandle
    from .unit_context import UnitContext


__all__ = [
    "Aligned",
    "CaptureLedger",
    "CaptureState",
    "Client",
    "Comparison",
    "EventsReadClient",
    "Reader",
    "Reference",
    "Run",
    "Settings",
    "SpanHandle",
    "UnitContext",
    "active_run",
    "finish",
    "init",
    "log",
    "log_artifact",
    "log_hw",
    "resolve",
    "span",
    "stable_external_key",
    "stable_span_id",
]
