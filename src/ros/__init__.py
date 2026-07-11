"""ros - CLI + SDK client for research-os (Probe experiment tracking).

Import name and CLI name (`ros` / `exp`) are placeholders per the primitives sketch.

Quick start (SDK):
    import ros
    client = ros.Client()  # resolves token from env / `exp login`
    run = client.run(experiment="dockq-sweep", hypothesis="...", name="run-1")
    run.log({"loss": 0.42, "dockq": 0.71}, step=42)
    run.finish()
"""

from __future__ import annotations

from . import errors
from .sdk import Client, Run, Settings, resolve

__all__ = ["Client", "Run", "Settings", "resolve", "errors", "__version__"]
__version__ = "0.1.0"
