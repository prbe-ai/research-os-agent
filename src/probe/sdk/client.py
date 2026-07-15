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
from typing import Any

from . import defaults, errors
from ..models import EdgeCreate, ExecutionRecordCreate, ExperimentVersionMint, IngestRunRequest
from .config import Settings, resolve
from .spool import Spool
from .transport import Page, Transport


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
        from .config import load_file, save_file
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
        data = load_file()
        data["base_url"] = self.settings.base_url
        data["token"] = token
        save_file(data)
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

    # -- projects -----------------------------------------------------------
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

    def list_projects(self, **params) -> Page:
        return self.transport.get_page("/v1/projects", params=params or None)

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

    # -- runs (read) --------------------------------------------------------
    def get_run(self, run_id: str, *, include_deleted: bool = False) -> dict:
        params = {"include": "deleted"} if include_deleted else None
        return self.transport.get(f"/v1/runs/{run_id}", params=params)

    def run_bundle(self, run_id: str) -> dict:
        return self.transport.get(f"/v1/runs/{run_id}/bundle")

    def run_lineage(self, run_id: str) -> dict:
        return self.transport.get(f"/v1/runs/{run_id}/lineage")

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
