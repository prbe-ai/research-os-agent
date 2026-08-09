"""Read-only Reader for a ``probe_svc_`` service token — the external-product egress surface.

A service token is team-scoped, userless, and read-only. This client exposes exactly the
backend's allowlisted read endpoints (metrics, enumeration, metadata, artifact bytes) as
one method each. See ``docs/service-token-sdk.md`` for the full method -> endpoint map.

    from probe import Reader

    r = Reader.from_env()                         # PROBE_SERVICE_TOKEN + PROBE_BASE_URL
    pt = r.metrics(run_id, key="reward", labels={"sample": 5})   # one synchronous query
    blob = r.download_artifact(artifact_id)       # bytes: presigned URL, proxy fallback

Every method is a thin, synchronous request/response — this is a live-read client, not a
bulk mirror. Unlisted operations (writes, transcripts, search) 403 by design: the token's
allowlist is enforced server-side.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from . import errors
from .config import resolve
from .transport import Transport


@dataclass
class Reference:
    """A client-owned reference artifact: its bytes live in YOUR store, not Probe's.

    Probe never server-fetches a reference (the confused-deputy rule), so the Reader
    hands back the pointer instead of bytes — resolve ``uri`` with your own credentials.
    """

    uri: str | None
    local_path: str | None = None
    host: str | None = None


def _clean(params: dict[str, Any]) -> dict[str, Any]:
    """Drop None-valued query params so an unset filter is simply absent."""
    return {k: v for k, v in params.items() if v is not None}


def _json_param(value: Any) -> Any:
    """A dict coord/label filter becomes the JSON string the API expects; scalars pass through."""
    return json.dumps(value) if isinstance(value, dict) else value


class Reader:
    """Read-only egress client authenticated by a ``probe_svc_`` service token.

    Construct with :meth:`from_env` (or pass a prepared :class:`Transport`). Use as a
    context manager to close the underlying HTTP client.
    """

    def __init__(self, transport: Transport):
        self._t = transport

    @classmethod
    def from_env(
        cls, *, base_url: str | None = None, service_token: str | None = None
    ) -> "Reader":
        """Build a Reader from ``PROBE_SERVICE_TOKEN`` + ``PROBE_BASE_URL`` (or explicit args).

        Deliberately does NOT fall back to ``PROBE_TOKEN``: a Reader is a service-token
        client and carries ONLY the service token as its ``/v1`` bearer.
        """
        settings = resolve(base_url=base_url, service_token=service_token)
        if not settings.service_token:
            raise errors.AuthError(
                "no service token configured (set PROBE_SERVICE_TOKEN or pass service_token=)"
            )
        settings.token = None
        return cls(Transport(settings))

    def close(self) -> None:
        self._t.close()

    def __enter__(self) -> "Reader":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- pagination ---------------------------------------------------------
    def _all(self, path: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Follow ``X-Next-Cursor`` and return every item across all pages."""
        out: list[dict] = []
        params = dict(params or {})
        while True:
            page = self._t.get_page(path, params=params)
            out.extend(page.items)
            if not page.next_cursor:
                return out
            params["cursor"] = page.next_cursor

    # -- enumeration --------------------------------------------------------
    def projects(self, *, workspace_id=None, slug=None, tags=None) -> list[dict]:
        return self._all(
            "/v1/projects", _clean({"workspace_id": workspace_id, "slug": slug, "tags": tags})
        )

    def project(self, project_id: str) -> dict:
        return self._t.get(f"/v1/projects/{project_id}")

    def experiments(self, *, project_id=None, slug=None, tags=None) -> list[dict]:
        return self._all(
            "/v1/experiments", _clean({"project_id": project_id, "slug": slug, "tags": tags})
        )

    def experiment(self, experiment_id: str) -> dict:
        return self._t.get(f"/v1/experiments/{experiment_id}")

    def runs(self, *, experiment_id=None, project_id=None, status=None, tags=None) -> list[dict]:
        return self._all(
            "/v1/runs",
            _clean(
                {
                    "experiment_id": experiment_id,
                    "project_id": project_id,
                    "status": status,
                    "tags": tags,
                }
            ),
        )

    def run(self, ref: str) -> dict:
        """A run by UUID or petname short_id, with counts + metadata."""
        return self._t.get(f"/v1/runs/{ref}")

    def browse(self, *, scope=None, depth=1, status=None, tags=None) -> dict:
        """The 'what exists' tree (scope=``project:<id>`` | ``experiment:<id>``)."""
        return self._t.get(
            "/v1/browse", params=_clean({"scope": scope, "depth": depth, "status": status, "tags": tags})
        )

    def groups(self, experiment_id: str) -> list[dict]:
        return self._t.get(f"/v1/experiments/{experiment_id}/groups")

    def group(self, group_id: str) -> dict:
        return self._t.get(f"/v1/groups/{group_id}")

    # -- metrics ------------------------------------------------------------
    def metrics(
        self,
        run: str,
        *,
        key=None,
        kind=None,
        dimensions=None,
        labels=None,
        span_id=None,
        step_from=None,
        step_to=None,
        limit=None,
    ) -> list[dict]:
        """The synchronous point query — pull a specific point or set by any
        differentiator. ``dimensions``/``labels`` are dicts (type-faithful JSON
        containment). A ``labels`` or ``span_id`` filter REQUIRES ``key`` (the backend
        prunes the scan by (run, key) first)."""
        return self._t.get(
            f"/v1/runs/{run}/metrics",
            params=_clean(
                {
                    "key": key,
                    "kind": kind,
                    "dimensions": _json_param(dimensions),
                    "labels": _json_param(labels),
                    "span_id": span_id,
                    "step_from": step_from,
                    "step_to": step_to,
                    "limit": limit,
                }
            ),
        )

    def metrics_grouped(
        self,
        run: str,
        key: str,
        *,
        by=None,
        where=None,
        agg=None,
        kind=None,
        step_bucket=None,
        step_from=None,
        step_to=None,
    ) -> dict:
        """Server-side reduce/group over coordinate axes (``by``/``where`` on dims)."""
        return self._t.get(
            f"/v1/runs/{run}/metrics/grouped",
            params=_clean(
                {
                    "key": key,
                    "by": by,
                    "where": _json_param(where),
                    "agg": agg,
                    "kind": kind,
                    "step_bucket": step_bucket,
                    "step_from": step_from,
                    "step_to": step_to,
                }
            ),
        )

    def metrics_wide(self, run: str, *, key=None, kind=None, step_from=None, step_to=None) -> dict:
        """Step x metric pivot (DataFrame-friendly)."""
        return self._t.get(
            f"/v1/runs/{run}/metrics/wide",
            params=_clean({"key": key, "kind": kind, "step_from": step_from, "step_to": step_to}),
        )

    def series(self, run: str) -> list[dict]:
        """The run's series catalog (kind/key/dims + eligibility flags)."""
        return self._t.get(f"/v1/runs/{run}/series")

    def series_query(self, run_ids: list[str], **body: Any) -> dict:
        """Multi-run charting read (downsampled/smoothed); POST-for-read."""
        return self._t.post("/v1/series/query", {"run_ids": run_ids, **body}, idempotent=True)

    def coordinates(self, run: str) -> list[dict]:
        """Every non-empty coordinate any fact landed on."""
        return self._t.get(f"/v1/runs/{run}/coordinates")

    # -- run detail ---------------------------------------------------------
    def bundle(self, run: str) -> dict:
        """One-shot: run + series + chart settings + artifacts + lineage."""
        return self._t.get(f"/v1/runs/{run}/bundle")

    def lineage(self, run: str) -> dict:
        return self._t.get(f"/v1/runs/{run}/lineage")

    # -- artifacts ----------------------------------------------------------
    def artifacts(self, run: str, *, kind=None, prefix=None) -> list[dict]:
        """Artifact metadata under a run (list; bytes come from download_artifact)."""
        return self._t.get(
            f"/v1/runs/{run}/artifacts", params=_clean({"kind": kind, "prefix": prefix})
        )

    def download_url(self, artifact_id: str) -> "str | Reference":
        """The presigned GET URL for a managed artifact, or a :class:`Reference` when the
        artifact is a client-owned pointer (resolve it with your own credentials)."""
        try:
            return self._t.post(f"/v1/artifacts/{artifact_id}/download")["download_url"]
        except errors.ConflictError as exc:
            ref = _reference_or_none(exc)
            if ref is not None:
                return ref
            raise

    def download_artifact(
        self, artifact_id: str, *, dest: str | None = None, mode: str = "auto"
    ) -> "bytes | dict | Reference":
        """Fetch a managed artifact's bytes.

        - ``mode="auto"`` (default): presigned URL first; if the object store is
          unreachable (self-host / air-gap), fall back to the API byte proxy.
        - ``mode="url"``: return the presigned URL string (or a :class:`Reference`)
          without fetching.
        - ``mode="proxy"``: always fetch through the API byte proxy (``/content``) —
          the path for a store the client cannot reach.

        A client-owned reference returns a :class:`Reference` (never server-fetched).
        ``dest``: stream to a file and return ``{size_bytes, sha256}`` instead of bytes.
        """
        if mode == "proxy":
            return self._proxy(artifact_id, dest)
        target = self.download_url(artifact_id)
        if isinstance(target, Reference):
            return target
        if mode == "url":
            return target
        try:
            if dest is not None:
                size, sha = self._t.download_to(target, dest)
                return {"size_bytes": size, "sha256": sha}
            return self._t.get_url(target)
        except errors.TransportError:
            if mode == "auto":
                return self._proxy(artifact_id, dest)  # store unreachable -> API proxy
            raise

    def preview(self, artifact_id: str) -> bytes:
        """Bounded inline bytes for previewable (text/raster) artifacts."""
        return self._t.request("GET", f"/v1/artifacts/{artifact_id}/preview").content

    def _proxy(self, artifact_id: str, dest: str | None) -> "bytes | dict | Reference":
        try:
            resp = self._t.request("GET", f"/v1/artifacts/{artifact_id}/content")
        except errors.ConflictError as exc:
            ref = _reference_or_none(exc)
            if ref is not None:
                return ref
            raise
        if dest is None:
            return resp.content
        with open(dest, "wb") as fh:
            fh.write(resp.content)
        return {"size_bytes": len(resp.content), "sha256": hashlib.sha256(resp.content).hexdigest()}


def _reference_or_none(exc: errors.ConflictError) -> Reference | None:
    detail = exc.detail
    if isinstance(detail, dict) and detail.get("reason") == "reference":
        return Reference(
            uri=detail.get("uri"), local_path=detail.get("local_path"), host=detail.get("host")
        )
    return None
