"""Experiment-upload surface for evidence-linked research events.

API v3 has no first-class research-event table yet. The compatibility encoding
uses a run artifact whose metadata is a versioned, append-only event envelope.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from .client import Client

EVENT_KINDS = {
    "intent",
    "hypothesis",
    "decision",
    "observation",
    "failure",
    "result",
    "deviation",
    "next_step",
}


class ResearchEventClient:
    """Append structured session knowledge to a run.

    This is normal experiment upload. Hooks may call it at checkpoints, but it
    is equally valid for a researcher, agent, notebook, or platform adapter.
    """

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
        event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        strict: bool | None = None,
    ) -> dict | None:
        if kind not in EVENT_KINDS:
            raise ValueError(f"kind must be one of {sorted(EVENT_KINDS)}")
        if not statement.strip():
            raise ValueError("statement must not be empty")
        if confidence is not None and not 0 <= confidence <= 1:
            raise ValueError("confidence must be between 0 and 1")
        event_id = event_id or str(uuid4())
        event = {
            "schema_version": "1.0",
            "event_id": event_id,
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
            "kind": "research_event",
            "name": f"{kind}-{event_id}",
            "is_reference": False,
            "meta": event,
        }
        return self.client.write(
            "POST", f"/v1/runs/{run_id}/artifacts", body, strict=strict
        )
