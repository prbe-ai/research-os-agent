"""Two distinct surfaces that both involve "events":

- ``NoteClient`` (write) — structured *research notes* (intent/decision/observation),
  stored as a ``kind="note"`` run artifact. Probe Research has no first-class research-note
  table; this is the compatibility encoding. Exposed as ``client.notes``.
- ``EventsReadClient`` (read) — the backend's append-only ``events`` log (fold #10),
  which is server-emitted (lifecycle + structure) and READ-ONLY. Exposed as
  ``client.events``.

These were merged under one name before; they are different things and are now split.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from .client import Client
    from .transport import Page

NOTE_KINDS = {
    "intent",
    "hypothesis",
    "decision",
    "observation",
    "failure",
    "result",
    "deviation",
    "next_step",
}


class NoteClient:
    """Append a structured research note to a run (stored as a ``kind="note"`` artifact).

    Normal experiment upload: a researcher, agent, notebook, or platform adapter may
    call it. Distinct from the backend lifecycle ``events`` log (see EventsReadClient)."""

    def __init__(self, client: "Client"):
        self.client = client

    def add(
        self,
        run_id: str,
        kind: str,
        statement: str,
        *,
        evidence_refs: list[str] | None = None,
        authority: str = "agent_summarized",
        confidence: float | None = None,
        supersedes: str | None = None,
        note_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        strict: bool | None = None,
    ) -> dict | None:
        if kind not in NOTE_KINDS:
            raise ValueError(f"kind must be one of {sorted(NOTE_KINDS)}")
        if not statement.strip():
            raise ValueError("statement must not be empty")
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        note_id = note_id or str(uuid4())
        note = {
            "schema_version": "1.0",
            "note_id": note_id,
            "kind": kind,
            "statement": statement.strip(),
            "evidence_refs": evidence_refs or [],
            "authority": authority,
            "confidence": confidence,
            "supersedes": supersedes,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": metadata or {},
        }
        body = {
            "kind": "note",
            "name": f"{kind}-{note_id}",
            "is_reference": False,
            "meta": note,
        }
        return self.client.write(
            "POST", f"/v1/runs/{run_id}/artifacts", body, strict=strict
        )


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
