"""Ambient below-run coordinate context: the ``run.unit(...)`` surface.

The coordinate layer (server PR prbe-ai/research-os#177) splits sub-run
identity into two flat maps:

- ``coords`` — bounded, low-cardinality grouping axes (``rank``, ``split``,
  ``phase``…). SERIES identity: the server canonicalizes + hashes them so
  metric points, spans, and artifacts stamped at one coordinate always join.
  Never a per-sample id, and never the step axis (that is ``step_index``).
- ``labels`` — unbounded per-sample drill-down ids (``sample``, ``uid``…).
  POINT identity only; they can never mint a metric series.

A key may not appear in both maps — the server 422s that, and this module
raises the same complaint client-side, where the stack trace still points at
the offending call site.

``UnitContext`` rides a :class:`contextvars.ContextVar`, so a unit entered in
one thread or asyncio task never leaks into another, and exit restores the
previous state by token (safe under overlapping generators/awaits). Nested
units MERGE: child ∪ parent, child winning per key within the same map.

The ambient maps are folded into each producer's payload at CALL time —
before the fail-open spool ever sees the body — so a spooled write replayed
minutes later still carries the coordinate that was ambient when the value
was produced, not whatever is ambient at flush time.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any, Mapping

#: (coords, labels) of the innermost active unit. The default is a shared
#: empty pair; every mutation path builds fresh dicts, so it is never written.
_STATE: ContextVar[tuple[dict[str, Any], dict[str, Any]]] = ContextVar(
    "probe_unit_context", default=({}, {})
)


def _flat(mapping: Mapping[str, Any] | None, what: str) -> dict[str, Any]:
    """A defensive copy of ``mapping``, shape-checked as a flat scalar map.

    Mirrors the cheap half of the server's ``validate_flat_map`` (string keys,
    no nested containers) so the common mistakes fail here with a useful
    traceback; budgets (key counts, value lengths) stay server-enforced.
    """
    if mapping is None:
        return {}
    if not isinstance(mapping, Mapping):
        raise TypeError(f"{what} must be a flat mapping, got {type(mapping).__name__}")
    for key, value in mapping.items():
        if not isinstance(key, str) or not key:
            raise ValueError(f"{what} keys must be non-empty strings, got {key!r}")
        if isinstance(value, (Mapping, list, tuple, set)):
            raise ValueError(
                f"{what}[{key!r}] must be a scalar (flat map), got {type(value).__name__}"
            )
    return dict(mapping)


def _check_disjoint(coords: dict[str, Any], labels: dict[str, Any]) -> None:
    overlap = sorted(coords.keys() & labels.keys())
    if overlap:
        raise ValueError(
            f"key(s) {overlap} appear in both coords and labels after merging; "
            "a key is either a grouping axis (coords) or a per-sample id (labels), "
            "never both (the server rejects this with a 422)"
        )


def current() -> tuple[dict[str, Any], dict[str, Any]]:
    """The ambient ``(coords, labels)`` pair — copies, safe for the caller to own."""
    coords, labels = _STATE.get()
    return dict(coords), dict(labels)


def merged(
    coords: Mapping[str, Any] | None = None,
    labels: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Ambient maps with explicit call-site maps merged over them.

    The call site wins per key. Raises ``ValueError`` when a key lands in both
    maps after merging — the client-side mirror of the server's 422.
    """
    ambient_coords, ambient_labels = _STATE.get()
    out_coords = {**ambient_coords, **_flat(coords, "coords")}
    out_labels = {**ambient_labels, **_flat(labels, "labels")}
    _check_disjoint(out_coords, out_labels)
    return out_coords, out_labels


def merged_coords(coords: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Ambient coords with explicit coords merged over them (call site wins).

    For producers that carry only a coordinate (spans): ambient *labels* are
    deliberately not consulted, so a span write can never trip the
    coords/labels overlap check on a map it does not send.
    """
    ambient_coords, _ = _STATE.get()
    return {**ambient_coords, **_flat(coords, "coords")}


class UnitContext:
    """``with run.unit(coords=..., labels=...):`` — see :meth:`Run.unit`.

    Re-entrant per instance is NOT supported (one token per instance); create a
    fresh unit per ``with`` block, which is what the surface reads as anyway.
    """

    def __init__(
        self,
        *,
        coords: Mapping[str, Any] | None = None,
        labels: Mapping[str, Any] | None = None,
    ) -> None:
        # Validate shape eagerly, at the declaration site.
        self._coords = _flat(coords, "coords")
        self._labels = _flat(labels, "labels")
        self._token: Token | None = None

    def __enter__(self) -> "UnitContext":
        parent_coords, parent_labels = _STATE.get()
        coords = {**parent_coords, **self._coords}
        labels = {**parent_labels, **self._labels}
        _check_disjoint(coords, labels)
        self._token = _STATE.set((coords, labels))
        return self

    def __exit__(self, *exc: object) -> None:
        if self._token is not None:
            _STATE.reset(self._token)
            self._token = None
