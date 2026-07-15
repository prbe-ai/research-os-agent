"""Framework-independent implementation of the six read-only MCP operations."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from ..sdk import errors
from .source import ResearchOSSource

# Tool corpora vocabulary -> backend /v1/search `corpus` values. Experiments are
# always searched (the tool's core corpus); transcripts have no backend corpus
# yet and are reported as a missing kb_corpora capability.
_CORPORA_TO_BACKEND: dict[str, set[str]] = {
    "assets": {"files"},
    "procedures": {"files"},
    "documents": {"github", "files"},
    "experiments": {"experiments"},
}


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
    backend: set[str] = {"experiments"}
    unsupported: list[str] = []
    for corpus in corpora:
        mapped = _CORPORA_TO_BACKEND.get(corpus)
        if mapped is None:
            unsupported.append(corpus)
        else:
            backend.update(mapped)
    return sorted(backend), sorted(set(unsupported))


def _exact_result(row: dict) -> dict:
    """An exact-channel hit (project | experiment | artifact) in the tool's result shape."""
    entity_type = row.get("entity_type")
    entity_id = row.get("id")
    card = {
        key: row.get(key)
        for key in ("name", "slug", "workspace_id", "project_id", "experiment_id", "run_id")
        if row.get(key) is not None
    }
    return {
        "entity_type": entity_type,
        "id": entity_id,
        "card": card,
        "why_matched": {"mode": "exact", "channel": "exact", "score": row.get("score")},
        "resource": (
            f"research://experiments/{entity_id}/card" if entity_type == "experiment" else None
        ),
    }


def _semantic_result(row: dict) -> dict:
    """A semantic-channel document hit (engine) in the tool's result shape."""
    ref = row.get("ref") or {}
    kind = ref.get("kind")
    entity_id = ref.get("id") or row.get("doc_id")
    resource = None
    if kind == "experiment":
        resource = f"research://experiments/{entity_id}/card"
    elif kind == "run":
        resource = f"research://runs/{entity_id}/handoff"
    card = {
        key: row.get(key)
        for key in ("title", "snippet", "source_system", "source_url", "doc_id")
        if row.get(key) is not None
    }
    return {
        "entity_type": kind or "document",
        "id": entity_id,
        "card": card,
        "why_matched": {"mode": "semantic", "channel": "semantic", "score": row.get("score")},
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


def _split_cursor(cursor: str | None) -> tuple[str | None, str | None]:
    """The tool's opaque cursor is a JSON object of per-channel backend cursors."""
    if not cursor:
        return None, None
    try:
        parsed = json.loads(cursor)
        if not isinstance(parsed, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        raise ValueError(
            "malformed cursor: pass the next_cursor value from a previous research_search call"
        ) from None
    return parsed.get("exact"), parsed.get("semantic")


def _join_cursor(exact: str | None, semantic: str | None) -> str | None:
    cursors = {key: value for key, value in (("exact", exact), ("semantic", semantic)) if value}
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
        state: str = "complete",
        missing: list[str] | None = None,
        next_cursor: str | None = None,
    ) -> dict:
        identity = self.source.identity()
        return {
            "schema_version": "1.0",
            "as_of": _now(),
            "scope": {
                "customer_id": identity.get("customer_id"),
                "researcher": identity.get("email") or identity.get("user_id"),
            },
            "capabilities": self.source.capabilities(),
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
        missing = [
            key for key, enabled in self.source.capabilities().items() if not enabled
        ]
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
                    if not self.source.capabilities()["versioned_assets"]
                    else []
                ),
            },
            state="partial" if missing else "complete",
            missing=missing,
        )

    def research_search(
        self,
        query: str,
        corpora: list[str] | None = None,
        filters: dict[str, Any] | None = None,
        collapse: str = "experiment",
        limit: int = 8,
        cursor: str | None = None,
        workspace_id: str | None = None,
    ) -> dict:
        filters = filters or {}
        workspace_id = workspace_id or filters.get("workspace_id")
        corpus, unsupported = _map_corpora(corpora)
        exact_cursor, semantic_cursor = _split_cursor(cursor)
        page = max(1, min(limit, 50))  # backend caps top_k/exact_limit at 50
        try:
            response = self.source.search(
                query,
                corpus=corpus,
                workspace_id=workspace_id,
                top_k=page,
                exact_limit=page,
                exact_cursor=exact_cursor,
                semantic_cursor=semantic_cursor,
            )
        except errors.CapabilityUnavailable:
            # This backend predates POST /v1/search: keep the old servers working
            # with the structured keyword fallback.
            return self._keyword_search(query, corpora, filters, collapse, limit, cursor)

        exact = response.get("exact") or {}
        semantic = response.get("semantic") or {}
        results = _interleave(
            [_exact_result(row) for row in exact.get("results") or []],
            [_semantic_result(row) for row in semantic.get("results") or []],
        )[:limit]
        missing = []
        if exact.get("error"):
            missing.append("exact_search")
        if semantic.get("error"):
            missing.append("semantic_search")
        if unsupported:
            missing.append("kb_corpora")
        return self._envelope(
            {
                "query": query,
                "collapse": collapse,
                "results": results,
                "channels": {
                    "exact": {"error": exact.get("error")},
                    "semantic": {"error": semantic.get("error")},
                },
                "unsupported_corpora": unsupported,
            },
            state="complete" if response.get("state") == "ok" and not missing else "partial",
            missing=sorted(set(missing)),
            next_cursor=_join_cursor(exact.get("cursor"), semantic.get("cursor")),
        )

    def _keyword_search(
        self,
        query: str,
        corpora: list[str] | None,
        filters: dict[str, Any],
        collapse: str,
        limit: int,
        cursor: str | None,
    ) -> dict:
        """Pre-/v1/search behavior: keyword match over experiment cards only."""
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
                        "entity_type": "experiment",
                        "id": item.get("id"),
                        "card": {
                            "name": item.get("name"),
                            "hypothesis": item.get("hypothesis"),
                            "summary": item.get("summary") or {},
                        },
                        "why_matched": {"mode": "keyword_fallback", "terms": matched},
                        "resource": f"research://experiments/{item.get('id')}/card",
                    }
                )
        results.sort(key=lambda item: len(item["why_matched"]["terms"]), reverse=True)
        missing = []
        if not self.source.capabilities()["semantic_search"]:
            missing.append("semantic_search")
        if corpora and any(c in {"assets", "procedures", "documents", "transcripts"} for c in corpora):
            missing.append("kb_corpora")
        return self._envelope(
            {"query": query, "collapse": collapse, "results": results[:limit]},
            state="partial" if missing else "complete",
            missing=sorted(set(missing)),
            next_cursor=cursor,
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
