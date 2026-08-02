"""Framework-independent implementation of the read-only MCP operations."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import islice
from typing import Any

from ..sdk import errors
from .contract import (
    BackendCorpus,
    BackendSearchState,
    Capability,
    Channel,
    ChannelError,
    CollapseMode,
    EntityType,
    EnvelopeState,
    MatchMode,
    MissingMarker,
    ToolCorpus,
    View,
)
from .source import ResearchOSSource

# `search_in` vocabulary -> backend /v1/search `corpus` values. NOT a passthrough:
# three entries are identity and one is not, which is the whole reason the tool
# parameter is no longer called `corpora` -- the identity majority is exactly what
# made "this is just `corpus` pluralised" such an easy wrong conclusion.
# Experiments are one value among four, not an always-on floor: they are searched
# when the caller names nothing at all (no filter) or names them explicitly.
#
#   search_in      backend corpus        shape
#   transcripts -> transcripts           identity
#   experiments -> experiments           identity
#   files -> files                       identity
#   documents -> github + files          fans out
#
# `documents` deliberately overlaps `files`: it is the broader ask (workspace
# files PLUS indexed github docs), and `files` is the narrower one.
#
# The tool docstring in server.py carries this same table for callers, and
# tests/test_mcp_schema_docs.py fails if the two disagree.
_SEARCH_IN_TO_BACKEND: dict[str, set[BackendCorpus]] = {
    ToolCorpus.FILES: {BackendCorpus.FILES},
    ToolCorpus.DOCUMENTS: {BackendCorpus.GITHUB, BackendCorpus.FILES},
    ToolCorpus.TRANSCRIPTS: {BackendCorpus.TRANSCRIPTS},
    ToolCorpus.EXPERIMENTS: {BackendCorpus.EXPERIMENTS},
}

# The knowledge-side values (every one except experiments). Drives the kb_values
# missing-marker; carries no "always searched" implication for experiments.
_KB_TOOL_VALUES = {
    ToolCorpus.FILES,
    ToolCorpus.DOCUMENTS,
    ToolCorpus.TRANSCRIPTS,
}

# Backend caps top_k / exact_limit.
_BACKEND_CHANNEL_CAP = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(record: dict) -> str:
    fields = [
        record.get("id"),
        record.get("slug"),
        record.get("name"),
        record.get("description"),
        record.get("hypothesis"),
        " ".join(record.get("tags") or []),
    ]
    return " ".join(str(value) for value in fields if value).lower()


def _map_search_in(search_in: list[str] | None) -> tuple[list[str] | None, list[str]]:
    """Translate the tool's `search_in` vocabulary into backend corpus values.

    Returns ``(backend_corpus_or_None, unsupported_values)``. Naming nothing means
    no filter (the backend searches every corpus).

    Naming values NARROWS to exactly those. Experiments used to be unioned in
    unconditionally, which made a narrowed search unusable in practice: the
    per-channel budget is roughly half of `top_k`, experiment projections
    outrank the knowledge corpora on most queries, and so `["transcripts"]`
    came back holding nothing but experiments. Ask for experiments alongside by
    naming them (`["experiments", "transcripts"]`)."""
    if not search_in:
        return None, []
    backend: set[str] = set()
    unsupported: list[str] = []
    for value in search_in:
        mapped = _SEARCH_IN_TO_BACKEND.get(value)
        if mapped is None:
            unsupported.append(value)
        else:
            backend.update(mapped)
    if not backend:
        # Every named value was unrecognized. Falling through with an empty
        # filter would search EVERYTHING and read as success; keep the old
        # experiments-only floor and let `unsupported_values` carry the miss.
        backend = {BackendCorpus.EXPERIMENTS}
    return sorted(backend), sorted(set(unsupported))


def _why_matched(
    mode: str, channel: str, *, score: float | None = None, terms: list[str] | None = None
) -> dict:
    """A stable, channel-uniform provenance shape: {mode, channel, score, terms}."""
    return {"mode": mode, "channel": channel, "score": score, "terms": terms or []}


def _section(response: Any, key: str) -> dict[str, Any]:
    """Normalize one per-channel section of a /v1/search response, degrading a
    malformed body to an empty section with an explicit error marker (so a
    broken proxy/server yields state=partial, never an exception)."""
    section = response.get(key) if isinstance(response, dict) else None
    if not isinstance(section, dict):
        return {"results": [], "cursor": None, "error": ChannelError.MALFORMED_RESPONSE}
    raw = section.get("results")
    rows = [row for row in raw if isinstance(row, dict)] if isinstance(raw, list) else []
    error = section.get("error")
    error = error if isinstance(error, str) else (str(error) if error else None)
    if not isinstance(raw, list) or len(rows) != len(raw):
        error = error or ChannelError.MALFORMED_RESPONSE
    cursor = section.get("cursor")
    return {
        "results": rows,
        "cursor": cursor if isinstance(cursor, str) else None,
        "error": error,
        # Carried through so the tool can surface them. Type-guarded like
        # everything else here: a malformed body must degrade, never raise.
        "total_candidates": (
            section["total_candidates"]
            if isinstance(section.get("total_candidates"), int)
            else None
        ),
        "active_runs_count": (
            section["active_runs_count"]
            if isinstance(section.get("active_runs_count"), int)
            else None
        ),
    }


def _exact_result(row: dict) -> dict:
    """An exact-channel hit (project | experiment | artifact) in the tool's result shape."""
    entity_type = row.get("entity_type")
    entity_id = row.get("id")
    card = {
        key: row.get(key)
        for key in ("name", "slug", "workspace_id", "project_id", "experiment_id", "run_id")
        if row.get(key) is not None
    }
    resource = None
    if entity_type == EntityType.EXPERIMENT:
        resource = f"research://experiments/{entity_id}/card"
    elif entity_type == EntityType.PROJECT:
        resource = f"research://projects/{entity_id}/card"
    # artifacts have no addressable research:// resource (no single-GET route)
    return {
        "entity_type": entity_type,
        "id": entity_id,
        "card": card,
        "why_matched": _why_matched(MatchMode.EXACT, Channel.EXACT, score=row.get("score")),
        "resource": resource,
    }


def _semantic_result(row: dict) -> dict:
    """A semantic-channel document hit (engine) in the tool's result shape."""
    ref = row.get("ref") or {}
    kind = ref.get("kind") if isinstance(ref, dict) else None
    entity_id = (ref.get("id") if isinstance(ref, dict) else None) or row.get("doc_id")
    resource = None
    if kind == EntityType.EXPERIMENT:
        resource = f"research://experiments/{entity_id}/card"
    elif kind == EntityType.RUN:
        resource = f"research://runs/{entity_id}/handoff"
    card = {
        key: row.get(key)
        for key in ("title", "snippet", "source_system", "source_url", "doc_id")
        if row.get(key) is not None
    }
    return {
        "entity_type": kind or EntityType.DOCUMENT,
        "id": entity_id,
        "card": card,
        "why_matched": _why_matched(MatchMode.SEMANTIC, Channel.SEMANTIC, score=row.get("score")),
        "resource": resource,
    }


