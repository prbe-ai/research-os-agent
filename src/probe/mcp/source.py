"""Probe Research API data-source adapter used by the read-only MCP service."""

from __future__ import annotations

import time
import uuid
from typing import Any

from ..sdk import errors
from ..sdk.client import Anchor, Client
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
        # Lock-free by choice, now under REAL thread concurrency (server._tool
        # offloads tool bodies): writes are GIL-atomic and last-writer-wins.
        # During a mixed-version backend rollout, threads probing different
        # pods can disagree and the losing verdict sticks for up to
        # _SUPPORT_RECHECK_SECONDS — accepted: a lock cannot reconcile pods
        # that genuinely differ, and the verdict self-heals at the recheck.
        # (2026-07-30 review; revisit only if it bites — see TODOS.md.)
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
        # managed_artifact_upload was hardcoded False and STALE — the routes are in
        # schema/openapi.json and answer live (/v1/runs/{id}/artifacts/uploads +
        # /v1/artifacts/{id}/download). It was set in one stroke alongside a
        # /v1/search change, never as a deliberate call. Because research_context
        # derives `missing` from this map, that stale flag pinned EVERY context
        # envelope to state="partial" — the same "unconditionally missing" lie the
        # contract/versions/usage views told. (A `versioned_assets` flag sat here
        # for the same reason; it went with the asset registry itself.)
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
        tags: list[str] | None = None,
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
                scope=scope,
                depth=depth,
                status=status,
                tags=tags,
                limit=limit,
                cursor=cursor,
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

    def identity(self) -> dict:
        return self.client.me()

    def projects(self, *, limit: int = 50) -> list[dict]:
        return self.client.list_projects(limit=limit).items

    def experiments(self, *, project_id: str | None = None, limit: int = 100) -> list[dict]:
        return self.client.list_experiments(project_id=project_id, limit=limit).items

    def runs(
        self,
        *,
        experiment_id: str | None = None,
        project_id: str | None = None,
        direct: bool = False,
        limit: int = 100,
    ) -> list[dict]:
        """``project_id`` lists ALL the project's runs — project-direct and
        experiment-attached (research-os 0054); ``direct=True`` narrows to
        experiment-less runs server-side."""
        return self.client.list_runs(
            experiment_id=experiment_id,
            project_id=project_id,
            direct=direct,
            limit=limit,
        ).items

    def artifact_ref(self, value: str) -> dict:
        """Resolve ``artifact:<value>`` where value is a NAME or an ID.

        The two axes answer DIFFERENT questions, and conflating them is what #135
        got wrong in the other direction:

        * a NAME asks "does an official X already exist" -- the reuse check. It is
          scoped to SHARED and stays that way (:meth:`artifact_by_name`).
        * an ID is an identity the caller ALREADY holds (``search_knowledge``
          returns artifact hits carrying an id and ``resource: null``). It is not a
          reuse question at all, so the shared scoping does not apply to it.

        #135 answered an id with a 422 saying ids are unresolvable because no
        ``GET /v1/artifacts/{id}`` route exists. The premise was wrong: no ENTITY
        route exists, but ``GET /v1/artifacts/{id}/versions`` takes a raw id and
        already backs the versions view, so an id is resolvable today.
        """
        try:
            uuid.UUID(value)
        except ValueError:
            return self.artifact_by_name(value)
        return self.artifact_by_id(value)

    def artifact_by_id(self, artifact_id: str) -> dict:
        """Resolve an artifact ID, without widening the reuse check's scope.

        DELIBERATE SCOPE DECISION -- an id resolves for EVERY artifact, but only a
        SHARED one resolves to a full card:

        * ``view=versions`` works for any artifact whatever its anchor, because
          ``GET /v1/artifacts/{id}/versions`` is an unscoped by-id route. A caller
          holding an id gets the version chain even for a run-anchored artifact.
        * a full CARD needs the artifact's ROW (name, metadata), and the only list
          that yields one is ``GET /v1/shared/files``. So a shared id gets the same
          card a name does; a non-shared id resolves to an id-only identity that
          says where the rest lives.

        This does NOT widen the reuse check. That check is ``artifact:<name>`` and
        is still SHARED-only: "is there an official X" must not be answered off a
        run-anchored copy. Resolving an id the caller already holds answers a
        different question and licences nothing.

        A not-found here is authoritative in a way the name path's never is: ids
        are global and unscoped, so "no artifact with this id" rules out every
        anchor at once, where "no artifact named X" only ever ruled out SHARED.
        """
        # Shared FIRST, for the card. Costs one unbounded list (same read the name
        # path already does) and is the only source of a full row.
        rows = self.client.list_anchored(Anchor.SHARED) or []
        for row in rows:
            if str(row.get("id")) == artifact_id:
                return row
        # Not shared. The artifact almost certainly still EXISTS -- run-, experiment-
        # and project-anchored artifacts never appear in the shared list -- so
        # answering not-found here would be the #135 inversion wearing new clothes:
        # get_entity's contract reads an error as "a new identity is licensed".
        # Prove existence off the unscoped by-id route instead of guessing.
        self.artifact_versions(artifact_id)  # 404s iff the id is genuinely unknown
        return {
            "id": artifact_id,
            "shared": False,
            # Carried so the card says why it is thin rather than looking truncated.
            "resolution_note": (
                f"artifact {artifact_id} exists but is not shared, so only its id "
                f"and version chain are readable here (there is no "
                f"GET /v1/artifacts/{{id}} entity route, and only "
                f"GET /v1/shared/files yields a full row). view=versions is "
                f"complete; for name and metadata read the owning container with "
                f"run:<id> / experiment:<id> view=artifacts, or promote it with "
                f"`probe shared share`. This is NOT absence and licences no new "
                f"identity."
            ),
        }

    def artifact_by_name(self, name: str) -> dict:
        """Resolve an artifact NAME to the artifact, at the shared (lab-wide) level.

        This is the reuse-before-you-create seam, and it is by NAME because the
        question is "does an official X already exist" -- you have the name, not an
        id. There is no ``GET /v1/artifacts/{id}`` read route at all, so name is not
        merely the convenient axis here, it is the only one.

        SHARED, not a container, on purpose: the retired asset registry enforced
        tenant-unique names, and a shared-level artifact is what that identity
        became (#143 folded every asset into an artifact keeping its id). Scoping
        this to a project would answer a narrower question than the one the caller
        asked and would licence a duplicate that already exists one level up.
        """
        # `prefix` is a FOLDER filter, NOT a name filter: `path` is a GENERATED
        # column holding the DIRNAME of `name` (research-os 0029), and the clause
        # is `path = $1 OR path LIKE '$1/%'`. Passing the whole name would match
        # nothing for a root-level file, whose path is '' -- and "nothing" here
        # reads as "no such artifact", i.e. licence to create the duplicate this
        # check exists to prevent. So narrow by the name's DIRECTORY and match the
        # name ourselves. A root-level name has no directory, and the whole shared
        # scope is then the honest search space.
        # An id never reaches here: `artifact_ref` routes UUID-shaped values to
        # `artifact_by_id`. #135 raised a 422 at this point instead, on the premise
        # that ids were unresolvable -- they are not, and refusing one sent agents
        # back to a name they may not have. The invariant that 422 protected still
        # holds, just earlier: an id is never answered as an absent NAME.
        folder = name.rsplit("/", 1)[0] if "/" in name else ""
        params = {"prefix": folder} if folder else {}
        # NO `limit`, deliberately. A cap would make an artifact past the cap read
        # as absent, and absent is defined downstream as licence to create a new
        # identity -- reintroducing the precise bug the retired asset registry
        # had (assets.resolve() read one default-limit page, so asset 51+ resolved
        # to no_match and callers were told to register a duplicate). An unbounded
        # read of one curated team folder is the cheaper failure. A server-side
        # name filter would fix both; /v1/shared/files has no such parameter yet.
        rows = self.client.list_anchored(Anchor.SHARED, **params) or []
        exact = [row for row in rows if row.get("name") == name]
        if not exact:
            # States what was SEARCHED, not what exists. This lookup covers live
            # COMPLETE artifacts at the SHARED level only -- a run/experiment/
            # project-anchored artifact of the same name is real and invisible
            # here, and so is an upload still pending. "Nothing exists" would
            # overstate all of that, and downstream this message is read as
            # licence to create a new identity, so the overstatement is the
            # expensive direction to be wrong in.
            raise errors.NotFoundError(
                f"no completed artifact named {name!r} at the SHARED level, which "
                f"is the only scope this check covers. That is the answer to "
                f"'does an OFFICIAL one exist' -- it does not rule out a copy "
                f"anchored to a run, experiment or project, or an upload still "
                f"pending. Check a container with run:<id> / experiment:<id> "
                f"view=artifacts; promote a workspace file with `probe shared share`."
            )
        if len(exact) > 1:
            # Loud, and deliberately NOT a not-found: an agent reads not-found as
            # "a new identity is licensed" and would create a THIRD copy of a name
            # that is already duplicated. Naming the ids is the only answer that
            # leads to a merge instead.
            ids = ", ".join(sorted(str(row.get("id")) for row in exact))
            raise errors.ValidationError(
                f"{len(exact)} shared artifacts are named {name!r} (ids: {ids}); "
                f"this name is already duplicated -- reconcile them rather than "
                f"pinning either blindly",
                status=422,
            )
        return exact[0]

    def get(self, ref: str) -> tuple[str, dict]:
        """Resolve ``kind:value`` (or a bare id) to ``(kind, entity)``.

        ``group`` is here rather than behind a research_list_groups tool: a sweep is
        an experiment-shaped noun, so it belongs on the same ref seam as the rest.
        ``artifact`` is reached by name OR id (see :meth:`artifact_ref`).
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
        if kind == EntityType.ARTIFACT.value:
            return kind, self.artifact_ref(value)
        if kind in getters:
            return kind, getters[kind](value)
        if kind:
            # An unknown kind is REJECTED, never guessed at. `asset:<name>` used to
            # land here and fall into the loop below, where get_experiment() raised
            # a raw 422 uuid_parsing on a non-UUID value -- so the agent that
            # followed the retired instruction to the letter got a Postgres-shaped
            # parse error naming `experiment_id`, and nothing that said "assets are
            # gone". Any ref kind retired later would leak the same way.
            known = sorted([*getters, EntityType.ARTIFACT.value])
            raise errors.ValidationError(
                f"unknown ref kind {kind!r} in {ref!r}; supported kinds are {known} "
                f"(assets were folded into artifacts: use artifact:<name>)",
                status=422,
            )
        # A bare ref is only ever an id, and every id route validates it as a UUID.
        # Checking the SHAPE here rather than catching the backend's 422 keeps a
        # REAL 422 -- schema drift, a backend invariant -- propagating instead of
        # being rewritten into "nothing matches this ref". Catching every
        # ValidationError would swallow those, which is the same class of mistake
        # as the leak this replaced, only quieter.
        try:
            uuid.UUID(value)
        except ValueError:
            raise errors.NotFoundError(
                f"no run, experiment, project, or group matches {ref!r}: "
                f"a bare ref must be a UUID id (names are reached as artifact:<name>)"
            ) from None
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

    def run_edges(self, run_id: str) -> list[dict]:
        """Lineage EDGES touching this run (artifact / asset-version provenance).

        A different relation from `lineage()`, which walks `parent_run_id` and
        answers "which run was this forked from". Both are needed to answer
        "where did this run's data come from", and only one of them was ever
        surfaced -- which is why the lineage view reported empty on runs that
        demonstrably consumed a dataset and produced artifacts."""
        return self.client.run_edges(run_id)

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

    def run_metrics_grouped(self, run_id: str, key: str, **kw: Any) -> dict:
        """Server-side reduce/group (0059/0062). The client follows `next_step`
        paging; the service bounds the total through `max_rows`."""
        return self.client.get_metrics_grouped(run_id, key, **kw)

    def run_coordinates(self, run_id: str) -> list[dict]:
        return self.client.list_run_coordinates(run_id)

    def export_points(self, run_id: str, **kw: Any):
        """The lossless raw-point generator; the service slices one bounded page
        off it and hands the keyset cursor back to the agent."""
        return self.client.export_metric_points(run_id, **kw)

    def run_artifacts(self, run_id: str, **filters: Any) -> list[dict]:
        return self.client.list_run_artifacts(run_id, **filters)

    def experiment_artifacts(self, experiment_id: str) -> list[dict]:
        return self.client.list_experiment_artifacts(experiment_id)

    def project_artifacts(self, project_id: str) -> list[dict]:
        return self.client.list_project_artifacts(project_id)

    def notes(
        self,
        entity_kind: str,
        entity_id: str,
        *,
        kind: str | None = None,
        include_superseded: bool = False,
    ) -> list[dict]:
        """The anchor's research notes, supersession resolved by the SDK.

        `entity_kind` is the MCP entity type ("run"/"experiment"/"project"), which is
        also the anchor's wire name, so it passes straight through. Resolution stays
        in ``client.notes.list`` rather than being re-implemented here: the CLI reads
        the same method, and a second copy is how the two surfaces would drift into
        disagreeing about which decision is current.
        """
        return self.client.notes.list(
            entity_id,
            anchor=entity_kind,
            kind=kind,
            include_superseded=include_superseded,
        )

    def artifact_versions(self, artifact_id: str) -> list[dict]:
        """The artifact's version chain, newest first — what the reuse check reads
        once :meth:`artifact_by_name` has turned a name into an identity."""
        return self.client.list_artifact_versions(artifact_id)

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

