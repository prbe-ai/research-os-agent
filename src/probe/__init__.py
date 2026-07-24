"""probe - CLI + SDK client for Probe Research (experiment tracking).

Quick start (SDK):
    import probe
    client = probe.Client()   # resolves token from env / `probe login`
    run = client.run()        # no token -> one-time browser approval (TTY);
                              # experiment/name/hypothesis default from context
    run.log({"loss": 0.42, "dockq": 0.71}, step=42)
    run.finish()
"""

from __future__ import annotations

import importlib
from importlib import metadata as _metadata
from typing import TYPE_CHECKING

from . import errors

# SDK names load lazily (PEP 562): `import probe` -- or, crucially, `import
# probe.sdk.<leaf>` for a stdlib-only module like probe.sdk.durable -- must not
# eagerly pull in Client and therefore httpx. A distributed Miles actor only spills
# metric batches to disk; it should not import the network stack to do that. Naming
# one of these attributes resolves it through .sdk (itself lazy) on first access.
_LAZY = {
    "CaptureLedger",
    "CaptureState",
    "Client",
    "Run",
    "Settings",
    "resolve",
    "stable_external_key",
    "stable_span_id",
}


def __getattr__(name: str) -> object:
    if name in _LAZY:
        value = getattr(importlib.import_module(".sdk", __name__), name)
        globals()[name] = value  # cache: the lazy import runs once per name
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY)


if TYPE_CHECKING:  # eager names for type checkers / IDEs; never executed at runtime
    from .sdk import (
        CaptureLedger,
        CaptureState,
        Client,
        Run,
        Settings,
        resolve,
        stable_external_key,
        stable_span_id,
    )

__all__ = [
    "CaptureLedger",
    "CaptureState",
    "Client",
    "Run",
    "Settings",
    "resolve",
    "stable_external_key",
    "stable_span_id",
    "errors",
    "__version__",
]

# The distribution name, not the import name (`probe`). Renamed from `probe-agent` on
# 2026-07-15: that name is an unrelated project on PyPI that we never owned, so
# `pip install probe-agent` fetched a stranger's package. Keep this in step with
# `[project].name` in pyproject.toml — a mismatch is silent, and degrades `probe
# --version` to the dev fallback below.
_DISTRIBUTION = "probe-research"

try:  # the installed dist is the single source of version truth (pyproject)
    __version__ = _metadata.version(_DISTRIBUTION)
except _metadata.PackageNotFoundError:  # running from a source tree
    __version__ = "0.0.0.dev0"
