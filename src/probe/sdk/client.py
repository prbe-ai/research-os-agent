"""The Probe Research SDK client core.

Two write paths, one core (per the SDK/CLI primitives sketch):
  * granular ``/v1`` calls for interactive / agent-driven capture (Anthrogen);
  * one-shot idempotent ``/ingest`` push for install-once passive capture (Osmosis).

Every method maps onto a real v4 endpoint (Probe Research v0.4.0.0 ingestion fold-in).
"""

from __future__ import annotations

import os
import sys
import warnings
from enum import Enum
from pathlib import Path
from typing import Any

from . import defaults, errors
from ..models import (
    EdgeCreate,
    ExecutionRecordCreate,
    ExperimentVersionMint,
    IngestRunRequest,
    RunGcRequest,
    RunGroupCreate,
    RunGroupPatch,
    ScopedUploadRequest,
    UploadGcRequest,
    UploadRequest,
)
from .config import Settings, resolve
from .hashing import fingerprint
from .spool import Spool
from .transport import Page, Transport


class Anchor(str, Enum):
    """What an artifact hangs off.

    The database CHECKs that exactly one anchor is set, so this is a closed
    vocabulary, not a hint. Four of these are *artifacts*; workspace and shared are
    *files*, which is a different noun on the wire (see :meth:`Client.upload_file`).
    """

    RUN = "run"
    EXPERIMENT = "experiment"
    PROJECT = "project"
    WORKSPACE = "workspace"
    SHARED = "shared"


#: Anchors whose upload body is a ``ScopedUploadRequest``. That model is declared
#: ``extra="forbid"``, so a run-only field (``kind``, ``meta``, ``span_id``,
#: ``step_index``) sent to one of these is a 422 — silently ignored is NOT what
#: happens, which is why the client rejects it up front with a readable message.
_SCOPED_ANCHORS = frozenset(
    {Anchor.EXPERIMENT, Anchor.PROJECT, Anchor.WORKSPACE, Anchor.SHARED}
)

#: Anchors addressed as "files" rather than "artifacts": their identity is
#: (anchor, name) rather than (anchor, name, content_hash), so re-uploading a name
#: REPLACES it via a confirm-time swap instead of adding a second version. They also
#: have no metadata-only form — a file is its bytes.
_FILE_ANCHORS = frozenset({Anchor.WORKSPACE, Anchor.SHARED})


