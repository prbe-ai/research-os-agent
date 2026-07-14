"""probe - CLI + SDK client for Probe Research (experiment tracking).

Quick start (SDK):
    import probe
    client = probe.Client()  # resolves token from env / `probe login`
    run = client.run(experiment="dockq-sweep", hypothesis="...", name="run-1")
    run.log({"loss": 0.42, "dockq": 0.71}, step=42)
    run.finish()
"""

from __future__ import annotations

from . import errors
from .sdk import Client, Run, Settings, resolve

__all__ = ["Client", "Run", "Settings", "resolve", "errors", "__version__"]
__version__ = "0.1.0"
