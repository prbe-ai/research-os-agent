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

from importlib import metadata as _metadata

from . import errors
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
