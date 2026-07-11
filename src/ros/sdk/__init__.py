"""Typed Research OS SDK.

The SDK is the single implementation surface. The CLI, MCP read adapter, future
hooks, Python experiments, and passive platform integrations all build on it.
"""

from .assets import AssetClient
from .client import Client
from .config import Settings, resolve
from .events import ResearchEventClient
from .run import Run
from .sessions import SessionCaptureClient

__all__ = [
    "AssetClient",
    "Client",
    "ResearchEventClient",
    "Run",
    "SessionCaptureClient",
    "Settings",
    "resolve",
]
