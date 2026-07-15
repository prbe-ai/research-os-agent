"""Framework-independent implementation of the six read-only MCP operations."""

from __future__ import annotations

import json
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
)
from .source import ResearchOSSource

# Tool corpora vocabulary -> backend /v1/search `corpus` values. Experiments are
# always searched (the tool's core corpus); transcripts have no backend corpus
# yet and are reported as a missing kb_corpora capability.
_CORPORA_TO_BACKEND: dict[str, set[BackendCorpus]] = {
    ToolCorpus.ASSETS: {BackendCorpus.FILES},
    ToolCorpus.PROCEDURES: {BackendCorpus.FILES},
    ToolCorpus.DOCUMENTS: {BackendCorpus.GITHUB, BackendCorpus.FILES},
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


def _split_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    """The tool's opaque cursor is a JSON object of per-channel backend cursors."""
    if not cursor:
        return None, None
    try:
        parsed = json.loads(cursor)
        if not isinstance(parsed, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise errors.ValidationError(
            "malformed cursor: pass the next_cursor value from a previous "
            "research_search call",
            status=422,
        ) from None
    return parsed.get(Channel.EXACT), parsed.get(Channel.SEMANTIC)


def _join_cursor(exact: str | None, semantic: str | None) -> str | None:
    cursors = {
        key: value
        for key, value in ((Channel.EXACT.value, exact), (Channel.SEMANTIC.value, semantic))
        if value
    }
    return json.dumps(cursors, sort_keys=True) if cursors else None


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
        missing = [key for key, enabled in capabilities.items() if not enabled]
        return self._envelope(
            {
                "task": task,
                "session_id": session_id,
                "token_budget": token_budget,
                "project": project,
                "projects": projects if project is None else None,
                "active_runs": active_runs[:10],
                "relevant_experiments": relevant,
                "official_assets": [],
                "warnings": (
                    ["versioned asset resolution is unavailable on API v3"]
                    if not capabilities[Capability.VERSIONED_ASSETS]
                    else []
                ),
            },
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

    def research_get(
        self,
        ref: str,
        view: str = "card",
        token_budget: int = 2000,
        cursor: str | None = None,
    ) -> dict:
        kind, entity = self.source.get(ref)
        data: dict[str, Any] = {"entity_type": kind, "entity": entity, "view": view}
        missing: list[str] = []
        if kind == "run" and view in {"reproduce", "handoff", "metrics", "artifacts"}:
            data["bundle"] = self.source.bundle(str(entity["id"]))
        if kind == "run" and view == "lineage":
            data["lineage"] = self.source.lineage(str(entity["id"]))
        if kind != "run" and view in {"reproduce", "handoff", "lineage", "metrics", "artifacts"}:
            missing.append(f"{view}_view_for_{kind}")
        if view in {"contract", "versions", "usage"}:
            missing.append("versioned_assets")
        return self._envelope(
            {**data, "token_budget": token_budget},
            state="partial" if missing else "complete",
            missing=missing,
            next_cursor=cursor,
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
        try:
            result = self.source.resolve_asset(
                name=name, kind=kind, requirement=requirement, at=at
            )
            return self._envelope(result)
        except errors.CapabilityUnavailable:
            return self._envelope(
                {
                    "state": "no_match",
                    "name": name,
                    "kind": kind,
                    "requirement": requirement,
                    "at": at,
                    "searched": [],
                    "warning": "asset registry is not implemented on API v3",
                },
                state="partial",
                missing=["versioned_assets"],
            )

    def research_trace_file(self, query: str) -> dict:
        result = self.source.trace_file(query)
        missing = [result["missing_capability"]] if result.get("missing_capability") else []
        return self._envelope(
            result,
            state="partial" if missing else "complete",
            missing=missing,
        )
