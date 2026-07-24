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
    "AssetClient": "assets",
    "CaptureLedger": "capture",
    "CaptureState": "capture",
    "stable_external_key": "capture",
    "stable_span_id": "capture",
    "Client": "client",
    "Settings": "config",
    "resolve": "config",
    "EventsReadClient": "events",
    "NoteClient": "events",
    "Run": "run",
    "SessionCaptureClient": "sessions",
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
    from .assets import AssetClient
    from .capture import (
        CaptureLedger,
        CaptureState,
        stable_external_key,
        stable_span_id,
    )
    from .client import Client
    from .config import Settings, resolve
    from .events import EventsReadClient, NoteClient
    from .run import Run
    from .sessions import SessionCaptureClient


__all__ = [
    "AssetClient",
    "CaptureLedger",
    "CaptureState",
    "Client",
    "EventsReadClient",
    "NoteClient",
    "Run",
    "SessionCaptureClient",
    "Settings",
    "resolve",
    "stable_external_key",
    "stable_span_id",
]