def _interleave(first: list[dict], second: list[dict]) -> list[dict]:
    """Fair round-robin merge — the backend returns per-channel sections with no
    merged ranking, so neither channel gets to starve the other."""
    merged: list[dict] = []
    for index in range(max(len(first), len(second))):
        if index < len(first):
            merged.append(first[index])
        if index < len(second):
            merged.append(second[index])
    return merged


def _score(row: dict) -> float:
    value = (row.get("why_matched") or {}).get("score")
    return value if isinstance(value, (int, float)) else float("-inf")


def _in_project(row: dict, project_id: str) -> bool:
    """Whether an exact hit belongs to the requested project. Rows without a
    project linkage are conservatively dropped (never out-of-project hits)."""
    if row.get("project_id") == project_id:
        return True
    return row.get("entity_type") == EntityType.PROJECT and row.get("id") == project_id


def _collapse_experiments(results: list[dict]) -> list[dict]:
    """``collapse="experiment"``: one hit per experiment id, keeping the
    best-scoring representative's channel provenance.

    RUN hits pass through (deduped by id) instead of being dropped: experiment
    is OPTIONAL grouping now (research-os 0054), so a project-direct run has no
    experiment-level hit to represent it — and the result rows carry no
    experiment linkage to tell a direct run from an attached one.

    Everything else — document/file/project/asset hits — passes through too.
    Collapse DEDUPES; it does not filter. Dropping the non-experiment rows made
    every knowledge corpus unreachable through the default call: `search_in` maps
    transcripts/documents/files straight into the backend query, the backend
    returns them, and then this discarded all of them before the caller ever saw
    one. A search that silently answers a different question than it was asked is
    worse than one that errors."""

    def _key(row: dict) -> Any:
        if row.get("entity_type") not in (EntityType.EXPERIMENT, EntityType.RUN):
            return None  # never deduped, never dropped
        return (row.get("entity_type"), row.get("id"))

    best: dict[Any, dict] = {}
    for row in results:
        key = _key(row)
        if key is None:
            continue
        kept = best.get(key)
        if kept is None or _score(row) > _score(kept):
            best[key] = row
    # Emit in the merged ranking order the caller was given: a collapsed
    # experiment takes the position of its FIRST occurrence, carrying the
    # best-scoring representative. Appending the pass-through rows at the end
    # instead would bury every document hit below every experiment hit.
    emitted: set[Any] = set()
    collapsed: list[dict] = []
    for row in results:
        key = _key(row)
        if key is None:
            collapsed.append(row)
        elif key not in emitted:
            emitted.add(key)
            collapsed.append(best[key])
    return collapsed