class Client:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        token: str | None = None,
        ingest_token: str | None = None,
        hmac_secret: str | None = None,
        settings: Settings | None = None,
        transport: Transport | None = None,
        fail_open: bool = True,
        spool: Spool | None = None,
    ):
        self.settings = settings or resolve(
            base_url=base_url,
            token=token,
            ingest_token=ingest_token,
            hmac_secret=hmac_secret,
        )
        self.transport = transport or Transport(self.settings)
        self.fail_open = fail_open
        self.spool = spool or Spool()
        self._sessions = None
        self._events = None
        self._notes = None
        self._assets = None

    # -- lifecycle ----------------------------------------------------------
    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- fail-open write ----------------------------------------------------
    def write(self, method: str, path: str, body: dict | None = None, *, strict: bool | None = None):
        """A data write that spools on failure unless ``strict`` (or ``fail_open``
        is off). Returns the parsed response, or None if it was spooled."""
        strict = (not self.fail_open) if strict is None else strict
        try:
            resp = self.transport.request(method, path, json_body=body)
            return resp.json() if resp.content else None
        except errors.RosError:
            if strict:
                raise
            self.spool.append(method, path, body)
            return None

    def flush(self) -> int:
        return self.spool.flush(self.transport)

    # -- identity / auth ----------------------------------------------------
    def ensure_authenticated(self, *, interactive: bool | None = None) -> bool:
        """Make sure a user token exists, minting one via the browser device flow
        when a human can approve it.

        The interactive path runs only when stdin+stderr are TTYs and
        ``PROBE_AUTO_LOGIN`` is not ``0`` (or when ``interactive=True`` forces it).
        On success the token is persisted to the same config file ``probe login``
        writes, so the browser round-trip happens once per machine. Returns True
        when a token is available; False leaves the transport to raise its normal
        ``AuthError`` on first use (the crisp headless/CI behavior)."""
        if self.settings.token:
            return True
        if interactive is None:
            interactive = (
                os.environ.get("PROBE_AUTO_LOGIN", "1") != "0"
                and sys.stdin.isatty()
                and sys.stderr.isatty()
            )
        if not interactive:
            return False
        from .config import save_context
        from .device import DeviceLoginError, device_login

        print(
            f"no Probe token found — opening {self.settings.base_url} for browser approval…",
            file=sys.stderr,
        )

        def _show(prompt) -> None:
            print(f"  visit: {prompt.verification_uri_complete}", file=sys.stderr)
            print(f"  code:  {prompt.user_code}", file=sys.stderr)

        try:
            token = device_login(self.settings.base_url, on_prompt=_show)
        except DeviceLoginError as exc:
            warnings.warn(f"automatic device login failed: {exc}", stacklevel=2)
            return False
        save_context({"base_url": self.settings.base_url, "token": token})
        # Settings is shared with the transport; mutating it authenticates both.
        self.settings.token = token
        print("logged in — token saved for future runs", file=sys.stderr)
        return True

    def me(self) -> dict:
        # /v1/me (not the session-only /auth/me): resolves through the unified
        # door, so a `probe_pat` or OAuth token identifies its own tenant/role.
        return self.transport.get("/v1/me")

    def logout(self) -> None:
        """Revoke the calling token (CLI logout)."""
        self.transport.delete("/v1/tokens/current")

    # -- tokens -------------------------------------------------------------
    def list_tokens(self) -> list[dict]:
        """My live (unrevoked) tokens. Secrets are never returned — only
        ``token_prefix``, which is what a human matches against."""
        return self.transport.get("/v1/tokens")

    def create_token(
        self,
        name: str,
        *,
        scopes: list[str] | None = None,
        open_browser: bool = True,
        on_prompt=None,
    ) -> dict:
        """Mint a named token through the browser device flow.

        NOT ``POST /v1/tokens``: that route is session-only by design, so it 403s
        for a token-authenticated CLI. The device flow reaches the same minter with
        a human approving in the browser, which is what the invariant "a leaked
        token must not be able to mint more tokens" is protecting.

        Returns ``TokenCreated``; ``["token"]`` is the plaintext secret and this is
        the only time it exists. Callers must show it once and never persist it.
        """
        from .device import device_authorize

        return device_authorize(
            self.settings.base_url,
            scopes=scopes,
            token_name=name,
            open_browser=open_browser,
            on_prompt=on_prompt,
        )

    def revoke_token(self, token_id: str) -> None:
        """Revoke a token by id. Your own: any writer. A teammate's: needs a
        browser session AND owner/admin, so it 403s from the CLI (by design)."""
        self.transport.delete(f"/v1/tokens/{token_id}")

    # -- workspaces ---------------------------------------------------------
    def list_workspaces(self) -> list[dict]:
        """Every workspace I can see, as a plain list.

        Deliberately NOT paginated: a workspace is one person's folder and there is
        exactly one per team member, so the result is bounded by team size. The server
        offers no cursor — adding one here would invent a contract.

        Server order is caller's-own first, then every other member's, alphabetical.
        Preserved as returned, since "mine first" is the useful default for a picker.
        """
        return self.transport.get("/v1/workspaces")

    def get_workspace(self, workspace_id: str) -> dict:
        return self.transport.get(f"/v1/workspaces/{workspace_id}")

    def rename_workspace(self, workspace_id: str, name: str) -> dict:
        """PATCH /v1/workspaces/{id}. ``name`` is the only user-editable field —
        slug and ownership are server-managed identity."""
        return self.transport.patch(f"/v1/workspaces/{workspace_id}", {"name": name})

    # -- projects -----------------------------------------------------------
    def create_project(
        self,
        slug: str,
        name: str | None = None,
        *,
        workspace_id: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """Create a project. Raises ``ConflictError`` if the slug is taken —
        use :meth:`ensure_project` for get-or-create."""
        body: dict[str, Any] = {"slug": slug, "name": name or slug}
        if workspace_id is not None:
            body["workspace_id"] = workspace_id
        if description is not None:
            body["description"] = description
        if metadata is not None:
            body["metadata"] = metadata
        return self.transport.post("/v1/projects", body)

    def ensure_project(self, slug: str, name: str | None = None, **kw) -> dict:
        try:
            return self.transport.post(
                "/v1/projects", {"slug": slug, "name": name or slug, **kw}
            )
        except errors.ConflictError as exc:
            if exc.existing_id:
                return self.transport.get(f"/v1/projects/{exc.existing_id}")
            raise

    def get_project(self, project_id: str) -> dict:
        return self.transport.get(f"/v1/projects/{project_id}")

    def list_projects(self, *, workspace_id: str | None = None, **params) -> Page:
        query = dict(params)
        if workspace_id is not None:
            query["workspace_id"] = workspace_id
        return self.transport.get_page("/v1/projects", params=query or None)

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
    ) -> dict:
        """PATCH /v1/projects/{id} for display fields only.

        Re-filing into another workspace is :meth:`move_project`, not a keyword here.
        Same route, but splitting the verbs keeps a reindex fan-out (see move_project)
        from being something you can trigger by mistyping an update.
        """
        body = {
            key: value
            for key, value in {
                "name": name,
                "description": description,
                "metadata": metadata,
            }.items()
            if value is not None
        }
        if not body:
            raise ValueError("update_project needs at least one field to set")
        return self.transport.patch(f"/v1/projects/{project_id}", body)

    def move_project(self, project_id: str, workspace_id: str) -> dict:
        """Re-file a project into another workspace.

        PATCH is the only backend door, but this is a much heavier operation than the
        verb suggests: when the workspace actually changes, the server reindexes every
        live descendant experiment and terminal run in the same transaction, because
        those documents denormalize ``workspace_id``. A no-op move (same workspace)
        skips the fan-out entirely.

        An unknown workspace is a 422, not a 404 — it is a rejected *value*, not a
        missing resource.
        """
        return self.transport.patch(
            f"/v1/projects/{project_id}", {"workspace_id": workspace_id}
        )

    def archive_project(self, project_id: str) -> dict:
        """Hide a project without destroying it. The tenant's ``default`` project
        cannot be archived (409) — it is the fallback every run needs."""
        return self.transport.post(f"/v1/projects/{project_id}/archive", None)

    def restore_project(self, project_id: str) -> dict:
        return self.transport.post(f"/v1/projects/{project_id}/restore", None)

    # -- anchored artifacts / files -----------------------------------------
    # Every route below is written as its own literal call site rather than looked up
    # in a table. That is deliberate: the contract-parity guard resolves paths from the
    # AST, and a path built by `.format()` or a dict lookup is invisible to it — the
    # routes would read as unreachable and the guard would stop guarding them.

    def _presign_anchored(self, anchor: Anchor, anchor_id: str | None, body: dict) -> dict:
        if anchor is Anchor.RUN:
            return self.transport.post(f"/v1/runs/{anchor_id}/artifacts/uploads", body)
        if anchor is Anchor.EXPERIMENT:
            return self.transport.post(f"/v1/experiments/{anchor_id}/artifacts/uploads", body)
        if anchor is Anchor.PROJECT:
            return self.transport.post(f"/v1/projects/{anchor_id}/artifacts/uploads", body)
        if anchor is Anchor.WORKSPACE:
            return self.transport.post(f"/v1/workspaces/{anchor_id}/files/uploads", body)
        return self.transport.post("/v1/shared/files/uploads", body)

    def list_anchored(self, anchor: Anchor, anchor_id: str | None = None, **params) -> Any:
        """List the artifacts/files under one anchor."""
        query = params or None
        if anchor is Anchor.RUN:
            return self.transport.get(f"/v1/runs/{anchor_id}/artifacts", params=query)
        if anchor is Anchor.EXPERIMENT:
            return self.transport.get(f"/v1/experiments/{anchor_id}/artifacts", params=query)
        if anchor is Anchor.PROJECT:
            return self.transport.get(f"/v1/projects/{anchor_id}/artifacts", params=query)
        if anchor is Anchor.WORKSPACE:
            return self.transport.get(f"/v1/workspaces/{anchor_id}/files", params=query)
        return self.transport.get("/v1/shared/files", params=query)

    def create_anchored_reference(
        self, anchor: Anchor, anchor_id: str, body: dict
    ) -> dict:
        """Record a metadata-only (reference) artifact — no bytes uploaded.

        Only the three *artifact* anchors have this door. Workspace and shared are
        file anchors: a file is its bytes, so there is no reference-without-bytes form
        of one, and the backend declares no such route.
        """
        if anchor is Anchor.RUN:
            return self.transport.post(f"/v1/runs/{anchor_id}/artifacts", body)
        if anchor is Anchor.EXPERIMENT:
            return self.transport.post(f"/v1/experiments/{anchor_id}/artifacts", body)
        if anchor is Anchor.PROJECT:
            return self.transport.post(f"/v1/projects/{anchor_id}/artifacts", body)
        raise ValueError(
            f"{anchor.value} is a file anchor — a file has no metadata-only form; "
            "upload bytes with upload_file() instead"
        )

    def upload_file(
        self,
        anchor: Anchor,
        anchor_id: str | None,
        name: str,
        path: str,
        *,
        content_type: str | None = None,
        kind: str | None = None,
        meta: dict | None = None,
        span_id: str | None = None,
        step_index: int | None = None,
    ) -> dict:
        """Upload a local file to any anchor: fingerprint -> presign -> PUT -> confirm.

        ``kind``/``meta``/``span_id``/``step_index`` are run-only. Passing them with a
        non-run anchor raises here rather than letting the server 422, because
        ``ScopedUploadRequest`` forbids extras and the resulting error does not say
        which field was the problem.

        Strict by design — no fail-open reference fallback. The fallback exists on
        :meth:`Run.log_artifact` so a training loop is never blocked by a flaky
        upload; an operator running ``probe artifact add`` wants to be told it failed.
        """
        anchor = Anchor(anchor)
        run_only = {
            "kind": kind,
            "meta": meta,
            "span_id": span_id,
            "step_index": step_index,
        }
        if anchor in _SCOPED_ANCHORS:
            offending = sorted(k for k, v in run_only.items() if v is not None)
            if offending:
                raise ValueError(
                    f"{', '.join(offending)} {'is' if len(offending) == 1 else 'are'} "
                    f"only accepted on a run anchor; the {anchor.value} upload contract "
                    "rejects extra fields (422)"
                )
        if anchor is not Anchor.SHARED and not anchor_id:
            raise ValueError(f"a {anchor.value} anchor needs an id")

        digest, size = fingerprint(path)
        if anchor in _SCOPED_ANCHORS:
            req = ScopedUploadRequest(
                name=name, content_hash=digest, size_bytes=size, content_type=content_type
            )
        else:
            req = UploadRequest(
                name=name,
                content_hash=digest,
                size_bytes=size,
                content_type=content_type,
                span_id=span_id,
                step_index=step_index,
                kind=kind,
                meta=meta or None,
            )
        presign = self._presign_anchored(
            anchor, anchor_id, req.model_dump(mode="json", exclude_none=True)
        )
        # `have` means the server already holds these bytes (content-addressed dedup),
        # so there is nothing to PUT. For a file anchor the swap to live also already
        # happened, in its own transaction.
        if not presign.get("have"):
            with open(path, "rb") as handle:
                data = handle.read()
            self.transport.put_url(
                presign["upload_url"],
                data,
                content_type=content_type or "application/octet-stream",
                headers=presign.get("upload_headers") or presign.get("headers"),
            )
        # Confirmed unconditionally, including on the `have` path: the server's confirm
        # returns an already-complete row unchanged (uploads_router.py `_confirm_pending_row`
        # is explicitly idempotent), so this costs one call and buys a single uniform
        # return shape — the stored artifact — across every anchor.
        try:
            return self.transport.post(
                f"/v1/artifacts/{presign['artifact_id']}/confirm", None
            )
        except errors.NotFoundError:
            if not presign.get("have"):
                raise
            # `have` means the bytes were already stored and, for a file anchor, already
            # swapped live. A concurrent replace of the same (anchor, name) can then
            # soft-delete this row before the confirm reads it. The upload succeeded;
            # failing here would report a phantom error for work the server did.
            #
            # Return an artifact-shaped row, NOT the presign: the presign carries
            # `upload_url` (a signed, bearer-equivalent write capability) and callers
            # print this — `probe shared add` sends it straight to stdout, where it
            # would land in CI logs. It also has no `id`/`status`, so every caller
            # relying on the documented uniform return shape would KeyError on exactly
            # this race.
            return {
                "id": presign["artifact_id"],
                "name": name,
                "content_hash": digest,
                "size_bytes": size,
                "status": "complete",
                "superseded": True,
            }

    # -- shared folder ------------------------------------------------------
    def share_workspace_file(self, artifact_id: str, *, replace: bool = False) -> dict:
        """Move a workspace file into the team's Shared folder.

        A MOVE, not a copy: ownership transfers and the search index is re-keyed in the
        same transaction, so the file leaves your workspace listing when it lands in
        Shared.

        A name collision in the destination is a 409 by default — the server never
        auto-supersedes someone else's file. ``replace=True`` atomically supersedes
        the prior one, which has to be asked for explicitly.
        """
        return self.transport.request(
            "POST",
            f"/v1/workspace-files/{artifact_id}/share",
            params={"replace": replace} if replace else None,
        ).json()

    def unshare_file(self, artifact_id: str, *, replace: bool = False) -> dict:
        """Move a Shared file back into the caller's personal workspace.

        Same collision rule as :meth:`share_workspace_file`, in the other direction.
        """
        return self.transport.request(
            "POST",
            f"/v1/shared/files/{artifact_id}/unshare",
            params={"replace": replace} if replace else None,
        ).json()

    def download_shared_file(self, artifact_id: str) -> dict:
        """Presigned download URL for a Shared file."""
        return self.transport.get(f"/v1/shared/files/{artifact_id}/download")

    def delete_shared_file(self, artifact_id: str) -> None:
        self.transport.delete(f"/v1/shared/files/{artifact_id}")

    def confirm_shared_file(self, artifact_id: str) -> dict:
        """The Shared folder's own confirm door. Equivalent to the generic
        ``/v1/artifacts/{id}/confirm``; both delegate to the same core."""
        return self.transport.post(f"/v1/shared/files/{artifact_id}/confirm", None)

    # -- experiments --------------------------------------------------------
    def ensure_experiment(
        self,
        slug: str,
        name: str,
        hypothesis: str | None = None,
        *,
        project_id: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
    ) -> dict:
        """Get-or-create. A create requires a hypothesis (422); an existing
        experiment keeps its own (first-write-wins), so re-running is safe.

        ``hypothesis=None`` composes a marked ``[auto]`` placeholder from ambient
        context (repo@branch, script, coding-agent session) — replace it later
        with :meth:`update_experiment`. It only ever lands on a brand-new
        experiment; an existing one is never overwritten by the fallback."""
        if hypothesis is None:
            hypothesis = defaults.auto_hypothesis(slug)
        body: dict[str, Any] = {"slug": slug, "name": name, "hypothesis": hypothesis}
        if project_id:
            body["project_id"] = project_id
        if description is not None:
            body["description"] = description
        if tags is not None:
            body["tags"] = tags
        try:
            return self.transport.post("/v1/experiments", body)
        except errors.ConflictError as exc:
            if exc.existing_id:
                return self.transport.get(f"/v1/experiments/{exc.existing_id}")
            raise

    def get_experiment(self, experiment_id: str) -> dict:
        return self.transport.get(f"/v1/experiments/{experiment_id}")

    def update_experiment(
        self,
        experiment_id: str,
        *,
        hypothesis: str | None = None,
        name: str | None = None,
        description: str | None = None,
        metadata: dict | None = None,
        summary: dict | None = None,
    ) -> dict:
        """PATCH /v1/experiments/{id} — e.g. replace an ``[auto]`` hypothesis with
        the real one once the experiment's intent is settled."""
        body = {
            key: value
            for key, value in {
                "hypothesis": hypothesis,
                "name": name,
                "description": description,
                "metadata": metadata,
                "summary": summary,
            }.items()
            if value is not None
        }
        if not body:
            raise ValueError("update_experiment needs at least one field to set")
        return self.transport.patch(f"/v1/experiments/{experiment_id}", body)

    def list_experiments(self, *, project_id: str | None = None, **params) -> Page:
        query = dict(params)
        if project_id is not None:
            query["project_id"] = project_id
        return self.transport.get_page("/v1/experiments", params=query or None)

    def archive_experiment(self, experiment_id: str) -> dict:
        """Hide an experiment without destroying it. Idempotent: re-archiving keeps
        the original archive time."""
        return self.transport.post(f"/v1/experiments/{experiment_id}/archive", None)

    def restore_experiment(self, experiment_id: str) -> dict:
        """Un-archive an experiment."""
        return self.transport.post(f"/v1/experiments/{experiment_id}/restore", None)

    def experiment_edges(self, experiment_id: str) -> list[dict]:
        """Every lineage edge under an experiment (the run-level view is
        :meth:`run_edges`)."""
        return self.transport.get(f"/v1/experiments/{experiment_id}/edges")

    # -- run groups (sweeps / ensembles) ------------------------------------
    def create_group(
        self,
        experiment_id: str,
        name: str,
        *,
        kind: str = "group",
        spec: dict | None = None,
    ) -> dict:
        """Create a run group under an experiment — coordination metadata for a
        sweep or ensemble; ``spec`` holds e.g. the search space.

        Pass the returned ``id`` to :meth:`create_run` as ``group_id`` to file a run
        under it. 409 if the name is taken within the experiment."""
        model = RunGroupCreate(name=name, kind=kind, spec=spec or {})
        return self.transport.post(
            f"/v1/experiments/{experiment_id}/groups",
            model.model_dump(mode="json", exclude_none=True),
        )

    def list_groups(self, experiment_id: str) -> list[dict]:
        return self.transport.get(f"/v1/experiments/{experiment_id}/groups")

    def get_group(self, group_id: str) -> dict:
        return self.transport.get(f"/v1/groups/{group_id}")

    def update_group(
        self, group_id: str, *, name: str | None = None, spec: dict | None = None
    ) -> dict:
        """Field-replace PATCH: only the fields you pass change."""
        model = RunGroupPatch(name=name, spec=spec)
        body = model.model_dump(mode="json", exclude_none=True)
        if not body:
            raise ValueError("update_group needs at least one of name/spec")
        return self.transport.patch(f"/v1/groups/{group_id}", body)

    # -- runs (create) ------------------------------------------------------
    def create_run(
        self,
        experiment_id: str,
        name: str,
        *,
        source: str = "api",
        external_id: str | None = None,
        parent_run_id: str | None = None,
        parent_relation: str | None = None,
        group_id: str | None = None,
        config: dict | None = None,
        tags: list[str] | None = None,
        metadata: dict | None = None,
    ) -> "Run":
        body: dict[str, Any] = {"name": name, "source": source}
        if external_id is not None:
            body["external_id"] = external_id
        if parent_run_id is not None:
            body["parent_run_id"] = parent_run_id
            body["parent_relation"] = parent_relation or "fork"
        if group_id is not None:
            body["group_id"] = group_id
        if config is not None:
            body["config"] = config
        if tags is not None:
            body["tags"] = tags
        if metadata is not None:
            body["metadata"] = metadata
        data = self.transport.post(f"/v1/experiments/{experiment_id}/runs", body)
        return Run(self, data)

    def run(
        self,
        *,
        experiment: str | None = None,
        hypothesis: str | None = None,
        name: str | None = None,
        project: str | None = None,
        experiment_name: str | None = None,
        **run_kw,
    ) -> "Run":
        """High-level: ensure the experiment (and project) exist, then open a run.
        This is the ``/experiment`` launch path.

        Every identity argument now has an opinionated default so
        ``client.run()`` alone works: no token triggers the one-time browser
        device login (TTY only), ``experiment`` falls back to the git repo /
        script name, ``name`` to a timestamp (the backend also mints a petname
        ``short_id``), and a brand-new experiment gets a marked ``[auto]``
        hypothesis composed from context — set the real one with
        :meth:`update_experiment` / ``probe experiment set``."""
        self.ensure_authenticated()
        experiment = experiment or defaults.default_experiment_slug()
        name = name or defaults.default_run_name()
        project_id = None
        if project:
            project_id = self.ensure_project(project)["id"]
        exp = self.ensure_experiment(
            experiment, experiment_name or experiment, hypothesis, project_id=project_id
        )
        return self.create_run(exp["id"], name, **run_kw)

    def heartbeat_run(self, run_id: str) -> dict:
        """``POST /v1/runs/{id}/heartbeat``: report that this run is still alive.

        Liveness cannot be inferred from `status`: it is a plain column, so a run
        whose process dies without a final PATCH stays 'running' forever and any
        "what is active" count decays into noise. Call this periodically while a
        run executes and the server's reaper marks anything that stops beating
        as 'crashed'.

        Beating is what makes a run REAPABLE -- a run that has never beat is
        never reaped -- so adopting this is safe and gradual, but a run that
        beats ONCE and then stops will eventually be marked crashed. Either beat
        for the run's whole life or not at all.

        Only a 'running' run is stamped; a late beat racing a normal completion
        is a no-op rather than an error.
        """
        return self.transport.post(f"/v1/runs/{run_id}/heartbeat", None, idempotent=True)

    # -- runs (read) --------------------------------------------------------
    def get_run(self, run_id: str, *, include_deleted: bool = False) -> dict:
        params = {"include": "deleted"} if include_deleted else None
        return self.transport.get(f"/v1/runs/{run_id}", params=params)

    def run_bundle(self, run_id: str) -> dict:
        return self.transport.get(f"/v1/runs/{run_id}/bundle")

    def run_lineage(self, run_id: str) -> dict:
        return self.transport.get(f"/v1/runs/{run_id}/lineage")

    def run_metrics(
        self,
        run_id: str,
        *,
        key: str | None = None,
        kind: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Raw metric points for a run. :meth:`run_series` is the summarized view;
        :meth:`query_series` is the multi-run comparison."""
        params = {k: v for k, v in {"key": key, "kind": kind, "limit": limit}.items() if v is not None}
        return self.transport.get(f"/v1/runs/{run_id}/metrics", params=params or None)

    def run_series(self, run_id: str) -> list[dict]:
        """Per-series summary for a run (key/kind/dimensions + first/last/min/max)."""
        return self.transport.get(f"/v1/runs/{run_id}/series")

    def run_spans(
        self,
        run_id: str,
        *,
        span_type: str | None = None,
        parent_span_id: str | None = None,
        step_from: int | None = None,
        step_to: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Read a run's trajectory spans back (the write path is ``Run.span``)."""
        params = {
            k: v
            for k, v in {
                "span_type": span_type,
                "parent_span_id": parent_span_id,
                "step_from": step_from,
                "step_to": step_to,
                "limit": limit,
            }.items()
            if v is not None
        }
        return self.transport.get(f"/v1/runs/{run_id}/spans", params=params or None)

    def get_span(self, span_id: str) -> dict:
        return self.transport.get(f"/v1/spans/{span_id}")

    # -- lifecycle (soft-delete / restore / purge) --------------------------
    def delete_run(self, run_id: str) -> dict:
        """Soft-delete: hides the run until restore or gc, and keeps its natural key
        reserved. Returns the deleted run. 404 if already deleted or absent."""
        return self.transport.delete(f"/v1/runs/{run_id}")

    def restore_run(self, run_id: str) -> dict:
        """Un-delete a soft-deleted run."""
        return self.transport.post(f"/v1/runs/{run_id}/restore", None)

    def gc_runs(self, *, run_ids: list[str] | None = None, older_than: str | None = None) -> dict:
        """PERMANENTLY purge soft-deleted runs (owner/admin). Exactly one selector:
        an explicit id list, or everything deleted before ``older_than``.

        Irreversible, and cascades to spans/metrics/artifacts. Purges DB rows only —
        R2 blobs are not touched (deferred, backend-side)."""
        # Truthiness, not `is None`: an empty run_ids list is not a valid selector, and
        # sending `{"run_ids": []}` could be read server-side as an unfiltered purge.
        if bool(run_ids) == bool(older_than):
            raise ValueError("gc_runs needs exactly one of run_ids (non-empty) or older_than")
        model = RunGcRequest(run_ids=run_ids, older_than=older_than)
        return self.transport.post(
            "/v1/runs/gc", model.model_dump(mode="json", exclude_none=True)
        )

    def presign_download(self, artifact_id: str) -> str:
        """Presigned GET URL for an artifact's blob (``POST /v1/artifacts/{id}/download``).

        The one home for this route literal: ``download_artifact*`` and the callers
        that need the raw doc in memory (``trial expand``, ``asset materialize``) all
        route through here, so the parity guard sees one reachable call site."""
        return self.transport.post(f"/v1/artifacts/{artifact_id}/download", None)["download_url"]

    def download_artifact(self, artifact_id: str) -> bytes:
        """Fetch an artifact's bytes into memory. Use :meth:`download_artifact_to`
        for anything large -- this holds the whole blob at once."""
        return self.transport.get_url(self.presign_download(artifact_id))

    def download_artifact_to(self, artifact_id: str, dest: str) -> dict:
        """Stream an artifact's blob to ``dest`` without buffering it in memory.

        Returns ``{artifact_id, dest, size_bytes, sha256}`` -- ``sha256`` is computed
        over the bytes as they land, so the caller can check it against the
        ``content_hash`` from a listing to prove the round trip (metadata match is not
        blob existence). On a mid-stream failure the partial file is removed rather
        than left behind as a truncated blob masquerading as the artifact -- the old
        in-memory path buffered before writing, so it never wrote a partial, and this
        preserves that guarantee."""
        url = self.presign_download(artifact_id)
        ok = False
        try:
            size, digest = self.transport.download_to(url, dest)
            ok = True
        finally:
            if not ok:
                Path(dest).unlink(missing_ok=True)
        return {"artifact_id": artifact_id, "dest": str(dest), "size_bytes": size, "sha256": digest}

    def delete_artifact(self, artifact_id: str) -> None:
        """Delete an artifact row."""
        self.transport.delete(f"/v1/artifacts/{artifact_id}")

    def gc_uploads(self, older_than: str) -> dict:
        """Sweep abandoned (never-confirmed) artifact uploads older than
        ``older_than``. Only ever touches pending rows; confirmed artifacts are
        untouched."""
        model = UploadGcRequest(older_than=older_than)
        return self.transport.post("/v1/artifacts/uploads/gc", model.model_dump(mode="json"))

    def check_run(self, run_id: str) -> dict:
        """Assess capture completeness from the bounded run bundle.

        This is a local read/assessment over API v3, not an assertion that the
        target immutable manifest exists.
        """
        bundle = self.run_bundle(run_id)
        run = bundle.get("run", bundle)
        artifacts = bundle.get("artifacts", [])
        metadata = run.get("metadata") or {}
        missing: list[str] = []
        # env_ref (execution record) is the launch-capture signal (fold #7). On the
        # ingest path it is run.env_ref; on the interactive path it is metadata.env_ref.
        if not (run.get("env_ref") or metadata.get("env_ref")):
            missing.append("execution_record")
        if not any(item.get("kind") == "code_snapshot" for item in artifacts):
            missing.append("code_snapshot_artifact")
        local_only = [
            item.get("id") or item.get("name")
            for item in artifacts
            if item.get("is_reference") and not item.get("uri")
        ]
        if local_only:
            missing.append("portable_artifact_bytes")
        return {
            "run_id": run_id,
            "state": "complete" if not missing else "incomplete",
            "missing": missing,
            "local_only_artifacts": local_only,
        }

    # -- lineage edges (fold #2) -------------------------------------------
    def add_edge(
        self,
        *,
        source_type: str,
        source_id: str,
        relation: str,
        target_type: str,
        target_id: str,
        meta: dict | None = None,
        strict: bool | None = None,
    ) -> dict | None:
        """POST /v1/edges. Closed vocab for types (run/artifact/asset_version) and
        relation (consumes/produces/evaluates_on/...); the generated EdgeCreate enforces it."""
        model = EdgeCreate(
            source_type=source_type,
            source_id=source_id,
            relation=relation,
            target_type=target_type,
            target_id=target_id,
            meta=meta or {},
        )
        return self.write(
            "POST", "/v1/edges", model.model_dump(mode="json", exclude_none=True), strict=strict
        )

    def run_edges(self, run_id: str) -> list[dict]:
        return self.transport.get(f"/v1/runs/{run_id}/edges")

    # -- execution records (fold #7) ---------------------------------------
    def execution_record(
        self,
        *,
        code: dict | None = None,
        deps: dict | None = None,
        hardware: dict | None = None,
        settings: dict | None = None,
        paths: dict | None = None,
    ) -> dict:
        """POST /v1/execution-records (content-addressed, idempotent). Returns
        {content_hash, ...}."""
        model = ExecutionRecordCreate(
            code=code or {},
            deps=deps or {},
            hardware=hardware or {},
            settings=settings or {},
            paths=paths or {},
        )
        return self.transport.post(
            "/v1/execution-records", model.model_dump(mode="json"), idempotent=True
        )

    def get_execution_record(self, content_hash: str) -> dict:
        return self.transport.get(f"/v1/execution-records/{content_hash}")

    # -- experiment versions (fold #6) -------------------------------------
    def experiment_version(
        self,
        experiment_id: str,
        *,
        label: str | None = None,
        as_of: str | None = None,
        exclude_run_ids: list[str] | None = None,
        strict: bool | None = None,
    ) -> dict | None:
        """POST /v1/experiments/{id}/versions - mint an immutable launch-time manifest
        (a snapshot of the experiment's runs). This replaces the removed run-level
        `promote`; Probe Research rejected promotion tiers."""
        model = ExperimentVersionMint(
            label=label, as_of=as_of, exclude_run_ids=exclude_run_ids or []
        )
        return self.write(
            "POST",
            f"/v1/experiments/{experiment_id}/versions",
            model.model_dump(mode="json", exclude_none=True),
            strict=strict,
        )

    def list_experiment_versions(self, experiment_id: str) -> list[dict]:
        return self.transport.get(f"/v1/experiments/{experiment_id}/versions")

    def get_experiment_version(self, experiment_id: str, version: int | str) -> dict:
        return self.transport.get(f"/v1/experiments/{experiment_id}/versions/{version}")

    def list_runs(self, *, experiment_id: str | None = None, **params) -> Page:
        query = dict(params)
        if experiment_id is not None:
            query["experiment_id"] = experiment_id
        return self.transport.get_page("/v1/runs", params=query or None)

    def list_run_artifacts(
        self,
        run_id: str,
        *,
        kind: str | None = None,
        step_from: int | None = None,
        step_to: int | None = None,
    ) -> list[dict]:
        """List a run's artifacts, optionally server-filtered by kind and/or an
        inclusive step window — e.g. sandbox states around a collapse:
        ``list_run_artifacts(run_id, kind="sandbox_state", step_from=599, step_to=601)``."""
        params = {
            key: value
            for key, value in {"kind": kind, "step_from": step_from, "step_to": step_to}.items()
            if value is not None
        }
        return self.transport.get(f"/v1/runs/{run_id}/artifacts", params=params or None)

    def list_experiment_artifacts(self, experiment_id: str) -> list[dict]:
        return self.transport.get(f"/v1/experiments/{experiment_id}/artifacts")

    def query_series(self, run_ids: list[str], **kw) -> dict:
        return self.transport.post(
            "/v1/series/query", {"run_ids": run_ids, **kw}, idempotent=True
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
        """``POST /v1/search`` (workspaces+kb fold-in): one-index exact+semantic search.

        POST-for-read, so it retries like any GET. Returns the sectioned
        per-channel response ``{query, state, exact:{results,cursor,error},
        semantic:{results,cursor,error}}``; a backend that predates the
        endpoint 404s (callers such as the MCP source fall back)."""
        body: dict[str, Any] = {"query": query}
        optional = {
            "corpus": corpus,
            "workspace_id": workspace_id,
            "project_id": project_id,
            "top_k": top_k,
            "exact_limit": exact_limit,
            "exact_cursor": exact_cursor,
            "semantic_cursor": semantic_cursor,
        }
        body.update({key: value for key, value in optional.items() if value is not None})
        return self.transport.post("/v1/search", body, idempotent=True)

    def browse(
        self,
        *,
        scope: str | None = None,
        depth: int | None = None,
        status: str | None = None,
        limit: int | None = None,
        cursor: str | None = None,
    ) -> dict:
        """``GET /v1/browse``: the structured "what exists" tree.

        Where ``search`` ranks by relevance and needs a query, this enumerates
        structure and needs nothing -- the cold-start read. Returns
        ``{projects|experiments|runs, cursor, depth, limit, truncated}`` with
        exactly one level populated at the top, decided by ``scope``.

        A backend that predates the endpoint 404s; callers fall back honestly
        rather than presenting an empty tree as "nothing exists".
        """
        params: dict[str, Any] = {}
        optional = {
            "scope": scope,
            "depth": depth,
            "status": status,
            "limit": limit,
            "cursor": cursor,
        }
        params.update({k: v for k, v in optional.items() if v is not None})
        return self.transport.get("/v1/browse", params=params or None)

    # -- passive / batch push ----------------------------------------------
    def ingest(
        self,
        *,
        experiment_slug: str,
        run: dict,
        project_slug: str | None = None,
        experiment_hypothesis: str | None = None,
        batch_id: str | None = None,
        execution_record: dict | None = None,
        spans: list[dict] | None = None,
        metrics: list[dict] | None = None,
        artifacts: list[dict] | None = None,
        strict: bool | None = None,
    ) -> dict | None:
        """One idempotent push (bearer ingest token + optional HMAC). Keyed on
        ``(customer_id, run.source, run.external_id)`` with ``batch_id`` dedup.

        Built through the generated ``IngestRunRequest`` (the backend now declares
        this body in its OpenAPI schema), so a malformed run/span/metric/artifact
        fails client-side instead of as a server 422.

        The ingest path is where the fold-in fields actually pin server-side:
        ``run['foreign_keys']`` (per-key new-wins merge), ``execution_record``
        (pins ``run.env_ref``), and per-metric ``dimensions``."""
        model = IngestRunRequest(
            experiment_slug=experiment_slug,
            run=run,
            project_slug=project_slug,
            experiment_hypothesis=experiment_hypothesis,
            batch_id=batch_id,
            execution_record=execution_record,
            spans=spans or [],
            metrics=metrics or [],
            artifacts=artifacts or [],
        )
        body = model.model_dump(mode="json", exclude_none=True)
        return self.write("POST", "/ingest/v1/runs", body, strict=strict)

    # -- composed SDK surfaces --------------------------------------------
    @property
    def sessions(self):
        """Hook-facing session capture API; not an experiment telemetry API."""
        if self._sessions is None:
            from .sessions import SessionCaptureClient

            self._sessions = SessionCaptureClient(self)
        return self._sessions

    @property
    def notes(self):
        """Write structured research notes (intent/decision/observation) as artifacts."""
        if self._notes is None:
            from .events import NoteClient

            self._notes = NoteClient(self)
        return self._notes

    @property
    def events(self):
        """Read the backend append-only lifecycle+structure events log (read-only)."""
        if self._events is None:
            from .events import EventsReadClient

            self._events = EventsReadClient(self)
        return self._events

    @property
    def assets(self):
        """Versioned-asset registry client (fold #5): register + zero-copy versions."""
        if self._assets is None:
            from .assets import AssetClient

            self._assets = AssetClient(self)
        return self._assets


# Late import to avoid a cycle at module load (Run needs Client, Client returns Run).
from .run import Run  # noqa: E402
