"""Two distinct surfaces that both involve "events":

- ``NoteClient`` (write + read) — structured *research notes*
  (intent/decision/observation), stored as ``kind="note"`` artifacts. Probe Research
  has no first-class research-note table; this is the compatibility encoding.
  Exposed as ``client.notes``.
- ``EventsReadClient`` (read) — the backend's append-only ``events`` log (fold #10),
  which is server-emitted (lifecycle + structure) and READ-ONLY. Exposed as
  ``client.events``.

These were merged under one name before; they are different things and are now split.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .client import Anchor

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


#: The anchors a note can hang off. A note is an artifact, and the three *artifact*
#: anchors all take `kind` and `meta` on their JSON create route -- so a decision
#: recorded before any run exists lands on the PROJECT, which is the whole point of
#: the anchor parameter. Workspace and Shared are *file* anchors: their bodies carry
#: no `meta`, so there is nowhere for the note to go and this rejects them by name
#: rather than letting the server 422 on a field the caller cannot see.
NOTE_ANCHORS = {Anchor.RUN, Anchor.EXPERIMENT, Anchor.PROJECT}


class NoteClient:
    """Structured research notes, stored as ``kind="note"`` artifacts.

    Normal experiment upload: a researcher, agent, notebook, or platform adapter may
    call it. Distinct from the backend lifecycle ``events`` log (see EventsReadClient).

    Notes anchor to a run, an experiment, or a PROJECT. The project anchor is what
    makes this a decision journal rather than an execution log: planning,
    investigation and architecture decisions all happen before any run exists, and
    before it they had nowhere to live."""

    def __init__(self, client: "Client"):
        self.client = client

    @staticmethod
    def _anchor(anchor: "Anchor | str") -> "Anchor":
        try:
            resolved = Anchor(anchor)
        except ValueError:
            raise ValueError(
                f"unknown anchor {anchor!r}; notes anchor to "
                f"{sorted(a.value for a in NOTE_ANCHORS)}"
            ) from None
        if resolved not in NOTE_ANCHORS:
            raise ValueError(
                f"{resolved.value} is a file anchor — a file carries no `meta`, so a "
                f"note has nowhere to live on one. Notes anchor to "
                f"{sorted(a.value for a in NOTE_ANCHORS)}."
            )
        return resolved

    def add(
        self,
        anchor_id: str,
        kind: str,
        statement: str,
        *,
        anchor: "Anchor | str" = Anchor.RUN,
        evidence_refs: list[str] | None = None,
        authority: str = "agent_summarized",
        confidence: float | None = None,
        supersedes: str | None = None,
        note_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        strict: bool | None = None,
    ) -> dict | None:
        """Append one note to ``anchor_id``, which is a run id unless ``anchor`` says
        otherwise. ``supersedes`` takes an earlier note's ``note_id``; nothing is
        overwritten, and :meth:`list` is what resolves the chain on read."""
        resolved = self._anchor(anchor)
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
        # One literal path per anchor rather than a table lookup or an f-string over a
        # variable segment: the contract-parity guard resolves routes from the AST, and
        # a computed path reads to it as a route no client method can reach.
        if resolved is Anchor.EXPERIMENT:
            return self.client.write(
                "POST", f"/v1/experiments/{anchor_id}/artifacts", body, strict=strict
            )
        if resolved is Anchor.PROJECT:
            return self.client.write(
                "POST", f"/v1/projects/{anchor_id}/artifacts", body, strict=strict
            )
        return self.client.write(
            "POST", f"/v1/runs/{anchor_id}/artifacts", body, strict=strict
        )

    def list(
        self,
        anchor_id: str,
        *,
        anchor: "Anchor | str" = Anchor.RUN,
        kind: str | None = None,
        include_superseded: bool = False,
    ) -> list[dict]:
        """The notes on one anchor, oldest first, with supersession RESOLVED.

        A note that another note supersedes is dropped by default and carries
        ``superseded_by`` (the superseding ``note_id``) when ``include_superseded``
        keeps it. That resolution lives here and only here — the CLI and the MCP
        notes view both read through this method, so the two surfaces cannot answer
        the same question differently. Without it ``--supersedes`` was written and
        read by nothing, and a reversed decision came back as a contradiction.

        `kind="note"` narrows SERVER-side on a run (that route filters) and
        client-side on an experiment or project (those routes take no filters at
        all). Either way the whole anchor is read before ``kind`` is applied, so the
        `kind` argument here is an honest narrowing of a complete list rather than a
        filter the backend may have ignored.
        """
        resolved = self._anchor(anchor)
        if kind is not None and kind not in NOTE_KINDS:
            raise ValueError(f"kind must be one of {sorted(NOTE_KINDS)}")
        params = {"kind": "note"} if resolved is Anchor.RUN else {}
        rows = self.client.list_anchored(resolved, anchor_id, **params) or []
        notes = []
        for row in rows:
            if row.get("kind") != "note":
                continue
            meta = row.get("meta")
            if not isinstance(meta, dict) or "note_id" not in meta:
                # An artifact someone else wrote under kind="note" without the note
                # encoding. Skipping is right: guessing a shape for it would put a
                # fabricated statement in a decision journal.
                continue
            notes.append({**meta, "artifact_id": row.get("id")})
        notes.sort(key=lambda n: (n.get("created_at") or "", n.get("note_id") or ""))
        superseded: dict[str, str] = {}
        for note in notes:
            target = note.get("supersedes")
            if target:
                superseded[target] = note["note_id"]
        out = []
        for note in notes:
            by = superseded.get(note["note_id"])
            if by and not include_superseded:
                continue
            if by:
                note = {**note, "superseded_by": by}
            if kind is not None and note.get("kind") != kind:
                continue
            out.append(note)
        return out


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
