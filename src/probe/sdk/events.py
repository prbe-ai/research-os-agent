"""The backend's append-only ``events`` log (fold #10).

``EventsReadClient`` is READ-ONLY: events are emitted server-side (run created/
updated, spans, gc, ...). Exposed as ``client.events``.

This module used to also hold ``NoteClient`` -- a structured research-note vocabulary
(intent/decision/observation/...) encoded into ``kind="note"`` artifacts, since Probe
Research has no note table. It was removed: the taxonomy was never validated,
aggregated or grouped by anything server-side, so eight kinds bought one list filter,
and the durable claims it was meant to hold were already being written to the repo as
markdown and indexed from there. A note that is just text does not need a schema.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import Client
    from .transport import Page


class EventsReadClient:
    """Read the backend append-only lifecycle+structure events log (fold #10).

    Read-only: events are emitted server-side (run created/updated, spans, gc, ...)."""

    def __init__(self, client: "Client"):
        self.client = client

    def list(self, **params: Any) -> "Page":
        """GET /v1/events (keyset paginated)."""
        return self.client.transport.get_page("/v1/events", params=params or None)

    def for_run(self, run_id: str) -> list[dict]:
        """GET /v1/runs/{id}/events."""
        return self.client.transport.get(f"/v1/runs/{run_id}/events")
