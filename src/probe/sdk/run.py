"""The SDK Run handle: the agent's per-run write surface.

Wraps a run row and exposes the write verbs from the SDK/CLI sketch, each mapped
to a v3 endpoint:

  log()/log_hw() -> POST /v1/runs/{id}/metrics       (first-class dimensions, fold #9)
  span()/step()  -> POST /v1/runs/{id}/spans | /steps      (trajectory)
  log_artifact() -> POST /v1/runs/{id}/artifacts, or the presign upload flow
                    (fold #16: fingerprint -> presign -> PUT to R2 -> confirm)
  link()         -> PATCH /v1/runs/{id} (per-key new-wins merge into the real
                    runs.foreign_keys column, fold #8)
  snapshot()     -> content-addressed execution record (fold #7); pins run.env_ref
                    and records the git shadow ref as a code_snapshot artifact
  finish()       -> PATCH /v1/runs/{id} {status, ended_at}

The presign upload flow carries ``kind``/``meta`` (Harbor-ownership Phase 0), so
byte uploads and reference artifacts label identically — no gaps flagged.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import warnings
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from . import errors
from . import snapshot as _snapshot
from ..models import (
    ArtifactCreate,
    ExecutionRecordCreate,
    MetricBatch,
    MetricPointIn,
    SpanBatch,
    SpanCreate,
    UploadRequest,
)

if TYPE_CHECKING:
    from .client import Client


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Run:
    def __init__(self, client: "Client", data: dict):
        self._client = client
        self._data = data

    # -- identity -----------------------------------------------------------
    @property
    def id(self) -> str:
        return str(self._data["id"])

    @property
    def experiment_id(self) -> str:
        return str(self._data["experiment_id"])

    @property
    def name(self) -> str:
        return str(self._data["name"])

    @property
    def status(self) -> str:
        return str(self._data.get("status", "running"))

    @property
    def short_id(self) -> str | None:
        """Human-readable petname (fold #21); present on /v1 reads (RunDetailOut)."""
        return self._data.get("short_id")

    @property
    def foreign_keys(self) -> dict:
        """Incumbent-id map (fold #8); present on /v1 reads (RunDetailOut)."""
        return self._data.get("foreign_keys") or {}

    @property
    def data(self) -> dict:
        return self._data

    def refresh(self) -> "Run":
        self._data = self._client.get_run(self.id)
        return self

    def edges(self) -> list[dict]:
        """Lineage edges touching this run (fold #2): GET /v1/runs/{id}/edges."""
        return self._client.transport.get(f"/v1/runs/{self.id}/edges")

    # -- spine --------------------------------------------------------------
    def child(self, name: str, *, relation: str = "fork", **kw) -> "Run":
        """Open a sub-run. ``relation`` in fork|resume|retry|branch."""
        return self._client.create_run(
            self.experiment_id,
            name,
            parent_run_id=self.id,
            parent_relation=relation,
            **kw,
        )

    # -- metrics ------------------------------------------------------------
    def log(
        self,
        metrics: dict[str, float],
        *,
        step: int | None = None,
        kind: str = "model",
        wall_clock: str | None = None,
        dimensions: dict[str, Any] | None = None,
        strict: bool | None = None,
    ):
        """Append metric points. Fail-open by default (spools on failure).

        ``dimensions`` is a bounded flat label map (<=8 keys); it widens the series
        identity to ``(run,kind,key,dims_hash)`` (fold #9). Dimension-less points stay
        byte-identical. Built through the generated ``MetricBatch``/``MetricPointIn``,
        so schema drift fails here, not as a server 422."""
        dims = dimensions or {}
        batch = MetricBatch(
            points=[
                MetricPointIn(
                    key=key,
                    kind=kind,
                    value=float(value),
                    step_index=step,
                    wall_clock=wall_clock,
                    dimensions=dims,
                )
                for key, value in metrics.items()
            ]
        )
        body = batch.model_dump(mode="json", exclude_none=True)
        return self._client.write(
            "POST", f"/v1/runs/{self.id}/metrics", body, strict=strict
        )

    def log_hw(
        self,
        metrics: dict[str, float],
        *,
        step: int | None = None,
        wall_clock: str | None = None,
        strict: bool | None = None,
        **dims: Any,
    ):
        """Log hardware metrics with real dimensions (host/rank/device, fold #9).

        ``run.log_hw({"gpu_temp": 88}, device=3, host="n1")`` sends
        ``dimensions={"device": 3, "host": "n1"}``, kind=hardware."""
        return self.log(
            metrics, step=step, kind="hardware", wall_clock=wall_clock,
            dimensions=dims or None, strict=strict,
        )

    # -- trajectory (spans) -------------------------------------------------
    def span(
        self,
        span_type: str,
        *,
        id: str | None = None,
        parent_span_id: str | None = None,
        name: str | None = None,
        step_index: int | None = None,
        external_key: str | None = None,
        provider: str | None = None,
        status: str = "running",
        started_at: str | None = None,
        ended_at: str | None = None,
        attributes: dict | None = None,
        summary: dict | None = None,
        strict: bool | None = None,
    ) -> str:
        """Upsert one span (client-generated UUID). Returns the span id."""
        span_id = id or str(uuid4())
        UUID(span_id)  # validate shape early
        span = SpanCreate(
            id=span_id,
            span_type=span_type,
            parent_span_id=parent_span_id,
            name=name,
            step_index=step_index,
            external_key=external_key,
            provider=provider,
            status=status,
            started_at=started_at,
            ended_at=ended_at,
            attributes=attributes or {},
            summary=summary or {},
        )
        body = SpanBatch(spans=[span]).model_dump(mode="json")
        self._client.write("POST", f"/v1/runs/{self.id}/spans", body, strict=strict)
        return span_id

    def step(self, step_index: int, *, name: str | None = None, **kw):
        body = {"step_index": step_index, "name": name, **kw}
        return self._client.write("POST", f"/v1/runs/{self.id}/steps", body)

    # -- artifacts ----------------------------------------------------------
    def log_artifact(
        self,
        name: str,
        *,
        path: str | None = None,
        uri: str | None = None,
        kind: str = "file",
        content_hash: str | None = None,
        content_type: str | None = None,
        size_bytes: int | None = None,
        is_reference: bool | None = None,
        span_id: str | None = None,
        step_index: int | None = None,
        meta: dict | None = None,
        strict: bool | None = None,
    ):
        """Record an artifact.

        With ``path`` and no ``uri``: the real presign upload flow (fold #16) runs,
        fingerprint -> presign -> PUT bytes to R2 -> confirm, carrying
        ``kind``/``meta`` so the stored artifact is labeled (harbor_trial,
        sandbox_state, ...) exactly like a reference artifact would be.

        With ``uri`` (object already in a bucket) or no bytes: a metadata-only
        reference artifact is recorded, as before."""
        meta = dict(meta or {})
        if path is not None and uri is None:
            digest, size = _fingerprint(path)
            return self._upload_file(
                name,
                path,
                kind=kind,
                content_hash=content_hash or digest,
                size_bytes=size_bytes if size_bytes is not None else size,
                content_type=content_type,
                span_id=span_id,
                step_index=step_index,
                meta=meta,
                strict=strict,
            )

        # Reference / uri path (no bytes uploaded).
        if path is not None:
            digest, size = _fingerprint(path)
            content_hash = content_hash or digest
            size_bytes = size_bytes if size_bytes is not None else size
            meta.setdefault("local_path", os.path.abspath(path))
        artifact = ArtifactCreate(
            kind=kind,
            name=name,
            uri=uri,
            content_hash=content_hash,
            content_type=content_type,
            size_bytes=size_bytes,
            is_reference=bool(is_reference) if is_reference is not None else (uri is not None),
            span_id=span_id,
            step_index=step_index,
            meta=meta,
        )
        body = artifact.model_dump(mode="json", exclude_none=True)
        return self._client.write(
            "POST", f"/v1/runs/{self.id}/artifacts", body, strict=strict
        )

    def _upload_file(
        self,
        name: str,
        path: str,
        *,
        kind: str,
        content_hash: str,
        size_bytes: int,
        content_type: str | None,
        span_id: str | None,
        step_index: int | None,
        meta: dict,
        strict: bool | None,
    ):
        """presign -> PUT -> confirm. Fail-open: on failure (and not strict) falls
        back to recording a hash+metadata reference so the training loop is unblocked."""
        strict_resolved = (not self._client.fail_open) if strict is None else strict
        req = UploadRequest(
            name=name,
            content_hash=content_hash,
            size_bytes=size_bytes,
            content_type=content_type,
            span_id=span_id,
            step_index=step_index,
            kind=kind if kind != "file" else None,  # None preserves labels on restage
            meta=meta or None,
        )
        try:
            presign = self._client.transport.post(
                f"/v1/runs/{self.id}/artifacts/uploads",
                req.model_dump(mode="json", exclude_none=True),
            )
            if not presign.get("have"):
                with open(path, "rb") as fh:
                    data = fh.read()
                self._client.transport.put_url(
                    presign["upload_url"],
                    data,
                    content_type=content_type or "application/octet-stream",
                    headers=presign.get("upload_headers") or presign.get("headers"),
                )
            return self._client.transport.post(
                f"/v1/artifacts/{presign['artifact_id']}/confirm", None
            )
        except errors.RosError:
            if strict_resolved:
                raise
            warnings.warn(
                f"artifact upload for '{name}' failed; recorded as a reference instead.",
                stacklevel=3,
            )
            fallback = ArtifactCreate(
                kind=kind,
                name=name,
                content_hash=content_hash,
                size_bytes=size_bytes,
                content_type=content_type,
                is_reference=True,
                span_id=span_id,
                step_index=step_index,
                meta={**meta, "local_path": os.path.abspath(path), "upload": "failed"},
            )
            return self._client.write(
                "POST",
                f"/v1/runs/{self.id}/artifacts",
                fallback.model_dump(mode="json", exclude_none=True),
                strict=False,
            )

    # -- foreign keys (shadow-SoT handles) ----------------------------------
    def link(self, *, strict: bool | None = None, **foreign_keys: Any):
        """Attach foreign keys (wandb_run_id, mlflow_run_id, s3_prefix, ...) to the
        real ``runs.foreign_keys`` column (fold #8). The server merges per-key
        new-wins via RunPatch, so a late-discovered id attaches without clobbering
        earlier keys and no read-modify-write round-trip is needed."""
        data = self._client.write(
            "PATCH", f"/v1/runs/{self.id}", {"foreign_keys": foreign_keys}, strict=strict
        )
        if data:
            self._data = data
        return data

    # -- snapshot (execution record) ----------------------------------------
    def snapshot(
        self,
        *,
        cwd: str | None = None,
        include_env: bool = True,
        include_gpu: bool = True,
        strict: bool | None = None,
    ) -> dict:
        """Capture code (git shadow ref) + deps + GPUs as a content-addressed
        execution record (fold #7), and record the shadow commit as a reference
        artifact. Non-disruptive.

        The execution record pins ``run.env_ref`` to its content hash via RunPatch
        (fold #7 + the RunPatch env_ref parity), the same column the ingest path sets."""
        git = _snapshot.capture_git_snapshot(self.id, cwd)
        record = ExecutionRecordCreate(
            code={"git": git},
            deps=_snapshot.capture_env() if include_env else {},
            hardware={"gpu": _snapshot.capture_gpu()} if include_gpu else {},
        )
        exec_rec = self._client.transport.post(
            "/v1/execution-records", record.model_dump(mode="json", exclude_none=True)
        )
        content_hash = exec_rec.get("content_hash") if exec_rec else None

        # Pin the real runs.env_ref column (FK to the execution record just created).
        if content_hash is not None:
            data = self._client.write(
                "PATCH", f"/v1/runs/{self.id}", {"env_ref": content_hash}, strict=strict
            )
            if data:
                self._data = data
                if data.get("env_ref") != content_hash:
                    message = (
                        "Probe Research API did not persist run.env_ref after snapshot "
                        f"(expected {content_hash}, got {data.get('env_ref')!r})"
                    )
                    if strict is True or (strict is None and not self._client.fail_open):
                        raise errors.CapabilityUnavailable("run.env_ref", message)
                    warnings.warn(message, stacklevel=2)
        # Record the shadow commit as a reference artifact for lineage.
        self.log_artifact(
            "code-snapshot",
            uri=f"git:{git['ref']}#{git['commit']}",
            kind="code_snapshot",
            is_reference=True,
            meta={"branch": git.get("branch"), "dirty": git.get("dirty"), "env_ref": content_hash},
            strict=strict,
        )
        return {"git": git, "execution_record": exec_rec, "content_hash": content_hash}

    # -- lifecycle ----------------------------------------------------------
    def set_status(self, status: str, *, ended_at: str | None = None, summary: dict | None = None):
        body: dict[str, Any] = {"status": status}
        if ended_at is not None:
            body["ended_at"] = ended_at
        if summary is not None:
            body["summary"] = summary
        data = self._client.write("PATCH", f"/v1/runs/{self.id}", body, strict=True)
        if data:
            self._data = data
        return data

    def finish(self, status: str = "completed", *, summary: dict | None = None):
        """Close the run. Flushes any spooled writes first."""
        self._client.flush()
        return self.set_status(status, ended_at=_now(), summary=summary)

    def execute(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        """Run a local command with deterministic run/process correlation.

        This is normal experiment execution capture, not the hook-only session
        API. Output streams pass through to the caller; a process span records
        argv, cwd, timestamps, and exit state.
        """
        if not argv:
            raise ValueError("argv must not be empty")
        started_at = _now()
        span_id = self.span(
            "process",
            name=os.path.basename(argv[0]),
            status="running",
            started_at=started_at,
            attributes={"argv": argv, "cwd": os.path.abspath(cwd or os.getcwd())},
        )
        process_env = {**os.environ, **(env or {}), "PROBE_RUN_ID": self.id}
        try:
            result = subprocess.run(argv, cwd=cwd, env=process_env, check=False)
        except BaseException:
            self.span(
                "process",
                id=span_id,
                name=os.path.basename(argv[0]),
                status="failed",
                started_at=started_at,
                ended_at=_now(),
                attributes={"argv": argv, "cwd": os.path.abspath(cwd or os.getcwd())},
            )
            raise
        self.span(
            "process",
            id=span_id,
            name=os.path.basename(argv[0]),
            status="completed" if result.returncode == 0 else "failed",
            started_at=started_at,
            ended_at=_now(),
            attributes={
                "argv": argv,
                "cwd": os.path.abspath(cwd or os.getcwd()),
                "exit_code": result.returncode,
            },
        )
        return result

    # -- context manager ----------------------------------------------------
    def __enter__(self) -> "Run":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.finish("failed" if exc_type else "completed")


def _fingerprint(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size
