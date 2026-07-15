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
from .sdk import Client, Run, Settings, resolve

__all__ = ["Client", "Run", "Settings", "resolve", "errors", "__version__"]

try:  # the installed dist is the single source of version truth (pyproject)
    __version__ = _metadata.version("probe-agent")
except _metadata.PackageNotFoundError:  # running from a source tree
    __version__ = "0.0.0.dev0"
