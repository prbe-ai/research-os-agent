"""Framework-independent implementation of the six read-only MCP operations."""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..sdk import errors
from .contract import (
    BackendCorpus,
    BackendSearchState,
    Capability,
    Channel,
    ChannelError,
    EntityType,
    EnvelopeState,
    MatchMode,
    MissingMarker,
    ToolCorpus,
    View,
)
from .source import ResearchOSSource

# Tool corpora vocabulary -> backend /v1/search `corpus` values. Experiments are
# always searched (the tool's core corpus). Transcripts now have a backend corpus
# (POST /v1/search accepts and defaults to it), so the tool maps them through
# instead of degrading them to an unsupported kb_corpora miss.
_CORPORA_TO_BACKEND: dict[str, set[BackendCorpus]] = {
    ToolCorpus.ASSETS: {BackendCorpus.FILES},
    ToolCorpus.PROCEDURES: {BackendCorpus.FILES},
    ToolCorpus.DOCUMENTS: {BackendCorpus.GITHUB, BackendCorpus.FILES},
    ToolCorpus.TRANSCRIPTS: {BackendCorpus.TRANSCRIPTS},
    ToolCorpus.EXPERIMENTS: {BackendCorpus.EXPERIMENTS},
}

# The knowledge-side tool corpora (everything except the always-on experiments).
_KB_TOOL_CORPORA = {
    ToolCorpus.ASSETS,
    ToolCorpus.PROCEDURES,
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


def _map_corpora(corpora: list[str] | None) -> tuple[list[str] | None, list[str]]:
    """Translate the tool's corpora vocabulary into backend corpus values.

    Returns ``(backend_corpus_or_None, unsupported_corpora)``. No corpora means
    no filter (the backend searches every corpus)."""
    if not corpora:
        return None, []
    backend: set[str] = {BackendCorpus.EXPERIMENTS}
    unsupported: list[str] = []
    for corpus in corpora:
        mapped = _CORPORA_TO_BACKEND.get(corpus)
        if mapped is None:
            unsupported.append(corpus)
        else:
            backend.update(mapped)
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
    """``collapse="experiment"``: experiment-level hits only, one per experiment
    id, keeping the best-scoring representative's channel provenance."""
    best: dict[Any, dict] = {}
    for row in results:
        if row.get("entity_type") != EntityType.EXPERIMENT:
            continue
        kept = best.get(row.get("id"))
        if kept is None or _score(row) > _score(kept):
            best[row.get("id")] = row  # replacement keeps first-seen order
    return list(best.values())


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

# Registry rows pulled for research_context's official_assets. GET /v1/assets caps
# `limit` at 200; the token budget trims below this anyway.
_CONTEXT_ASSET_LIMIT = 50


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
    (EntityType.RUN, View.ARTIFACTS): {"kind", "step_from", "step_to"},
}


