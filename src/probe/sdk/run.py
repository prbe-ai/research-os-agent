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

import os
import socket
import subprocess
import threading
import warnings
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from . import errors
from . import snapshot as _snapshot
from .hashing import fingerprint, local_file_uri, reference_fields
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


#: Statuses after which a run can never beat again. Mirrors the server's CHECK
#: constraint minus 'created'/'running' (db/experiment/schema.sql in research-os).
_TERMINAL_STATUSES = frozenset({"completed", "failed", "crashed", "canceled"})

#: The server reaps a beating run after `run_heartbeat_stale_seconds` of silence
#: (default 900, floor 300 — app/core/config.py in research-os). 60s keeps many
#: beats inside even the floor, so one dropped request never looks like death.
_HEARTBEAT_INTERVAL_SECONDS = 60.0


def _heartbeat_interval() -> float:
    """Read PROBE_HEARTBEAT_SECONDS at call time, never at import time.

    ``0`` (or any non-positive value) is the kill switch: no thread is started.
    """
    raw = os.environ.get("PROBE_HEARTBEAT_SECONDS")
    if raw is None:
        return _HEARTBEAT_INTERVAL_SECONDS
    try:
        return float(raw)
    except ValueError:
        return _HEARTBEAT_INTERVAL_SECONDS


def _beat_forever(client: "Client", run_id: str, stop: threading.Event, interval: float) -> None:
    """The heartbeat loop. A module function, not a bound method, so the thread
    pins only the client and the run id — an abandoned Run handle stays collectable.

    Failures are swallowed: a missed beat self-heals (the stale window is many
    intervals wide) and liveness reporting must never take down the work it is
    reporting on. Beats deliberately bypass the spool — replaying a stale "I was
    alive" later would be a lie.
    """
    while True:
        try:
            client.heartbeat_run(run_id)
        except Exception:
            pass
        if stop.wait(interval):
            return


