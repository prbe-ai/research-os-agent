"""Framework-independent implementation of the six read-only MCP operations."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..sdk import errors
from .source import ResearchOSSource


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
    ) -> dict:
        filters = filters or {}
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
