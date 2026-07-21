"""Probe Research API data-source adapter used by the read-only MCP service."""

from __future__ import annotations

import time
from typing import Any

from ..sdk import errors
from ..sdk.client import Client
from .contract import Capability, EntityType

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
        # GET /v1/browse support, same tri-state and same reasoning. Tracked
        # SEPARATELY from search: the two endpoints shipped in different
        # releases, so a backend can have one and not the other, and inferring
        # either from the other would report a capability the server lacks.
        self._browse_supported: bool | None = None
        self._browse_checked_at: float = float("-inf")

    def close(self) -> None:
        self.client.close()

    def capabilities(self) -> dict[str, bool]:
        # Search capabilities are discovered against the live backend (a cached
        # probe of POST /v1/search, once per token; an "unsupported" verdict is
        # re-checked after _SUPPORT_RECHECK_SECONDS); the rest still describe
        # the checked-in API contract statically.
        #
        # versioned_assets and managed_artifact_upload were both hardcoded False
        # and both were STALE — the routes are in schema/openapi.json and answer
        # live (/v1/assets{,/{id},/{id}/versions}; /v1/runs/{id}/artifacts/uploads
        # + /v1/artifacts/{id}/download). They were set in one stroke alongside a
        # /v1/search change, never as an assets decision. Because research_context
        # derives `missing` from this map, the two stale flags pinned EVERY context
        # envelope to state="partial" — the same "unconditionally missing" lie the
        # contract/versions/usage views told.
        if self._search_supported is None or (
            self._search_supported is False and self._verdict_expired()
        ):
            self._probe_search()
        supported = self._search_supported is True
        return {
            Capability.STRUCTURED_EXPERIMENTS: True,
            # Reported from the cached verdict only: probing browse here would
            # cost an extra request on every capabilities() call, and every
            # envelope carries one. None (never probed) reports True optimistically
            # -- browse itself raises CapabilityUnavailable if the route is absent,
            # which is a truthful failure rather than a preemptive denial.
            Capability.STRUCTURED_BROWSE: self._browse_supported is not False,
            Capability.UNIFIED_SEARCH: supported,
            Capability.SEMANTIC_SEARCH: supported and self._search_semantic_ok,
            Capability.KB_DOCUMENTS: supported and self._search_semantic_ok,
            Capability.VERSIONED_ASSETS: True,
            # The one honest False: sdk/snapshot.py captures git/env LOCALLY and
            # there is no backend snapshot route to read one back.
            Capability.PORTABLE_SNAPSHOTS: False,
            Capability.MANAGED_ARTIFACT_UPLOAD: True,
        }

    def _verdict_expired(self) -> bool:
        return (time.monotonic() - self._search_checked_at) > _SUPPORT_RECHECK_SECONDS

    def _browse_verdict_expired(self) -> bool:
        return (time.monotonic() - self._browse_checked_at) > _SUPPORT_RECHECK_SECONDS

    def browse(
        self,
        *,
        scope: str | None = None,
        depth: int | None = None,
        status: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict:
        """GET /v1/browse, raising CapabilityUnavailable on a backend without it.

        A pre-browse backend must NOT degrade to an empty tree: "nothing exists
        here" and "this server cannot tell you what exists" are opposite claims,
        and the first one would stop an agent looking further.
        """
        if self._browse_supported is False and not self._browse_verdict_expired():
            raise self._browse_unavailable()
        try:
            response = self.client.browse(
                scope=scope, depth=depth, status=status, limit=limit, cursor=cursor
            )
        except errors.NotFoundError:
            # A scoped 404 means the SCOPE was not found on a backend that has
            # the route; only an unscoped 404 proves the route is missing.
            if scope is not None:
                raise
            self._browse_supported = False
            self._browse_checked_at = time.monotonic()
            raise self._browse_unavailable() from None
        self._browse_supported = True
        self._browse_checked_at = time.monotonic()
        return response

    def _browse_unavailable(self) -> errors.CapabilityUnavailable:
        return errors.CapabilityUnavailable(
            Capability.STRUCTURED_BROWSE,
            "this Probe Research backend predates GET /v1/browse",
        )

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
        project_id: str | None = None,
        top_k: int | None = None,
        exact_limit: int | None = None,
        exact_cursor: str | None = None,
        semantic_cursor: str | None = None,
    ) -> dict:
        """POST /v1/search, raising :class:`errors.CapabilityUnavailable` when the
        backend predates the endpoint (so callers can fall back honestly).

        404 / staleness policy (rolling deploys + oracle-safe SCOPE 404s):
        - a FRESH cached "unsupported" verdict short-circuits here, so a
          pre-search server does not eat a doomed POST per call; the verdict
          expires after ``_SUPPORT_RECHECK_SECONDS`` and is then re-checked.
        - any search 404 re-probes the endpoint itself (invalidating a cached
          True). If the probe finds the endpoint the search is retried ONCE
          first (a stale pod mid-deploy 404s scoped and unscoped alike); only a
          second 404 is attributed to the scope, and then a workspace- or
          project-scoped one surfaces as NotFound because those are oracle-safe
          404s.
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
                    project_id=project_id,
                    top_k=top_k,
                    exact_limit=exact_limit,
                    exact_cursor=exact_cursor,
                    semantic_cursor=semantic_cursor,
                )
            except errors.NotFoundError:
                if retried:
                    # Second 404 with the endpoint already confirmed up. Now the
                    # 404 is about the REQUEST, not the deployment: a scoped one
                    # means that scope is absent (both scopes are oracle-safe
                    # 404s), an unscoped one is a persistent server 404.
                    raise
                # First 404: is the endpoint there at all? Probe ONCE per call --
                # re-probing on the retry too would cost four requests to answer
                # one question.
                self._search_supported = None
                self._probe_search()
                if self._search_supported is None:
                    raise  # probe could not classify; do not cache a verdict
                if self._search_supported is not True:
                    raise self._capability_unavailable() from None
                # Endpoint exists. RETRY BEFORE ATTRIBUTING: a stale pod
                # mid-rolling-deploy 404s scoped and unscoped requests alike, and
                # calling a scoped 404 "that project does not exist" without
                # retrying turns a deploy into a wrong answer about the caller's
                # own data. Project scope is the commoner of the two, so getting
                # this wrong has a wider blast radius than the workspace-only
                # version it replaced.
                retried = True
                continue
            self._record_search_response(response)
            return response

    def asset_versions(self, asset_id: str) -> list[dict]:
        """An asset's versions, newest first."""
        return list(self.client.assets.versions(asset_id))

    def identity(self) -> dict:
        return self.client.me()

    def projects(self, *, limit: int = 50) -> list[dict]:
        return self.client.list_projects(limit=limit).items

    def experiments(self, *, project_id: str | None = None, limit: int = 100) -> list[dict]:
        return self.client.list_experiments(project_id=project_id, limit=limit).items

    def runs(self, *, experiment_id: str | None = None, limit: int = 100) -> list[dict]:
        return self.client.list_runs(experiment_id=experiment_id, limit=limit).items

    def get(self, ref: str) -> tuple[str, dict]:
        """Resolve ``kind:value`` (or a bare id) to ``(kind, entity)``.

        ``group`` is here rather than behind a research_list_groups tool: a sweep is
        an experiment-shaped noun, so it belongs on the same ref seam as the rest.
        """
        kind, _, value = ref.partition(":")
        if not value:
            value = kind
            kind = ""
        getters = {
            EntityType.RUN.value: self.client.get_run,
            EntityType.EXPERIMENT.value: self.client.get_experiment,
            EntityType.PROJECT.value: self.client.get_project,
            EntityType.GROUP.value: self.client.get_group,
        }
        if kind == EntityType.ASSET.value:
            # Assets resolve by NAME, not id -- that is what makes them useful
            # for the reuse check. Kept OUT of the bare-ref fallback below for
            # the same reason: a bare id would then trigger a name lookup on
            # every miss, and a typo would cost a registry round trip before
            # erroring.
            asset = self.client.assets.get_by_name(value)
            if asset is None:
                raise errors.NotFoundError(f"no asset named {value!r}")
            return kind, asset
        if kind in getters:
            return kind, getters[kind](value)
        for candidate in getters:
            try:
                return candidate, getters[candidate](value)
            except errors.NotFoundError:
                continue
        raise errors.NotFoundError(f"no run, experiment, project, or group matches {ref}")

    def bundle(self, run_id: str) -> dict:
        return self.client.run_bundle(run_id)

    def lineage(self, run_id: str) -> dict:
        return self.client.run_lineage(run_id)

    # -- reads the SDK already had, which the MCP simply never surfaced --------

    def run_spans(self, run_id: str, **filters: Any) -> list[dict]:
        """The trajectory itself. The run bundle carries span_type COUNTS only, so
        before this an agent could see that 500 rollouts happened and not one of
        what they did."""
        return self.client.run_spans(run_id, **filters)

    def run_series(self, run_id: str) -> list[dict]:
        return self.client.run_series(run_id)

    def run_metrics(self, run_id: str, **filters: Any) -> list[dict]:
        return self.client.run_metrics(run_id, **filters)

    def run_artifacts(self, run_id: str, **filters: Any) -> list[dict]:
        return self.client.list_run_artifacts(run_id, **filters)

    def experiment_artifacts(self, experiment_id: str) -> list[dict]:
        return self.client.list_experiment_artifacts(experiment_id)

    def run_events(self, run_id: str) -> list[dict]:
        return self.client.events.for_run(run_id)

    def experiment_edges(self, experiment_id: str) -> list[dict]:
        return self.client.experiment_edges(experiment_id)

    def experiment_groups(self, experiment_id: str) -> list[dict]:
        return self.client.list_groups(experiment_id)

    def experiment_versions(self, experiment_id: str) -> list[dict]:
        return self.client.list_experiment_versions(experiment_id)

    def execution_record(self, content_hash: str) -> dict:
        """The pinned environment behind ``run.env_ref`` — code, deps, hardware,
        settings, paths. This is what makes the reproduce view a reproduction."""
        return self.client.get_execution_record(content_hash)

    def experiment(self, experiment_id: str) -> dict:
        return self.client.get_experiment(experiment_id)

    def assets(self, *, limit: int = 50) -> list[dict]:
        """The versioned-asset registry — live since fold #5, and never once read
        by research_context, which returned a hardcoded empty official_assets."""
        return self.client.assets.list(limit=limit).items

    def resolve_asset(self, **query: Any) -> dict:
        return self.client.assets.resolve(**query)