class Run:
    def __init__(self, client: "Client", data: dict):
        self._client = client
        self._data = data
        self._hb_stop: threading.Event | None = None
        self._hb_thread: threading.Thread | None = None

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
        reference: bool = False,
        hash_content: bool = False,
        allow_missing: bool = False,
        span_id: str | None = None,
        step_index: int | None = None,
        meta: dict | None = None,
        strict: bool | None = None,
    ):
        """Record an artifact.

        With ``path`` and no ``uri`` and no ``reference``: the real presign upload flow
        (fold #16) runs, fingerprint -> presign -> PUT bytes to R2 -> confirm.

        With ``reference=True`` and a ``path``: a PATH reference is recorded -- the file's
        location is stored as a ``file://`` uri (raw path in ``meta.local_path``, recording
        host in ``meta.host``) and its bytes are NOT uploaded. Only ``os.stat`` runs unless
        ``hash_content`` asks for a fingerprint. This is the shared-volume case: a 16 GB
        checkpoint or a TB of files an agent on the same volume resolves locally. Raises
        ``FileNotFoundError`` if the path is missing unless ``allow_missing``.

        With ``uri`` (object already in a bucket) or no bytes: a metadata-only reference
        artifact is recorded, as before."""
        meta = dict(meta or {})
        # Explicit path reference: record WHERE the bytes live (file://) instead of
        # uploading them. Takes precedence over the upload branch so path + reference
        # never force-uploads (the old code ignored is_reference for path+no-uri).
        if reference and path is not None:
            fields = reference_fields(
                path, hash_content=hash_content, allow_missing=allow_missing
            )
            uri = uri or fields["uri"]
            if content_hash is None:
                content_hash = fields.get("content_hash")
            if size_bytes is None:
                size_bytes = fields.get("size_bytes")
            for key, value in fields["meta"].items():
                meta.setdefault(key, value)
            is_reference = True
        elif path is not None and uri is None:
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
        elif path is not None:
            # A uri AND a local copy: fingerprint for metadata, keep uri as the pointer.
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

    def list_artifacts(self, *, scope: str = "all", **filters: Any) -> list[dict]:
        """Artifacts visible to this run. Defaults to ``scope="all"`` -- the run's own
        artifacts PLUS the ones promoted to its experiment and project, each tagged
        ``source_level`` -- because during a run that inherited context is usually what
        you want. Pass ``scope="own"`` to see only this run's, ``scope="inherited"`` for
        just the parent levels. Extra kwargs (``kind``, ``step_from``, ``step_to``) filter
        server-side."""
        return self._client.list_run_artifacts(self.id, scope=scope, **filters)

    def resolve_artifact(self, name: str, *, scope: str = "all") -> dict | None:
        """The nearest artifact named ``name`` visible to this run, or ``None``. The
        backend returns nearest-wins order (run before experiment before project), so a
        run-level artifact shadows a same-named one promoted higher."""
        rows = self._client.list_run_artifacts(self.id, name=name, scope=scope)
        return rows[0] if rows else None

    def promote_artifact(self, artifact_id: str, *, to: str) -> dict:
        """Promote one of this run's artifacts up to its experiment or project so every
        run under that scope can see it (``to="experiment"`` or ``"project"``). Sugar over
        ``Client.move_artifact``; the target scope is derived from this run's chain."""
        return self._client.move_artifact(artifact_id, level=to)

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
                # Stream the file (model weights fit here); never read it whole into
                # memory. size_bytes is the fingerprinted length the presign signed.
                self._client.transport.put_file(
                    presign["upload_url"],
                    path,
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
            local = os.path.abspath(path)
            fallback = ArtifactCreate(
                kind=kind,
                name=name,
                uri=local_file_uri(local),
                content_hash=content_hash,
                size_bytes=size_bytes,
                content_type=content_type,
                is_reference=True,
                span_id=span_id,
                step_index=step_index,
                meta={
                    **meta,
                    "local_path": local,
                    "host": socket.gethostname(),
                    "upload": "failed",
                },
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

    # -- liveness -----------------------------------------------------------
    def start_heartbeat(self, interval_seconds: float | None = None) -> None:
        """Beat ``POST /v1/runs/{id}/heartbeat`` from a daemon thread until a
        terminal :meth:`set_status` (or the process exits). Idempotent.

        ``Client.create_run`` calls this for every handle it mints, so the rule
        from :meth:`Client.heartbeat_run` — beat for the run's whole life or not
        at all — holds by construction: the beats stop exactly when this process
        stops, and a process that dies without finishing is precisely what the
        server's reaper should flip to 'crashed'. Only start this on a handle
        whose run lives and dies with the current process; a run managed from
        outside (CLI ``run start``, the miles exporter) must never beat.

        Precedence for the interval mirrors config.resolve: explicit argument,
        then PROBE_HEARTBEAT_SECONDS, then the 60s default. Non-positive
        disables.
        """
        if self._hb_thread is not None and self._hb_thread.is_alive():
            return
        interval = _heartbeat_interval() if interval_seconds is None else float(interval_seconds)
        if interval <= 0:
            return
        stop = threading.Event()
        thread = threading.Thread(
            target=_beat_forever,
            args=(self._client, self.id, stop, interval),
            name=f"probe-run-heartbeat-{self.id[:8]}",
            daemon=True,
        )
        self._hb_stop = stop
        self._hb_thread = thread
        thread.start()

    def stop_heartbeat(self) -> None:
        if self._hb_stop is not None:
            self._hb_stop.set()
        self._hb_stop = None
        self._hb_thread = None

    # -- lifecycle ----------------------------------------------------------
    def set_status(self, status: str, *, ended_at: str | None = None, summary: dict | None = None):
        if status in _TERMINAL_STATUSES:
            # Stop before the PATCH: once the intent is to end the run, a beat
            # racing the flip is noise (the server no-ops late beats anyway), and
            # if the PATCH itself fails the reaper finishing the job is correct.
            self.stop_heartbeat()
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


#: One definition, shared with the anchored-upload path in sdk/client.py — the hash
#: is part of the wire contract, so two copies could silently diverge.
_fingerprint = fingerprint
