"""Surface attribution for outbound Probe Research API requests.

Every request the transport makes to the backend carries a header naming which
product surface it came from — the CLI, the SDK, or the read-only MCP server — so
the backend can attribute product-analytics events by surface. Headers ONLY: no
event payloads, no content, no analytics client. The backend reads them
case-insensitively.

The MCP additionally tags the tool being served (``search_knowledge`` etc.). That
name is threaded through a context variable rather than every
service -> source -> client -> transport signature: the MCP memoizes one client
per token and reuses it across tool calls, so the tool name is a per-call fact,
not a per-client one. The server binds it with :func:`tool_scope` around each tool
body; the transport reads it with :func:`current_tool`.
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum

#: Request header naming the originating product surface (value: a ``Surface``).
SURFACE_HEADER = "X-Probe-Surface"

#: Request header naming the MCP tool being served (MCP surface only).
TOOL_HEADER = "X-Probe-Tool"


class Surface(str, Enum):
    """Which product surface a backend request originated from."""

    CLI = "cli"
    SDK = "sdk"
    MCP = "mcp"


# The MCP tool name currently being served, or None outside an MCP tool call.
_current_tool: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "probe_mcp_tool", default=None
)


def current_tool() -> str | None:
    """The MCP tool name bound for the current call, or None."""
    return _current_tool.get()


@contextmanager
def tool_scope(name: str) -> Iterator[None]:
    """Bind ``name`` as the MCP tool being served for the duration of the block.

    Scoped to the calling context (a contextvar), so concurrent HTTP requests in
    the hosted server never see each other's tool name."""
    token = _current_tool.set(name)
    try:
        yield
    finally:
        _current_tool.reset(token)