def _pack_cursor(payload: dict) -> str:
    """Pack a cursor payload into an opaque token that is deliberately NOT JSON.

    A raw ``json.dumps({...})`` cursor cannot survive the MCP tool layer. FastMCP's
    `pre_parse_json` runs json.loads on every string argument and, when the result
    is not a scalar, REPLACES the argument with the parsed object — so a JSON-object
    cursor reaches the tool as a dict and is rejected against ``cursor: str | None``.
    Pagination then works perfectly in-process and 422s over the wire, which is
    exactly how it shipped: no test calls the tool layer, they all call the service
    directly.

    Base64 keeps the token a string through that pre-parse, and makes it genuinely
    opaque, so nobody hand-builds one and depends on the shape."""
    raw = json.dumps(payload, sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unpack_cursor(cursor: str, *, hint: str) -> dict:
    """Opaque token -> its payload, or a ValidationError naming how to get a real one.

    Raw-JSON cursors are still accepted: tokens minted before cursors were packed
    are already in agent transcripts, and refusing them would turn a stale cursor
    into an error instead of a page."""
    for decode in (
        lambda: json.loads(
            base64.urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode()).decode()
        ),
        lambda: json.loads(cursor),  # legacy: pre-pack raw-JSON cursor
    ):
        try:
            parsed = decode()
        except (json.JSONDecodeError, ValueError, TypeError, binascii.Error, UnicodeDecodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise errors.ValidationError(
        f"malformed cursor: pass the next_cursor value from a previous {hint} call",
        status=422,
    )


def _split_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    """The tool's opaque cursor carries the per-channel backend cursors."""
    if not cursor:
        return None, None
    parsed = _unpack_cursor(cursor, hint="research_search")
    return parsed.get(Channel.EXACT), parsed.get(Channel.SEMANTIC)


def _join_cursor(exact: str | None, semantic: str | None) -> str | None:
    cursors = {
        key: value
        for key, value in ((Channel.EXACT.value, exact), (Channel.SEMANTIC.value, semantic))
        if value
    }
    return _pack_cursor(cursors) if cursors else None


# -- research_get: token budget, cursor, and the view table --------------------

# ~4 chars per token of JSON. Approximate on purpose: this only has to BOUND a
# payload, and a real tokenizer would drag a model dependency into a read path
# without making the bound any safer.
_CHARS_PER_TOKEN = 4

# Rows fetched past the caller's offset — far more than any sane token_budget
# emits, so a page costs one backend call. The slice is client-side because the
# spans/artifacts/events routes take no offset (only `limit`).
_PAGE_FETCH = 200

# `limit` ceilings from schema/openapi.json.
_SPAN_BACKEND_MAX = 10_000  # GET /v1/runs/{id}/spans
_METRIC_BACKEND_MAX = 100_000  # GET /v1/runs/{id}/metrics

# Row bounds for the coordinate reads. Grouped cells are aggregates (one per step
# bucket x group), so a few hundred already draws a chart; export points are raw
# and unbounded, so the tool serves ONE keyset page and hands the cursor back.
# Both are clamps, not defaults an agent can override past.
_GROUPED_ROWS_MAX = 2_000
_GROUPED_ROWS_DEFAULT = 500
_EXPORT_PAGE_MAX = 1_000
_EXPORT_PAGE_DEFAULT = 200

# `limit` at 200; the token budget trims below this anyway.


def _tokens(value: Any) -> int:
    return max(1, len(json.dumps(value, default=str)) // _CHARS_PER_TOKEN)


def _fit(rows: list, budget: int) -> list:
    """Emit rows while they fit `budget` tokens.

    ALWAYS emits at least one row when any exist: a budget too small for even the
    first row must still make progress, or a cursor walk spins forever returning
    nothing. The over-budget row is honestly reported (token_budget_exceeded)
    rather than silently withheld."""
    out: list = []
    spent = 0
    for row in rows:
        cost = _tokens(row)
        if out and spent + cost > budget:
            break
        out.append(row)
        spent += cost
    return out


def _fit_sections(
    sections: list[tuple[str, list]], budget: int
) -> tuple[dict[str, list], bool]:
    """Spend one budget across several lists in priority order.

    research_context has no cursor, so unlike _fit there is no always-emit-one
    floor — nothing is paginating and a forced row would just blow the budget.
    Dropping rows silently is the failure to avoid, hence the `truncated` flag."""
    kept: dict[str, list] = {}
    truncated = False
    spent = 0
    for key, rows in sections:
        taken: list = []
        for row in rows:
            cost = _tokens(row)
            if spent + cost > budget:
                break
            taken.append(row)
            spent += cost
        truncated = truncated or len(taken) < len(rows)
        kept[key] = taken
    return kept, truncated


def _split_get_cursor(cursor: str | None, view: str) -> int:
    """research_get's opaque cursor, carrying ``{"view": v, "offset": n}``.

    The view is carried so a cursor can never be silently re-based onto another
    view — offset 40 of a trajectory means nothing in an events list, and quietly
    reinterpreting it would skip 40 events with no signal at all."""
    if not cursor:
        return 0
    parsed = _unpack_cursor(cursor, hint="research_get")
    try:
        offset, cursor_view = parsed["offset"], parsed["view"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        raise errors.ValidationError(
            "malformed cursor: pass the next_cursor value from a previous "
            "research_get call",
            status=422,
        ) from None
    if cursor_view != view:
        raise errors.ValidationError(
            f"cursor was issued for view={cursor_view!r} but this call asked for "
            f"view={view!r}: pass a cursor back with the view that produced it",
            status=422,
        )
    return offset


def _join_get_cursor(view: str, offset: int) -> str:
    return _pack_cursor({"offset": offset, "view": str(view)})


@dataclass(frozen=True)
class _Req:
    """What a view builder needs beyond the entity itself."""

    filters: dict[str, Any]
    offset: int


@dataclass
class _ViewData:
    """One view's payload, split by what SCALES.

    `rows` is the unbounded part — it is what token_budget bounds and what cursor
    walks. `payload` is the fixed-size part. A view with rows=None is ATOMIC: it is
    never truncated (see ResearchReadService.research_get).

    `more_beyond` says the backend has rows past the fetched window. It exists
    because forgetting it is a LIE, not an inefficiency: a bounded fetch of 200
    spans from a 500-span run would otherwise be emitted whole and reported
    complete, and the agent would believe it had read the entire trajectory.
    """

    payload: dict[str, Any] = field(default_factory=dict)
    rows: list[dict] | None = None
    rows_key: str = "rows"
    missing: list[str] = field(default_factory=list)
    more_beyond: bool = False


# (entity kind, view) -> builder method. Explicit and greppable: this table IS the
# answer to "what can I read about a run?", and _checked_view derives its error
# message from it, so a view can never be advertised without a builder behind it.
_VIEWS: dict[tuple[str, str], str] = {
    (EntityType.RUN, View.CARD): "_view_card",
    (EntityType.RUN, View.TRAJECTORY): "_view_trajectory",
    (EntityType.RUN, View.METRICS): "_view_metrics",
    (EntityType.RUN, View.ARTIFACTS): "_view_run_artifacts",
    (EntityType.RUN, View.REPRODUCE): "_view_reproduce",
    (EntityType.RUN, View.HANDOFF): "_view_handoff",
    (EntityType.RUN, View.LINEAGE): "_view_run_lineage",
    (EntityType.RUN, View.EVENTS): "_view_events",
    (EntityType.EXPERIMENT, View.CARD): "_view_card",
    (EntityType.EXPERIMENT, View.ARTIFACTS): "_view_experiment_artifacts",
    (EntityType.EXPERIMENT, View.LINEAGE): "_view_experiment_lineage",
    (EntityType.EXPERIMENT, View.GROUPS): "_view_groups",
    (EntityType.EXPERIMENT, View.VERSIONS): "_view_versions",
    (EntityType.PROJECT, View.CARD): "_view_card",
    (EntityType.GROUP, View.CARD): "_view_card",
    # Assets: the reuse-before-create seam. `versions` is where research_resolve
    # went -- an asset lookup is a read of one entity, and it never needed a
    # tool of its own.
    # NO asset lineage view. There is no backend route for it, so it could only
    # ever answer `edges: []` -- and an agent reads that as "this asset has no
    # lineage", a confident wrong answer. Exactly why research_trace_file was
    # removed rather than left returning empty. Add the view when the route
    # exists, not before.
}

# Filters each (kind, view) accepts, mapped onto the backend's REAL server-side
# filters. Anything else is rejected loudly — a silently-ignored filter returns a
# full result set that the agent believes was narrowed.
#
# Keyed by (kind, view), not view: GET /v1/experiments/{id}/artifacts takes no
# filters at all, so `kind` is honest on a RUN's artifacts and a lie on an
# experiment's.
_VIEW_FILTERS: dict[tuple[str, str], set[str]] = {
    (EntityType.RUN, View.TRAJECTORY): {"span_type", "parent_span_id", "step_from", "step_to"},
    (EntityType.RUN, View.METRICS): {"key", "kind"},
    (EntityType.RUN, View.ARTIFACTS): {"kind", "step_from", "step_to", "name", "scope"},
    # `at` is deliberately absent: the SDK accepted it and never read it, and
    # no backend as-of resolution exists. Advertising a parameter that silently
    # does nothing is worse than not having one.
}


def _compact(envelope: dict) -> dict:
    """Strip envelope bookkeeping an agent does not reason over.

    What is KEPT and why is the interesting half -- a drop-list without one
    rots into dropping something load-bearing:

    - `completeness` ALWAYS survives. It is the only field that says what the
      response could not cover, and stripping it would turn every partial answer
      into a confident one.
    - `capabilities` survives ONLY where it is False. A True flag is noise
      repeated on every call; a False flag says this backend cannot do something
      you may be about to rely on.
    - `next_cursor` survives when set: without it there is no way to know the
      result was a page rather than the whole answer.
    - `scope`, `as_of`, `schema_version` go. They are constant per token and per
      release; re-sending them on every call costs context and tells the agent
      nothing it can act on.
    - `evidence` goes when empty, stays when populated.
    """
    compacted = {
        "data": envelope["data"],
        "completeness": envelope["completeness"],
    }
    # ALWAYS emit the key, even when empty. Omitting it overloads absence to
    # mean both "everything works" and "not reported", so a caller doing
    # resp["capabilities"]["semantic_search"] would KeyError on a healthy
    # backend and succeed on a degraded one -- the inverse of a useful failure
    # mode. Empty dict means "nothing unavailable".
    compacted["capabilities"] = {
        k: v for k, v in (envelope.get("capabilities") or {}).items() if not v
    }
    if envelope.get("next_cursor") is not None:
        compacted["next_cursor"] = envelope["next_cursor"]
    if envelope.get("evidence"):
        compacted["evidence"] = envelope["evidence"]
    return compacted


def _echoes_project_scope(response: Any, project_id: str) -> bool:
    """Did the backend confirm it applied `project_id`?

    A server that supports the scope echoes it (research-os #103 returns it on
    the request echo); one that predates it accepts the unknown body field,
    ignores it, and answers tenant-wide. Absence of the echo is the only signal
    available, so absence is treated as unsupported -- the failure that matters
    is the silent one, and a false refusal is loud and correctable.
    """
    if not isinstance(response, dict):
        return False
    # Absence is treated as UNSUPPORTED, not as assent. The failure that matters
    # is the silent one -- tenant-wide results wearing state="complete" -- and a
    # false refusal is loud, immediate and correctable.
    return str(response.get("project_id") or "") == str(project_id)


def _satisfies(version: dict, requirement: str) -> bool:
    """Does this asset version satisfy `requirement`?

    Asset versions are MONOTONIC INTEGERS with optional labels, not semver --
    so this supports exactly what the data supports: an exact label/version
    match, or `>=N` / `>N` / `<=N` / `<N` against the integer. Pretending to
    understand semver ranges over integer versions would answer confidently and
    wrongly, which is the failure mode this whole view exists to prevent.
    """
    raw = str(version.get("version", ""))
    label = version.get("label")
    requirement = requirement.strip()
    # Order matters: ">=" must be tested before ">", and "==" before "=".
    for op in (">=", "<=", "==", ">", "<", "="):
        if requirement.startswith(op):
            operand = requirement[len(op):].strip()
            try:
                have, want = int(raw), int(operand)
            except (TypeError, ValueError):
                # A malformed operand must NOT quietly answer "no version
                # satisfies this". That is a THIRD kind of nothing, and it is
                # indistinguishable from a real version ceiling: the caller sees
                # state="no_match" with completeness="complete", believes the
                # asset is too old, and registers a duplicate -- the exact
                # outcome this view exists to prevent. ">=2.0" is the obvious
                # way to write this and it is not a version here.
                raise errors.ValidationError(
                    f"asset versions are monotonic integers, not semver: "
                    f"{requirement!r} does not name one (try '>=2', '<3', "
                    f"'==1', or a bare label)",
                    status=422,
                ) from None
            return {
                ">=": have >= want,
                "<=": have <= want,
                "==": have == want,
                "=": have == want,
                ">": have > want,
                "<": have < want,
            }[op]
    # Bare value: exact match on version or label.
    return requirement == raw or requirement == label


def _supported_views(kind: str) -> list[str]:
    return sorted(str(view) for entity_kind, view in _VIEWS if entity_kind == kind)


def _annotate(node: dict, kind: str) -> dict:
    """Tag a browse node with its kind, ref, and the views it supports.

    `available_views` is DERIVED from `_VIEWS`, never hand-written: the matrix
    already lives there, and a second hand-maintained copy is how documentation
    ends up describing views that no longer exist. It rides on browse so an
    agent orienting through the tree learns what it can ask for WITHOUT
    discovering it by making a call that fails.
    """
    return {
        **node,
        "entity_type": str(kind),
        "ref": f"{kind}:{node.get('id')}",
        "available_views": _supported_views(kind),
    }


def _summarize_manifest(record: dict | None) -> dict | None:
    """Drop the per-file manifest rows, keep the counts that decide the answer.

    `reproduce` is an atomic view: it is never truncated, so anything unbounded
    inside it makes the whole call fail. The code manifest holds one row per
    captured file — 224 rows was 94% of the payload on a small repo and blew the
    token budget outright. The summary (`tree_sha256`, `base_commit`, `remote`,
    and the two counts) is what tells you whether the run is reproducible; the
    rows only matter once you are actually fetching bytes, and they stay
    available in full at `/v1/execution-records/{env_ref}`.
    """
    manifest = ((record or {}).get("code") or {}).get("manifest")
    if not isinstance(manifest, dict) or "entries" not in manifest:
        return record
    entries = manifest.get("entries") or []
    trimmed = {k: v for k, v in manifest.items() if k != "entries"}
    trimmed["entries_omitted"] = len(entries)
    return {
        **record,
        "code": {**record["code"], "manifest": trimmed},
    }


class ResearchReadService:
    """Compact, provenance-bearing read model exposed through MCP."""

    def __init__(self, source: ResearchOSSource):
        self.source = source

    def _envelope(
        self,
        data: Any,
        *,
        evidence: list[dict] | None = None,
        state: str = EnvelopeState.COMPLETE,
        missing: list[str] | None = None,
        next_cursor: str | None = None,
        capabilities: dict[str, bool] | None = None,
        verbose: bool = True,
    ) -> dict:
        identity = self.source.identity()
        envelope = {
            "schema_version": "1.0",
            "as_of": _now(),
            "scope": {
                "customer_id": identity.get("customer_id"),
                "researcher": identity.get("email") or identity.get("user_id"),
            },
            "capabilities": capabilities if capabilities is not None else self.source.capabilities(),
            "data": data,
            "evidence": evidence or [],
            "completeness": {"state": state, "missing": missing or []},
            "next_cursor": next_cursor,
        }
        return envelope if verbose else _compact(envelope)

    def research_context(
        self,
        task: str,
        project_ref: str | None = None,
        session_id: str | None = None,
        token_budget: int = 1800,
    ) -> dict:
        projects = self.source.projects(limit=50)
        project = None
        if project_ref:
            needle = project_ref.lower()
            project = next(
                (
                    item
                    for item in projects
                    if needle in {str(item.get("id", "")).lower(), str(item.get("slug", "")).lower()}
                ),
                None,
            )
        elif len(projects) == 1:
            project = projects[0]
        experiments = self.source.experiments(
            project_id=str(project["id"]) if project else None, limit=30
        )
        terms = set(task.lower().split())
        relevant = sorted(
            experiments,
            key=lambda item: len(terms.intersection(_text(item).split())),
            reverse=True,
        )[:5]
        active_runs: list[dict] = []
        for experiment in relevant[:3]:
            active_runs.extend(
                run
                for run in self.source.runs(experiment_id=str(experiment["id"]), limit=10)
                if run.get("status") in {"created", "running"}
            )
        if project is not None:
            # PROJECT-DIRECT runs (research-os 0054) belong to no experiment, so
            # the sweep above can never see them. direct=True filters server-side
            # (so ten attached runs can't mask an active direct one); the
            # client-side experiment_id filter stays as belt-and-braces, and the
            # SDK's project_id guard raises on a pre-0054 backend that would
            # have returned unscoped rows — degrade to the experiment sweep.
            try:
                direct_runs = self.source.runs(
                    project_id=str(project["id"]), direct=True, limit=10
                )
            except errors.NotFoundError:
                direct_runs = []
            active_runs.extend(
                run
                for run in direct_runs
                if run.get("status") in {"created", "running"}
                and not run.get("experiment_id")
            )
        # One capability lookup per operation (the probe result is cached on the
        # source, but a transient probe failure must not re-fire three times here).
        capabilities = self.source.capabilities()

        # The separate asset registry is retired: a reusable asset IS an artifact
        # now, so there is no second inventory to fetch and nothing here can be
        # missing on its account.
        missing: list[str] = []

        fixed: dict[str, Any] = {
            "task": task,
            "session_id": session_id,
            "project": project,
            "warnings": [],
        }
        # `missing` is what THIS response lacks, NOT an inventory of everything the
        # backend cannot do. Deriving it from every False capability is what pinned
        # every context envelope to partial forever: portable_snapshots is honestly
        # and permanently False, so `missing` could never be empty and stopped
        # carrying information. Only versioned_assets gates content returned here.
        sections, truncated = _fit_sections(
            [
                ("relevant_experiments", relevant),
                ("active_runs", active_runs[:10]),
                ("projects", [] if project is not None else projects),
            ],
            max(0, token_budget - _tokens(fixed)),
        )
        if truncated:
            missing.append(MissingMarker.TRUNCATED_BY_TOKEN_BUDGET)
        if project is not None:
            sections["projects"] = None
        return self._envelope(
            {**fixed, **sections},
            state=EnvelopeState.PARTIAL if missing else EnvelopeState.COMPLETE,
            missing=missing,
            capabilities=capabilities,
        )

    def browse_research(
        self,
        scope: str | None = None,
        depth: int = 1,
        status: str | None = None,
        tags: list[str] | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """The structured tree: what EXISTS, as opposed to what MATCHES a query.

        Every node carries the ids the other read tools consume, plus the views
        available for that kind, so an agent orienting here already knows what
        it can ask for next without discovering it by failed call.
        """
        try:
            payload = self.source.browse(
                scope=scope,
                depth=depth,
                status=status,
                tags=tags,
                limit=limit,
                cursor=cursor,
            )
        except errors.CapabilityUnavailable:
            # NOT an empty tree: "nothing exists" and "this server cannot tell
            # you what exists" are opposite claims, and returning the first
            # would stop an agent looking any further.
            return self._envelope(
                {"scope": scope, "projects": None, "experiments": None, "runs": None},
                state=EnvelopeState.PARTIAL,
                missing=[MissingMarker.STRUCTURED_BROWSE],
            )
        data: dict[str, Any] = {"scope": scope, "depth": payload.get("depth", depth)}
        for level, kind in (
            ("projects", EntityType.PROJECT),
            ("experiments", EntityType.EXPERIMENT),
            ("runs", EntityType.RUN),
        ):
            nodes = payload.get(level)
            data[level] = None if nodes is None else [_annotate(n, kind) for n in nodes]
        missing: list[str] = []
        if payload.get("truncated"):
            # The tree was cut, so an absent child is not evidence of absence.
            missing.append(MissingMarker.TRUNCATED_BY_TOKEN_BUDGET)
        return self._envelope(
            data,
            state=EnvelopeState.PARTIAL if missing else EnvelopeState.COMPLETE,
            missing=missing,
            next_cursor=payload.get("cursor"),
        )

    def search_knowledge(
        self,
        query: str,
        search_in: list[str] | None = None,
        project_id: str | None = None,
        workspace_id: str | None = None,
        top_k: int = 8,
        collapse: str | None = "experiment",
        verbose: bool = True,
        cursor: str | None = None,
    ) -> dict:
        """Ranked retrieval across the lab.

        `verbose` defaults True HERE because the deprecation aliases call
        straight through and must keep returning the old envelope -- an alias
        that returns a different shape is a breaking change wearing a
        compatibility label. The new tools pass verbose=False explicitly.

        Scopes are TYPED parameters, not an untyped `filters` dict. The dict
        accepted exactly one documented key while looking like it accepted many,
        and a per-key cost (project scope used to disable semantic retrieval)
        cannot be documented on a `dict[str, Any]`.

        `search_in` was called `corpora` until the rename. That old name is a
        TOOL-SURFACE concern and its rejection lives in server.py, not here: this
        is the internal API, so a Python caller passing `corpora=` gets a
        TypeError, which is already loud.
        """
        # The tool surface types this as CollapseMode so the vocabulary ships in
        # the schema; this check is what protects DIRECT Python callers, for whom
        # nothing validates. Both spellings compare equal -- StrEnum is a str.
        if collapse is not None and collapse != CollapseMode.EXPERIMENT:
            raise errors.ValidationError(
                f'unknown collapse value {collapse!r}: pass "experiment" or null',
                status=422,
            )
        corpus, unsupported = _map_search_in(search_in)
        exact_cursor, semantic_cursor = _split_cursor(cursor)
        # Split the budget per channel with NO post-merge truncation: every row
        # the backend hands us is emitted, so the per-channel cursors we return
        # never point past rows the caller has not seen.
        per_channel = max(1, min(-(-top_k // 2), _BACKEND_CHANNEL_CAP))
        try:
            response = self.source.search(
                query,
                corpus=corpus,
                workspace_id=workspace_id,
                project_id=project_id,
                top_k=per_channel,
                exact_limit=per_channel,
                exact_cursor=exact_cursor,
                semantic_cursor=semantic_cursor,
            )
        except errors.CapabilityUnavailable:
            # This backend predates POST /v1/search: keep the old servers
            # working with the structured keyword fallback. A workspace scope
            # is unsatisfiable there (no workspaces exist) — refuse loudly
            # rather than silently returning tenant-wide results.
            if workspace_id is not None:
                raise errors.ValidationError(
                    "this Probe Research backend predates POST /v1/search and "
                    "cannot scope search to a workspace; drop workspace_id or "
                    "upgrade the server",
                    status=422,
                ) from None
            return self._keyword_search(
                query, search_in, project_id, collapse, top_k, verbose=verbose
            )

        exact = _section(response, Channel.EXACT)
        semantic = _section(response, Channel.SEMANTIC)
        scoped_server_side = project_id is None or _echoes_project_scope(
            response, project_id
        )
        if not scoped_server_side:
            # The backend did not confirm it applied the scope. SearchRequest
            # does not forbid extra body fields, so a server predating
            # server-side project scope accepts `project_id`, ignores it, and
            # returns TENANT-WIDE results -- a confident wrong answer with no
            # marker on it, which is worse than any error.
            #
            # Degrade rather than refuse. Refusing would be honest but would
            # also remove a capability that works today; filtering client-side
            # is equally honest and still useful. The exact channel carries
            # project_id per row so it can be narrowed here; the semantic
            # channel cannot be, so it is EMPTIED and marked rather than passed
            # through unscoped. A caller gets fewer results and is told why.
            exact["results"] = [
                row for row in exact["results"] if _in_project(row, project_id)
            ]
            semantic = {
                "results": [],
                "cursor": None,
                "error": ChannelError.PROJECT_SCOPE_UNSUPPORTED,
                "total_candidates": None,
                "active_runs_count": None,
            }
        # Project scope is applied SERVER-SIDE now (research-os #103): the
        # backend re-resolves semantic hits against live rows and over-fetches
        # so the filter runs before the cap. The old client-side path emptied
        # the semantic channel outright and reported
        # project_scope_unsupported -- a scoped search silently became
        # trigram-only. Nothing to do here but pass the scope through.
        results = _interleave(
            [_exact_result(row) for row in exact["results"]],
            [_semantic_result(row) for row in semantic["results"]],
        )
        if collapse == EntityType.EXPERIMENT:
            results = _collapse_experiments(results)
        missing = []
        if exact["error"]:
            missing.append(MissingMarker.EXACT_SEARCH)
        if semantic["error"]:
            missing.append(MissingMarker.SEMANTIC_SEARCH)
        if unsupported:
            missing.append(MissingMarker.KB_VALUES)
        if isinstance(response, dict) and response.get("truncated"):
            # The backend trimmed the response onto its size budget, so an
            # absent document is NOT evidence of absence. Surfacing this is the
            # whole point of the backend emitting it -- a caller that cannot see
            # the trim reads a short result set as a complete one.
            missing.append(MissingMarker.TRUNCATED_BY_RESPONSE_BUDGET)
        backend_ok = (
            isinstance(response, dict) and response.get("state") == BackendSearchState.OK
        )
        return self._envelope(
            {
                "query": query,
                "collapse": collapse,
                "results": results,
                "channels": {
                    Channel.EXACT.value: {"error": exact["error"]},
                    Channel.SEMANTIC.value: {"error": semantic["error"]},
                },
                # The recall hint the instructions tell the agent to check
                # before concluding the lab has not tried something. It was
                # instructed in two places and returned in none, which is worse
                # than not mentioning it: the agent looks, finds nothing, and
                # either gives up on the check or invents a number.
                # None on backends that do not report it.
                "total_candidates": semantic.get("total_candidates"),
                "active_runs_count": exact.get("active_runs_count"),
                "unsupported_values": unsupported,
            },
            state=(
                EnvelopeState.COMPLETE
                if backend_ok and not missing
                else EnvelopeState.PARTIAL
            ),
            missing=sorted(set(missing)),
            next_cursor=_join_cursor(exact["cursor"], semantic["cursor"]),
            verbose=verbose,
        )

    def _keyword_search(
        self,
        query: str,
        search_in: list[str] | None,
        project_id: str | None,
        collapse: str | None,
        top_k: int,
        verbose: bool = True,
    ) -> dict:
        """Pre-/v1/search behavior: keyword match over experiment cards only
        (project-scoped via filters.project_id). This path cannot paginate, so
        any incoming cursor is ignored and next_cursor is always None — echoing
        a packed /v1/search cursor here would make cursor-following consumers
        loop forever on version skew.

        It also cannot honor `search_in` narrowing: there is nothing here but
        experiment cards, so a caller who narrowed AWAY from experiments still
        gets experiment rows. That is the one place the tool's "narrowing" wording
        does not hold, which is exactly why any knowledge corpus on this path
        raises the kb_values marker — the envelope says `partial` rather than
        letting the rows pass as a complete answer to a question they do not
        answer. Backends this old predate the hosted service; the marker is the
        contract here, not silence."""
        experiments = self.source.experiments(project_id=project_id, limit=100)
        terms = set(query.lower().split())
        results = []
        for item in experiments:
            haystack = _text(item)
            matched = sorted(term for term in terms if term in haystack)
            if matched or not terms:
                results.append(
                    {
                        "entity_type": EntityType.EXPERIMENT.value,
                        "id": item.get("id"),
                        "card": {
                            "name": item.get("name"),
                            "hypothesis": item.get("hypothesis"),
                            "summary": item.get("summary") or {},
                        },
                        "why_matched": _why_matched(
                            MatchMode.KEYWORD_FALLBACK, Channel.KEYWORD, terms=matched
                        ),
                        "resource": f"research://experiments/{item.get('id')}/card",
                    }
                )
        results.sort(key=lambda item: len(item["why_matched"]["terms"]), reverse=True)
        missing = []
        if not self.source.capabilities()[Capability.SEMANTIC_SEARCH]:
            missing.append(MissingMarker.SEMANTIC_SEARCH)
        if search_in and any(v in _KB_TOOL_VALUES for v in search_in):
            missing.append(MissingMarker.KB_VALUES)
        return self._envelope(
            {"query": query, "collapse": collapse, "results": results[:top_k]},
            state=EnvelopeState.PARTIAL if missing else EnvelopeState.COMPLETE,
            missing=sorted(set(missing)),
            verbose=verbose,
            next_cursor=None,
        )

    # -- research_get --------------------------------------------------------

    def _checked_view(self, kind: str, view: str) -> str:
        """The view must EXIST for this kind. Rejecting loudly beats the old
        behavior — an unknown view used to fall through to a card-shaped payload,
        and contract/versions/usage returned an envelope that always said
        `missing`, which reads as "temporarily degraded" rather than "not a
        thing". The error names what this kind actually supports."""
        if (kind, view) in _VIEWS:
            return view
        raise errors.ValidationError(
            f"view={view!r} is not available for a {kind}; "
            f"{kind} supports {_supported_views(kind)}",
            status=422,
        )

    def _checked_filters(
        self, kind: str, view: str, filters: dict[str, Any] | None
    ) -> dict[str, Any]:
        # Empty values are dropped, not passed through: `{"key": ""}` is not a
        # filter. Kept, it would echo back in the payload as though it had been
        # applied while every truthiness check downstream ignored it.
        supplied = {
            key: value
            for key, value in (filters or {}).items()
            if value is not None and value != ""
        }
        allowed = _VIEW_FILTERS.get((kind, view), set())
        unknown = sorted(set(supplied) - allowed)
        if not unknown:
            return supplied
        detail = (
            f"view={view!r} on a {kind} accepts no filters"
            if not allowed
            else f"supported filters for view={view!r} on a {kind}: {sorted(allowed)}"
        )
        raise errors.ValidationError(f"unknown filter(s) {unknown}: {detail}", status=422)

    def get_entity(
        self,
        ref: str,
        view: str = View.CARD,
        token_budget: int = 2000,
        cursor: str | None = None,
        filters: dict[str, Any] | None = None,
        verbose: bool = True,
    ) -> dict:
        """One entity, one purpose-shaped view. See `_VIEWS` for the real matrix.

        token_budget bounds ROWS — the only part of any view that scales. Atomic
        views (reproduce) are never silently truncated: a reproduction manifest
        with fields dropped to fit reproduces nothing, so overflow is REPORTED
        (`token_budget_exceeded`) instead of corrupting the answer. Row views that
        do not fit report `truncated_by_token_budget` + a `next_cursor`.
        """
        kind, entity = self.source.get(ref)
        view = self._checked_view(kind, view)
        request = _Req(
            filters=self._checked_filters(kind, view, filters),
            offset=_split_get_cursor(cursor, view),
        )
        result: _ViewData = getattr(self, _VIEWS[(kind, view)])(entity, request)

        data: dict[str, Any] = {
            "entity_type": kind,
            "entity": entity,
            "view": str(view),
            **result.payload,
        }
        if view == View.CARD:
            # The default view teaches what else you can ask for, DERIVED from
            # the same matrix that validates the request. Discovery by failed
            # call is a fine contract with four views; with eleven across five
            # kinds it is a tax on every first look at an unfamiliar entity.
            data["available_views"] = _supported_views(kind)
        missing = list(result.missing)
        next_cursor: str | None = None

        if result.rows is None:
            if request.offset:
                raise errors.ValidationError(
                    f"view={view!r} returns a single payload and cannot be paginated",
                    status=422,
                )
        else:
            # Spend the budget on rows only after the fixed part is paid for.
            # _fit still emits one row at a floor of 0, so a caller always makes
            # progress and the overflow is reported below rather than hidden.
            window = result.rows[request.offset :]
            emitted = _fit(window, max(0, token_budget - _tokens(data)))
            data[result.rows_key] = emitted
            budget_cut = len(emitted) < len(window)
            if budget_cut or result.more_beyond:
                next_cursor = _join_get_cursor(view, request.offset + len(emitted))
            if budget_cut:
                # Only a BUDGET cut is "partial". Reaching the end of a fetch
                # window is ordinary pagination — research_search returns a cursor
                # with state=complete for exactly that, and this stays consistent
                # with it. Either way next_cursor is the signal that more exists.
                missing.append(MissingMarker.TRUNCATED_BY_TOKEN_BUDGET)

        if _tokens(data) > token_budget and MissingMarker.TRUNCATED_BY_TOKEN_BUDGET not in missing:
            missing.append(MissingMarker.TOKEN_BUDGET_EXCEEDED)
        return self._envelope(
            data,
            state=EnvelopeState.PARTIAL if missing else EnvelopeState.COMPLETE,
            missing=missing,
            next_cursor=next_cursor,
            verbose=verbose,
        )

    # -- view builders -------------------------------------------------------
    # Contract: report what is genuinely absent in `missing`, and NEVER report it
    # unconditionally — an always-`missing` view is the lie this rewrite removes.

    @staticmethod
    def _bounded(fetch: Any, offset: int, backend_max: int) -> tuple[list[dict], bool, bool]:
        """Fetch the caller's window plus ONE lookahead row, then drop it.

        Returns ``(rows, more_beyond, capped)``. The lookahead makes "are there more
        rows?" a fact instead of a guess, with no false positive when the window
        lands exactly on the end.

        `capped` means the BACKEND refused to go further (it returned its own
        ceiling), which is the only thing that makes rows genuinely unreachable.
        Inferring it from the offset instead reports the ceiling on a short run that
        was read in full -- a false `missing` marker, which corrupts the exact
        signal the envelope exists to carry.

        CALLERS MUST USE `capped`. At the ceiling `want == backend_max`, so the
        lookahead row cannot be fetched and `more_beyond` is False BY CONSTRUCTION:
        a caller that ignores `capped` there emits state="complete" with no cursor
        while rows sit unread. `capped` is the only signal left at that boundary.

        TODO(backend): these routes take `limit` and no offset, so each page refetches
        from row 0 and a full walk is quadratic (a 6000-span walk pulls ~90k rows).
        One backend call per page, but a linearly growing one. An `offset`/cursor on
        GET /v1/runs/{id}/spans would make this linear."""
        want = min(offset + _PAGE_FETCH, backend_max)
        fetched = fetch(min(want + 1, backend_max))
        return fetched[:want], len(fetched) > want, len(fetched) >= backend_max

    def _view_card(self, entity: dict, request: _Req) -> _ViewData:
        """The cheap glance: the entity as the backend returned it, no extra calls."""
        return _ViewData()

    def _view_trajectory(self, entity: dict, request: _Req) -> _ViewData:
        """The spans themselves. The run bundle carries span_type COUNTS, so before
        this an agent could see that 500 rollouts happened and not one of what they
        did — the sharpest gap for an RL-pitched product."""
        run_id = str(entity["id"])
        spans, more, capped = self._bounded(
            lambda limit: self.source.run_spans(run_id, limit=limit, **request.filters),
            request.offset,
            _SPAN_BACKEND_MAX,
        )
        return _ViewData(
            payload={"filters": request.filters or None},
            rows=spans,
            rows_key="spans",
            missing=[MissingMarker.SPANS_BEYOND_BACKEND_LIMIT] if capped else [],
            more_beyond=more,
        )

    def _view_metrics(self, entity: dict, request: _Req) -> _ViewData:
        """Series summaries by default; `filters.key` drills through to the raw
        points. Progressive disclosure inside one view, rather than dumping every
        metric point a run ever logged."""
        run_id = str(entity["id"])
        if request.filters.get("key"):
            points, more, capped = self._bounded(
                lambda limit: self.source.run_metrics(run_id, limit=limit, **request.filters),
                request.offset,
                _METRIC_BACKEND_MAX,
            )
            return _ViewData(
                payload={"granularity": "points", "filters": request.filters},
                rows=points,
                rows_key="points",
                missing=[MissingMarker.METRIC_POINTS_BEYOND_BACKEND_LIMIT] if capped else [],
                more_beyond=more,
            )
        series = self.source.run_series(run_id)
        kind = request.filters.get("kind")
        if kind:  # GET /v1/runs/{id}/series takes no filters; narrow client-side
            series = [row for row in series if row.get("kind") == kind]
        return _ViewData(
            payload={"granularity": "series_summary", "filters": request.filters or None},
            rows=series,
            rows_key="series",
        )

    def _view_run_artifacts(self, entity: dict, request: _Req) -> _ViewData:
        rows = self.source.run_artifacts(str(entity["id"]), **request.filters)
        payload: dict = {"filters": request.filters or None}
        # Async-outbox visibility (eng review 2026-07-29, 1A): a row with
        # status "pending" is a REGISTERED INTENT whose bytes have not arrived
        # (a client outbox may still deliver it); "failed" means the grace
        # window expired undelivered. Surfacing the counts here keeps the
        # metadata-match-is-not-the-blob rule unmissable for agents.
        pending = sum(1 for r in rows if isinstance(r, dict) and r.get("status") == "pending")
        failed = sum(1 for r in rows if isinstance(r, dict) and r.get("status") == "failed")
        if pending or failed:
            payload["undelivered"] = {
                "pending": pending,
                "failed": failed,
                "note": (
                    "pending = upload intent registered, bytes not yet arrived "
                    "(an async client outbox may still deliver); failed = the "
                    "grace window expired undelivered. Only status='complete' "
                    "rows have retrievable bytes."
                ),
            }
        return _ViewData(payload=payload, rows=rows, rows_key="artifacts")

    def _view_experiment_artifacts(self, entity: dict, request: _Req) -> _ViewData:
        return _ViewData(
            rows=self.source.experiment_artifacts(str(entity["id"])), rows_key="artifacts"
        )

    def _hypothesis_of(self, entity: dict, missing: list[str]) -> str | None:
        """A run's hypothesis lives on its experiment. Appends to `missing` rather
        than raising: a run whose experiment vanished is still worth reading, and
        the envelope is where that absence gets reported.

        A PROJECT-DIRECT run (experiment_id null WITH a project_id, the W&B
        shape) legitimately has no experiment — no hypothesis, and no missing
        marker either: nothing failed to load. The marker is reserved for a run
        that names an experiment this call could not read, and for old rows
        that carry neither id (pre-project_id backends)."""
        experiment_id = entity.get("experiment_id")
        if not experiment_id:
            if not entity.get("project_id"):
                missing.append(MissingMarker.EXPERIMENT)
            return None
        try:
            return self.source.experiment(str(experiment_id)).get("hypothesis")
        except errors.NotFoundError:
            missing.append(MissingMarker.EXPERIMENT)
            return None

    def _view_reproduce(self, entity: dict, request: _Req) -> _ViewData:
        """Hypothesis + the pinned environment + config — an actual reproduction,
        where this used to hand back the same bundle as three other views."""
        missing: list[str] = []
        hypothesis = self._hypothesis_of(entity, missing)
        env_ref = entity.get("env_ref")
        record = None
        if env_ref:
            try:
                record = self.source.execution_record(str(env_ref))
            except errors.NotFoundError:
                missing.append(MissingMarker.EXECUTION_RECORD)
        else:
            # Conditional, not decorative: this run genuinely captured no
            # environment, so it cannot be reproduced from here.
            missing.append(MissingMarker.EXECUTION_RECORD)
        return _ViewData(
            payload={
                "hypothesis": hypothesis,
                "config": entity.get("config"),
                "env_ref": env_ref,
                "execution_record": _summarize_manifest(record),
            },
            missing=missing,
        )

    def _view_handoff(self, entity: dict, request: _Req) -> _ViewData:
        """What a new session needs to continue.

        This is the one view the run bundle was always right for — state, series,
        lineage, and span_type counts that say a trajectory EXISTS and is worth a
        view="trajectory" call. The bug was never the bundle; it was four views
        sharing it. Artifacts are the part that scales, so they are the rows."""
        bundle = self.source.bundle(str(entity["id"]))
        missing: list[str] = []
        hypothesis = self._hypothesis_of(entity, missing)
        artifacts = bundle.get("artifacts") or []
        total = bundle.get("artifact_total")
        # The bundle's artifact list is capped SERVER-side (200) while artifact_total
        # counts them all, and the route takes no offset — so a cursor here would
        # page an already-truncated list and hand back an empty page as if it were
        # the end. Say it plainly and name the uncapped door instead: on a 5000-
        # artifact run this view would otherwise emit 200 and report `complete`.
        if isinstance(total, int) and total > len(artifacts):
            missing.append(MissingMarker.ARTIFACTS_BEYOND_BUNDLE_LIMIT)
        return _ViewData(
            payload={
                "hypothesis": hypothesis,
                "run": bundle.get("run"),
                "series": bundle.get("series"),
                "span_types": bundle.get("span_types"),
                "artifact_total": total,
                "parent_run_id": bundle.get("parent_run_id"),
                "child_run_ids": bundle.get("child_run_ids"),
            },
            rows=artifacts,
            rows_key="artifacts",
            missing=missing,
        )

    def _view_run_lineage(self, entity: dict, request: _Req) -> _ViewData:
        return _ViewData(payload={"lineage": self.source.lineage(str(entity["id"]))})

    def _view_experiment_lineage(self, entity: dict, request: _Req) -> _ViewData:
        """Lineage was run-only; experiment_edges was in the SDK the whole time."""
        return _ViewData(
            rows=self.source.experiment_edges(str(entity["id"])), rows_key="edges"
        )

    def _view_events(self, entity: dict, request: _Req) -> _ViewData:
        return _ViewData(rows=self.source.run_events(str(entity["id"])), rows_key="events")

    def _view_groups(self, entity: dict, request: _Req) -> _ViewData:
        """Sweeps/ensembles under an experiment — reached by a view, not by a
        research_list_groups tool. One group is research_get(ref="group:<id>")."""
        return _ViewData(
            rows=self.source.experiment_groups(str(entity["id"])), rows_key="groups"
        )

    def _view_versions(self, entity: dict, request: _Req) -> _ViewData:
        """Real, against the live registry: this view used to unconditionally
        report missing:["versioned_assets"] and had never been implemented."""
        return _ViewData(
            rows=self.source.experiment_versions(str(entity["id"])), rows_key="versions"
        )

    # -- coordinate reads (below-run coordinates, 0059-0062) -----------------

    def metrics_grouped(
        self,
        run_id: str,
        key: str,
        *,
        kind: str | None = None,
        agg: str | None = None,
        by: list[str] | None = None,
        where: dict[str, Any] | None = None,
        step_bucket: int | None = None,
        step_from: int | None = None,
        step_to: int | None = None,
        max_rows: int | None = None,
    ) -> dict:
        """One bounded server-side reduction. ``max_rows`` clamps to the tool's
        row bound — grouped cells are aggregates, so the clamp cuts pathology,
        not typical reads — and a cut read reports partial with the resume step
        in ``next_cursor``."""
        bound = min(max_rows or _GROUPED_ROWS_DEFAULT, _GROUPED_ROWS_MAX)
        payload = self.source.run_metrics_grouped(
            run_id,
            key,
            kind=kind,
            agg=agg,
            by=by,
            where=where,
            step_bucket=step_bucket,
            step_from=step_from,
            step_to=step_to,
            max_rows=bound,
        )
        missing = [MissingMarker.ROWS_BEYOND_PAGE_BOUND] if payload.get("truncated") else []
        next_step = payload.get("next_step")
        return self._envelope(
            {"run_id": run_id, **payload},
            state=EnvelopeState.PARTIAL if missing else EnvelopeState.COMPLETE,
            missing=missing,
            next_cursor=str(next_step) if missing and next_step is not None else None,
        )

    def run_coordinates(self, run_id: str) -> dict:
        """The coordinate catalog is bounded by the series cap's cardinality
        arithmetic (no pagination on the route), so this is one complete read."""
        return self._envelope(
            {"run_id": run_id, "coordinates": self.source.run_coordinates(run_id)}
        )

    def metrics_export(
        self,
        run_id: str,
        *,
        key: str | None = None,
        kind: str | None = None,
        step_from: int | None = None,
        step_to: int | None = None,
        after_id: int | None = None,
        limit: int | None = None,
    ) -> dict:
        """ONE keyset page of the lossless export. The SDK generator would follow
        the walk to the last point; a tool response cannot, so the page is sliced
        off it here and the cursor handed back as ``next_cursor`` (pass it back
        as ``after_id``)."""
        bound = max(1, min(limit or _EXPORT_PAGE_DEFAULT, _EXPORT_PAGE_MAX))
        filters = {
            name: value
            for name, value in {
                "key": key,
                "kind": kind,
                "step_from": step_from,
                "step_to": step_to,
                "after_id": after_id,
            }.items()
            if value is not None
        }
        walk = self.source.export_points(run_id, limit=bound, **filters)
        # Lookahead past the page bound, so "more points exist" is a fact rather
        # than an inference from a full page (the _bounded contract).
        points = list(islice(walk, bound + 1))
        more = len(points) > bound
        points = points[:bound]
        return self._envelope(
            {"run_id": run_id, "filters": filters or None, "points": points},
            state=EnvelopeState.PARTIAL if more else EnvelopeState.COMPLETE,
            missing=[MissingMarker.ROWS_BEYOND_PAGE_BOUND] if more else [],
            next_cursor=str(points[-1]["id"]) if more else None,
        )

    def research_compare(
        self,
        refs: list[str],
        dimensions: list[str] | None = None,
    ) -> dict:
        if len(refs) < 2:
            raise ValueError("compare requires at least two refs")
        rows = []
        for ref in refs:
            kind, entity = self.source.get(ref)
            rows.append({"ref": ref, "entity_type": kind, "entity": entity})
        requested = dimensions or ["config", "metadata", "summary", "status", "hypothesis"]
        comparison = {
            dimension: [row["entity"].get(dimension) for row in rows]
            for dimension in requested
        }
        return self._envelope({"entities": rows, "comparison": comparison})

