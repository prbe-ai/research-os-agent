"""Research OS API data-source adapter used by the read-only MCP service."""

from __future__ import annotations

from typing import Any

from ..sdk import errors
from ..sdk.client import Client


class ResearchOSSource:
    """Read authoritative structured data through research-os APIs.

    The source never connects directly to Postgres or R2. The API enforces
    tenancy and returns object-store resource pointers where appropriate.
    """

    def __init__(self, client: Client):
        self.client = client

    def close(self) -> None:
        self.client.close()

    def capabilities(self) -> dict[str, bool]:
        # These false values describe the checked-in API v3. Future capability
        # discovery should replace this static compatibility map.
        return {
            "structured_experiments": True,
            "semantic_search": False,
            "kb_documents": False,
            "versioned_assets": False,
            "portable_snapshots": False,
            "managed_artifact_upload": False,
            "promotion_manifests": False,
        }

    def identity(self) -> dict:
        return self.client.me()

    def projects(self, *, limit: int = 50) -> list[dict]:
        return self.client.list_projects(limit=limit).items

    def experiments(self, *, project_id: str | None = None, limit: int = 100) -> list[dict]:
        return self.client.list_experiments(project_id=project_id, limit=limit).items

    def runs(self, *, experiment_id: str | None = None, limit: int = 100) -> list[dict]:
        return self.client.list_runs(experiment_id=experiment_id, limit=limit).items

    def get(self, ref: str) -> tuple[str, dict]:
        kind, _, value = ref.partition(":")
        if not value:
            value = kind
            kind = ""
        getters = {
            "run": self.client.get_run,
            "experiment": self.client.get_experiment,
            "project": self.client.get_project,
        }
        if kind in getters:
            return kind, getters[kind](value)
        for candidate in ("run", "experiment", "project"):
            try:
                return candidate, getters[candidate](value)
            except errors.NotFoundError:
                continue
        raise errors.NotFoundError(f"no run, experiment, or project matches {ref}")

    def bundle(self, run_id: str) -> dict:
        return self.client.run_bundle(run_id)

    def lineage(self, run_id: str) -> dict:
        return self.client.run_lineage(run_id)

    def resolve_asset(self, **query: Any) -> dict:
        return self.client.assets.resolve(**query)

    def trace_file(self, query: str) -> dict:
        try:
            return self.client.transport.get("/v1/artifacts/trace", params={"q": query})
        except errors.NotFoundError:
            return {
                "query": query,
                "matches": [],
                "state": "partial",
                "missing_capability": "artifact_trace_index",
            }