def _supported_views(kind: str) -> list[str]:
    return sorted(str(view) for entity_kind, view in _VIEWS if entity_kind == kind)


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
    ) -> dict:
        identity = self.source.identity()
        return {
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
        # One capability lookup per operation (the probe result is cached on the
        # source, but a transient probe failure must not re-fire three times here).
        capabilities = self.source.capabilities()

        # `official_assets` was hardcoded [] behind a warning that the registry was
        # "unavailable on API v3". The registry is live, so the warning was the only
        # thing keeping the empty list from reading as "there are no official
        # assets" — an answer we had never actually looked for.
        official_assets: list[dict] = []
        missing: list[str] = []
        if capabilities[Capability.VERSIONED_ASSETS]:
            try:
                official_assets = self.source.assets(limit=_CONTEXT_ASSET_LIMIT)
            except errors.RosError:
                missing.append(Capability.VERSIONED_ASSETS)
        else:
            missing.append(Capability.VERSIONED_ASSETS)

        fixed: dict[str, Any] = {
            "task": task,
            "session_id": session_id,
            "project": project,
            "warnings": (
                ["versioned asset registry is unreachable; official_assets is not authoritative"]
                if missing
                else []
            ),
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
                ("official_assets", official_assets),
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

    def research_search(
        self,
        query: str,
        corpora: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        collapse: str | None = "experiment",
        limit: int = 8,
        cursor: str | None = None,
        workspace_id: str | None = None,
    ) -> dict:
        if collapse is not None and collapse != EntityType.EXPERIMENT:
            raise errors.ValidationError(
                f'unknown collapse value {collapse!r}: pass "experiment" or null',
                status=422,
            )
        filters = filters or {}
        workspace_id = workspace_id or filters.get("workspace_id")
        project_id = filters.get("project_id")
        corpus, unsupported = _map_corpora(corpora)
        exact_cursor, semantic_cursor = _split_cursor(cursor)
        # Split the budget per channel with NO post-merge truncation: every row
        # the backend hands us is emitted, so the per-channel cursors we return
        # never point past rows the caller has not seen.
        per_channel = max(1, min(-(-limit // 2), _BACKEND_CHANNEL_CAP))
        try:
            response = self.source.search(
                query,
                corpus=corpus,
                workspace_id=workspace_id,
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
            return self._keyword_search(query, corpora, filters, collapse, limit)

        exact = _section(response, Channel.EXACT)
        semantic = _section(response, Channel.SEMANTIC)
        if project_id is not None:
            # The backend has no project filter; scope client-side instead of
            # silently returning tenant-wide hits. Exact rows carry project_id
            # (rows without a project linkage are conservatively dropped). The
            # semantic channel cannot be project-scoped cheaply, so it is
            # excluded and marked — never pretended to cover the project.
            exact["results"] = [row for row in exact["results"] if _in_project(row, project_id)]
            semantic = {
                "results": [],
                "cursor": None,
                "error": ChannelError.PROJECT_SCOPE_UNSUPPORTED,
            }
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
            missing.append(MissingMarker.KB_CORPORA)
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
                "unsupported_corpora": unsupported,
            },
            state=(
                EnvelopeState.COMPLETE
                if backend_ok and not missing
                else EnvelopeState.PARTIAL
            ),
            missing=sorted(set(missing)),
            next_cursor=_join_cursor(exact["cursor"], semantic["cursor"]),
        )

    def _keyword_search(
        self,
        query: str,
        corpora: list[str] | None,
        filters: dict[str, Any],
        collapse: str | None,
        limit: int,
    ) -> dict:
        """Pre-/v1/search behavior: keyword match over experiment cards only
        (project-scoped via filters.project_id). This path cannot paginate, so
        any incoming cursor is ignored and next_cursor is always None — echoing
        a packed /v1/search cursor here would make cursor-following consumers
        loop forever on version skew."""
        project_id = filters.get("project_id")
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
        if corpora and any(c in _KB_TOOL_CORPORA for c in corpora):
            missing.append(MissingMarker.KB_CORPORA)
        return self._envelope(
            {"query": query, "collapse": collapse, "results": results[:limit]},
            state=EnvelopeState.PARTIAL if missing else EnvelopeState.COMPLETE,
            missing=sorted(set(missing)),
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

    def research_get(
        self,
        ref: str,
        view: str = View.CARD,
        token_budget: int = 2000,
        cursor: str | None = None,
        filters: dict[str, Any] | None = None,
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
        return _ViewData(
            payload={"filters": request.filters or None},
            rows=self.source.run_artifacts(str(entity["id"]), **request.filters),
            rows_key="artifacts",
        )

    def _view_experiment_artifacts(self, entity: dict, request: _Req) -> _ViewData:
        return _ViewData(
            rows=self.source.experiment_artifacts(str(entity["id"])), rows_key="artifacts"
        )

    def _hypothesis_of(self, entity: dict, missing: list[str]) -> str | None:
        """A run's hypothesis lives on its experiment. Appends to `missing` rather
        than raising: a run whose experiment vanished is still worth reading, and
        the envelope is where that absence gets reported."""
        experiment_id = entity.get("experiment_id")
        if not experiment_id:
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
                "execution_record": record,
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

    def research_resolve(
        self,
        name: str,
        kind: str | None = None,
        requirement: str | None = None,
        at: str | None = None,
    ) -> dict:
        # The old `except CapabilityUnavailable` branch here was UNREACHABLE:
        # source.resolve_asset -> client.assets.resolve never raises it (the only
        # raisers are the /v1/search probe and run.env_ref). It returned a
        # hardcoded no_match warning that "the asset registry is not implemented
        # on API v3" — while /v1/assets has been live all along. assets.resolve
        # already reports an honest state=match|no_match of its own.
        return self._envelope(
            self.source.resolve_asset(name=name, kind=kind, requirement=requirement, at=at)
        )
