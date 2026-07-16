"""Probe Research API data-source adapter used by the read-only MCP service."""

from __future__ import annotations

import time
from typing import Any

from ..sdk import errors
from ..sdk.client import Client
from .contract import Capability

# A cached "backend has no /v1/search" verdict is re-checked after this long, so
# a server upgrade (or a rolling deploy that briefly 404'd) is picked up without
# restarting the MCP process.
_SUPPORT_RECHECK_SECONDS = 300.0


class ResearchOSSource:
    """Read authoritative structured data through Probe Research APIs.

    The source never connects directly to Postgres or R2. The API enforces
    tenancy and returns object-store resource pointers where appropriate.
    """

    # Minimal request used to discover whether the backend ships POST /v1/search.
    _SEARCH_PROBE_QUERY = "capability probe"

    def __init__(self, client: Client):
        self.client = client
        # POST /v1/search support, discovered against the live backend and
        # cached on this source (the server memoizes one source per token, so
        # the probe fires once per token, not once per tool call). Tri-state:
        # None = unknown, True = supported (refreshed by every real search),
        # False = unsupported (expires after _SUPPORT_RECHECK_SECONDS).
        # Concurrent callers may double-probe; the probe is idempotent and
        # cheap, so no lock is taken here.
        self._search_supported: bool | None = None
        self._search_semantic_ok: bool = False
        self._search_checked_at: float = float("-inf")

    def close(self) -> None:
        self.client.close()

    def capabilities(self) -> dict[str, bool]:
        # Search capabilities are discovered against the live backend (a cached
        # probe of POST /v1/search, once per token; an "unsupported" verdict is
        # re-checked after _SUPPORT_RECHECK_SECONDS); the rest still describe
        # the checked-in API contract statically.
        if self._search_supported is None or (
            self._search_supported is False and self._verdict_expired()
        ):
            self._probe_search()
        supported = self._search_supported is True
        return {
            Capability.STRUCTURED_EXPERIMENTS: True,
            Capability.UNIFIED_SEARCH: supported,
            Capability.SEMANTIC_SEARCH: supported and self._search_semantic_ok,
            Capability.KB_DOCUMENTS: supported and self._search_semantic_ok,
            Capability.VERSIONED_ASSETS: False,
            Capability.PORTABLE_SNAPSHOTS: False,
            Capability.MANAGED_ARTIFACT_UPLOAD: False,
            Capability.PROMOTION_MANIFESTS: False,
        }

    def _verdict_expired(self) -> bool:
        return (time.monotonic() - self._search_checked_at) > _SUPPORT_RECHECK_SECONDS

    def _record_search_response(self, response: Any) -> None:
        self._search_supported = True
        self._search_checked_at = time.monotonic()
        semantic = response.get("semantic") if isinstance(response, dict) else None
        self._search_semantic_ok = isinstance(semantic, dict) and semantic.get("error") is None

    def _mark_search_unsupported(self) -> None:
        self._search_supported = False
        self._search_semantic_ok = False
        self._search_checked_at = time.monotonic()

    def _probe_search(self) -> None:
        """One trivial POST /v1/search to learn whether the backend has the
        one-index search door (404 = a server that predates it)."""
        try:
            response = self.client.search(self._SEARCH_PROBE_QUERY, exact_limit=1, top_k=1)
        except errors.NotFoundError:
            self._mark_search_unsupported()
        except errors.RosError:
            # Transient (network/5xx/auth): report unavailable for this call but
            # leave the tri-state unset so the next call re-probes.
            return
        else:
            self._record_search_response(response)

    def _capability_unavailable(self) -> errors.CapabilityUnavailable:
        return errors.CapabilityUnavailable(
            Capability.UNIFIED_SEARCH,
            "this Probe Research backend predates POST /v1/search",
        )

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
        backend predates the endpoint (so callers can fall back honestly).

        404 / staleness policy (rolling deploys + oracle-safe workspace 404s):
        - a FRESH cached "unsupported" verdict short-circuits here, so a
          pre-search server does not eat a doomed POST per call; the verdict
          expires after ``_SUPPORT_RECHECK_SECONDS`` and is then re-checked.
        - any search 404 re-probes the endpoint itself (invalidating a cached
          True). If the probe finds the endpoint: a workspace-scoped 404 means
          the WORKSPACE was not found (surfaced as NotFound); otherwise the
          search is retried once (we likely hit a stale pod mid-deploy) before
          the 404 is surfaced.
        - if the probe cannot classify (transient error) the original 404 is
          surfaced without caching a verdict, so the next call re-checks.
        """
        if self._search_supported is False and not self._verdict_expired():
            raise self._capability_unavailable()
        retried = False
        while True:
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
                self._search_supported = None
                self._probe_search()
                if self._search_supported is True:
                    if workspace_id is not None:
                        raise  # endpoint exists -> the workspace was not found
                    if not retried:
                        retried = True
                        continue  # likely a stale pod during a rolling deploy
                    raise  # endpoint present but this search persistently 404s
                if self._search_supported is None:
                    raise  # probe could not classify; do not cache a verdict
                raise self._capability_unavailable() from None
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
        # The artifact trace index does not exist server-side: there has never been a
        # `/v1/artifacts/trace` route, so the call this used to make could only ever
        # 404 into the degraded answer below. Returning it directly is the same
        # result without the doomed round-trip. `missing_capability` is the honest
        # signal to the agent that this tool has no backend yet.
        #
        # When the backend does ship the index, tests/test_parity.py will fail with
        # the new route listed as unreachable — that is the prompt to wire it here.
        return {
            "query": query,
            "matches": [],
            "state": "partial",
            "missing_capability": "artifact_trace_index",
        }
