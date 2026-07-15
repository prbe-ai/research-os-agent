"""Probe Research API data-source adapter used by the read-only MCP service."""

from __future__ import annotations

from typing import Any

from ..sdk import errors
from ..sdk.client import Client


class ResearchOSSource:
    """Read authoritative structured data through Probe Research APIs.

    The source never connects directly to Postgres or R2. The API enforces
    tenancy and returns object-store resource pointers where appropriate.
    """

    # Minimal request used to discover whether the backend ships POST /v1/search.
    _SEARCH_PROBE_QUERY = "capability probe"

    def __init__(self, client: Client):
        self.client = client
        # Tri-state: None = not yet discovered, then cached for the source's
        # lifetime (refreshed by every real search response).
        self._search_supported: bool | None = None
        self._search_semantic_ok: bool = False

    def close(self) -> None:
        self.client.close()

    def capabilities(self) -> dict[str, bool]:
        # Search capabilities are discovered against the live backend (one
        # cached probe of POST /v1/search); the rest still describe the
        # checked-in API contract statically.
        if self._search_supported is None:
            self._probe_search()
        return {
            "structured_experiments": True,
            "unified_search": bool(self._search_supported),
            "semantic_search": bool(self._search_supported) and self._search_semantic_ok,
            "kb_documents": bool(self._search_supported) and self._search_semantic_ok,
            "versioned_assets": False,
            "portable_snapshots": False,
            "managed_artifact_upload": False,
            "promotion_manifests": False,
        }

    def _record_search_response(self, response: dict) -> None:
        self._search_supported = True
        self._search_semantic_ok = (response.get("semantic") or {}).get("error") is None

    def _probe_search(self) -> None:
        """One trivial POST /v1/search to learn whether the backend has the
        one-index search door (404 = a server that predates it)."""
        try:
            response = self.client.search(self._SEARCH_PROBE_QUERY, exact_limit=1, top_k=1)
        except errors.NotFoundError:
            self._search_supported = False
            self._search_semantic_ok = False
        except errors.RosError:
            # Transient (network/5xx/auth): report unavailable for this call but
            # leave the tri-state unset so the next call re-probes.
            return
        else:
            self._record_search_response(response)

    def search(
        self,
        query: str,
        *,
        corpus: list[str] | None = None,
        workspace_id: str | None = None,
        top_k: int | None = None,
        exact_limit: int | None = None,
        exact_cursor: str | None = None,
        semantic_cursor: str | None = None,
    ) -> dict:
        """POST /v1/search, raising :class:`errors.CapabilityUnavailable` when the
        backend predates the endpoint (so callers can fall back honestly)."""
        try:
            response = self.client.search(
                query,
                corpus=corpus,
                workspace_id=workspace_id,
                top_k=top_k,
                exact_limit=exact_limit,
                exact_cursor=exact_cursor,
                semantic_cursor=semantic_cursor,
            )
        except errors.NotFoundError:
            # A 404 is ambiguous when a workspace filter rode along: the contract
            # 404s an unknown/foreign workspace_id (oracle-safe). Disambiguate via
            # the cached/probed endpoint support before deciding.
            if workspace_id is not None:
                if self._search_supported is None:
                    self._probe_search()
                if self._search_supported is not False:
                    raise  # endpoint exists (or unknown) -> surface the 404 as-is
            self._search_supported = False
            self._search_semantic_ok = False
            raise errors.CapabilityUnavailable(
                "unified_search",
                "this Probe Research backend predates POST /v1/search",
            ) from None
        self._record_search_response(response)
        return response

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
