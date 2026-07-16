"""Trajectory -> span-tree expansion (Phase 2 of the Harbor ownership plan).

Capture (``harbor.capture_trial``) is lossless and format-blind: the raw
``trajectory.json`` bytes are always stored. This module is the pluggable
*interpretation* layer on top — it reads a trajectory document and expands it
into child spans under the trial's rollout span, using a small normalized
vocabulary so trees are comparable across forks:

  rollout (root, created at capture)
    turn        one per trajectory step
      tool_call  one per tool invocation, result joined via source_call_id
    marker      explicit truncation marker when an eager cap was hit

Span IDs are deterministic (uuid5 of run/trial/path), so expansion is
idempotent — re-running it upserts the same rows — and *retroactive*: a trial
captured before its format had a parser can be expanded later from the stored
bytes (``probe trial expand``) without re-running anything.

Formats vary per fork, so parsers live in a registry keyed by format prefix.
``ATIF`` (Harbor upstream's Agent Trajectory Interchange Format, the
``schema_version: "ATIF-v1.x"`` documents) ships built in; private forks
register their own with :func:`register_trajectory_parser`. An unknown format
is never an error — expansion just reports ``expanded: false`` and the raw
bytes stay queryable.

Heavy content (full prompts, tool output) is excerpted in span attributes;
the complete text lives in the stored trajectory artifact.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from ..models import SpanBatch, SpanCreate

if TYPE_CHECKING:
    from ..sdk.run import Run

#: Eager-expansion window. A cap here is a lazy-loading concern, never a data
#: limit — raw bytes are always stored and ``max_spans=0`` expands everything.
DEFAULT_MAX_SPANS = 500

_EXCERPT_LIMIT = 700
_SPAN_POST_CHUNK = 200

_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "probe.harbor.trajectory")


def detect_trajectory_format(doc: Any) -> str | None:
    """Sniff a trajectory document's format. ATIF declares itself via
    ``schema_version``; other forks use ``schema``/``format``; anything
    else is ``"unknown"`` (still captured, just not expanded)."""
    if not isinstance(doc, dict):
        return "unknown" if doc is not None else None
    fmt = doc.get("schema_version") or doc.get("schema") or doc.get("format")
    return str(fmt) if fmt else "unknown"


@dataclass
class PlannedSpan:
    """One node a parser wants in the tree. ``path`` is the stable identity
    (uuid5 input) and ``parent_path`` links within the plan; ``None`` means
    the trial's rollout span. Parents must precede children in the plan so a
    prefix cut can never orphan a kept child."""

    path: str
    span_type: str
    name: str
    parent_path: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    status: str = "completed"
    attributes: dict = field(default_factory=dict)


Parser = Callable[[dict], list[PlannedSpan]]

_PARSERS: dict[str, Parser] = {}


def register_trajectory_parser(format_prefix: str, parser: Parser) -> None:
    """Register a parser for trajectory documents whose detected format starts
    with ``format_prefix`` (case-insensitive). Longest prefix wins."""
    _PARSERS[format_prefix.lower()] = parser


def parser_for(fmt: str | None) -> Parser | None:
    if not fmt:
        return None
    low = fmt.lower()
    best = None
    for prefix in _PARSERS:
        if low.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return _PARSERS[best] if best else None


def _excerpt(value: Any, limit: int = _EXCERPT_LIMIT) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        try:
            value = json.dumps(value, default=str)
        except (TypeError, ValueError):
            value = str(value)
    if len(value) > limit:
        return value[:limit] + f"… [{len(value) - limit} more chars in stored trajectory]"
    return value


def _flatten_message(message: Any) -> tuple[str | None, int]:
    """ATIF messages are a string or a list of content parts; return the
    joined text excerpt and how many image parts were present."""
    if isinstance(message, str):
        return _excerpt(message), 0
    if isinstance(message, list):
        texts, images = [], 0
        for part in message:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "image":
                images += 1
            elif part.get("text"):
                texts.append(str(part["text"]))
        return _excerpt("\n".join(texts)) if texts else None, images
    return None, 0


def _prune(mapping: dict) -> dict:
    return {k: v for k, v in mapping.items() if v is not None}


def _aware(ts: Any) -> str | None:
    """SpanCreate.started_at requires a tz-aware datetime; ATIF timestamps may
    be naive. Pass through only aware ones — naive stays in attributes."""
    if not isinstance(ts, str):
        return None
    try:
        return ts if datetime.fromisoformat(ts.replace("Z", "+00:00")).tzinfo else None
    except ValueError:
        return None


def parse_atif(doc: dict) -> list[PlannedSpan]:
    """Map an ATIF trajectory (any v1.x) into the normalized vocabulary.

    Structural mapping only — every step becomes a ``turn``, every tool call a
    ``tool_call`` child with its observation result joined via
    ``source_call_id``; unmatched observation results attach to the turn.
    Embedded subagent trajectories (v1.7) recurse under the tool_call/turn
    whose observation referenced them. Fork-specific ``extra`` fields pass
    through untouched in attributes.
    """
    return _parse_atif_inner(doc, prefix="", parent_path=None)


def _parse_atif_inner(doc: dict, *, prefix: str, parent_path: str | None) -> list[PlannedSpan]:
    plans: list[PlannedSpan] = []
    subagents = {
        sub.get("trajectory_id"): sub
        for sub in doc.get("subagent_trajectories") or []
        if isinstance(sub, dict)
    }
    agent = doc.get("agent") if isinstance(doc.get("agent"), dict) else {}

    for step in doc.get("steps") or []:
        if not isinstance(step, dict):
            continue
        step_id = step.get("step_id")
        source = step.get("source") or "agent"
        turn_path = f"{prefix}turn/{step_id}"
        message, image_parts = _flatten_message(step.get("message"))

        calls = [c for c in step.get("tool_calls") or [] if isinstance(c, dict)]
        results = []
        if isinstance(step.get("observation"), dict):
            results = [r for r in step["observation"].get("results") or [] if isinstance(r, dict)]
        by_call = {r.get("source_call_id"): r for r in results if r.get("source_call_id")}
        unmatched = [r for r in results if not r.get("source_call_id")]

        metrics = step.get("metrics") if isinstance(step.get("metrics"), dict) else None
        plans.append(
            PlannedSpan(
                path=turn_path,
                span_type="turn",
                name=f"{source} turn {step_id}",
                parent_path=parent_path,
                started_at=_aware(step.get("timestamp")),
                attributes=_prune(
                    {
                        "source": source,
                        "timestamp": step.get("timestamp"),
                        "model_name": step.get("model_name") or agent.get("model_name"),
                        "message": message,
                        "image_parts": image_parts or None,
                        "reasoning": _excerpt(step.get("reasoning_content")),
                        "observation": _excerpt(
                            "\n".join(str(r.get("content") or "") for r in unmatched)
                        )
                        if unmatched
                        else None,
                        "prompt_tokens": (metrics or {}).get("prompt_tokens"),
                        "completion_tokens": (metrics or {}).get("completion_tokens"),
                        "cached_tokens": (metrics or {}).get("cached_tokens"),
                        "cost_usd": (metrics or {}).get("cost_usd"),
                        "llm_call_count": step.get("llm_call_count"),
                        "is_copied_context": step.get("is_copied_context"),
                        "extra": step.get("extra"),
                    }
                ),
            )
        )

        for i, call in enumerate(calls):
            call_path = f"{turn_path}/call/{i}"
            result = by_call.get(call.get("tool_call_id"))
            plans.append(
                PlannedSpan(
                    path=call_path,
                    span_type="tool_call",
                    name=str(call.get("function_name") or "tool"),
                    parent_path=turn_path,
                    attributes=_prune(
                        {
                            "tool_call_id": call.get("tool_call_id"),
                            "function_name": call.get("function_name"),
                            "arguments": _excerpt(call.get("arguments")),
                            "result": _excerpt(result.get("content")) if result else None,
                            "extra": call.get("extra"),
                        }
                    ),
                )
            )
            plans.extend(
                _expand_subagent_refs(result, subagents, parent_path=call_path)
            )
        for result in unmatched:
            plans.extend(_expand_subagent_refs(result, subagents, parent_path=turn_path))
    return plans


def _expand_subagent_refs(
    result: dict | None, subagents: dict[str | None, dict], *, parent_path: str
) -> list[PlannedSpan]:
    plans: list[PlannedSpan] = []
    if not isinstance(result, dict):
        return plans
    for ref in result.get("subagent_trajectory_ref") or []:
        if not isinstance(ref, dict):
            continue
        traj_id = ref.get("trajectory_id")
        sub = subagents.get(traj_id)
        if not isinstance(sub, dict):
            continue  # external trajectory_path file — bytes captured, not expanded here
        sub_agent = sub.get("agent") if isinstance(sub.get("agent"), dict) else {}
        sub_path = f"{parent_path}/sub/{traj_id}"
        plans.append(
            PlannedSpan(
                path=sub_path,
                span_type="turn",
                name=f"subagent {sub_agent.get('name') or traj_id}",
                parent_path=parent_path,
                attributes=_prune(
                    {
                        "subagent": True,
                        "trajectory_id": traj_id,
                        "agent": sub_agent or None,
                    }
                ),
            )
        )
        plans.extend(_parse_atif_inner(sub, prefix=f"{sub_path}/", parent_path=sub_path))
    return plans


register_trajectory_parser("atif", parse_atif)


def span_id_for(run_id: str, trial: str, path: str) -> str:
    """Deterministic span id — same run/trial/path always maps to the same
    UUID, which is what makes expansion idempotent and re-runnable."""
    return str(uuid.uuid5(_NAMESPACE, f"{run_id}:{trial}:{path}"))


def expand_trajectory(
    run: "Run",
    doc: Any,
    *,
    root_span_id: str,
    trial: str,
    step_index: int | None = None,
    fmt: str | None = None,
    max_spans: int | None = None,
    strict: bool | None = None,
) -> dict:
    """Expand one trajectory document into spans under ``root_span_id``.

    ``max_spans`` is the eager window (``None`` -> :data:`DEFAULT_MAX_SPANS`,
    ``0`` -> unlimited). When the cap cuts the plan, an explicit ``marker``
    span records how many spans remain so truncation is visible, and a full
    re-expand later (idempotent ids) fills in the rest.

    Returns ``{format, expanded, spans, truncated, remaining, final_metrics}``.
    """
    fmt = fmt or detect_trajectory_format(doc)
    parser = parser_for(fmt)
    if parser is None or not isinstance(doc, dict):
        return {"format": fmt, "expanded": False, "spans": 0, "truncated": False}

    plans = parser(doc)
    limit = DEFAULT_MAX_SPANS if max_spans is None else max_spans
    remaining = 0
    if limit and len(plans) > limit:
        remaining = len(plans) - limit
        plans = plans[:limit]
        plans.append(
            PlannedSpan(
                path="truncation-marker",
                span_type="marker",
                name=f"{remaining} more spans not yet expanded",
                attributes={
                    "truncated": True,
                    "remaining": remaining,
                    "hint": "probe trial expand <run> <manifest-id> --max-spans 0",
                },
            )
        )

    spans = []
    for plan in plans:
        parent = (
            span_id_for(run.id, trial, plan.parent_path) if plan.parent_path else root_span_id
        )
        spans.append(
            SpanCreate(
                id=span_id_for(run.id, trial, plan.path),
                span_type=plan.span_type,
                parent_span_id=parent,
                name=plan.name,
                step_index=step_index,
                external_key=f"{trial}:{plan.path}",
                status=plan.status,
                started_at=plan.started_at,
                ended_at=plan.ended_at,
                attributes=plan.attributes,
                summary={},
            )
        )
    for start in range(0, len(spans), _SPAN_POST_CHUNK):
        batch = SpanBatch(spans=spans[start : start + _SPAN_POST_CHUNK])
        run._client.write(
            "POST",
            f"/v1/runs/{run.id}/spans",
            batch.model_dump(mode="json"),
            strict=strict,
        )
    report: dict[str, Any] = {
        "format": fmt,
        "expanded": True,
        "spans": len(spans),
        "truncated": remaining > 0,
    }
    if remaining:
        report["remaining"] = remaining
    if isinstance(doc.get("final_metrics"), dict):
        report["final_metrics"] = doc["final_metrics"]
    return report
