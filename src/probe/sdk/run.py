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

import contextvars
import json
import os
import socket
import subprocess
import threading
import warnings
import weakref
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from . import errors
from . import snapshot as _snapshot
from . import unit_context
from .hashing import fingerprint, local_file_uri, reference_fields
from .unit_context import UnitContext
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


def _as_metric_value(value: Any) -> float | None:
    """``value`` as a metric point, or None if it belongs in the step record.

    ``metric_points.value`` is ``DOUBLE PRECISION NOT NULL`` (db/experiment/
    schema.sql), so this is a hard contract boundary, not a preference.

    Strings are excluded deliberately even though ``float("0.4")`` parses: a
    caller who logged ``"0.4"`` meant the string, and quietly retyping it would
    make it indistinguishable from the number afterwards. Bools DO become 1/0 —
    they are plottable, and a chart is what people log them for. Anything with a
    ``__float__`` (numpy scalars, 0-d torch tensors) coerces for free."""
    if isinstance(value, (str, bytes, bytearray)):
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_safe(key: str, value: Any) -> Any:
    """``value`` if it survives a JSON round trip, else its repr.

    ``spans.attributes`` is JSONB, so an unserialisable object would fail at
    encode time — INSIDE the training loop, past the fail-open boundary. Keeping
    the repr loses fidelity but never the loop, and says so."""
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        warnings.warn(
            f"metric {key!r} is not JSON-serialisable ({type(value).__name__}); "
            "recording repr() instead.",
            stacklevel=3,
        )
        return repr(value)


#: "The caller did not pass this at all", as distinct from an explicit ``None``.
#: Load-bearing for spans: the ATIF expander and the Harbor trial importer replay
#: STORED trajectories and pass ``started_at=<maybe None>`` deliberately, because a
#: stored step may have no usable timestamp. Defaulting those to ``now()`` would
#: write a fabricated time into a historical record. Omitting the argument — which
#: only live code does — is what opts into a default.
_UNSET: Any = object()

#: The span currently entered via ``with run.span(...)``, or None. A contextvar
#: rather than an attribute on Run: concurrent rollouts in threads or asyncio
#: tasks each get their own view, so they never adopt each other as parents. This
#: is span NESTING, distinct from unit_context's coords/labels contextvar.
_current_span: contextvars.ContextVar["SpanHandle | None"] = contextvars.ContextVar(
    "probe_current_span", default=None
)


class SpanHandle(str):
    """A span id that is also a context manager.

    Subclasses ``str`` because a span's handle IS its id: existing callers do
    ``span_id = run.span(...)`` and pass that string onward, so scope behaviour
    has to arrive without changing what :meth:`Run.span` returns.

        with run.span("rollout", name="rollout-0") as span:
            span.attributes["reward"] = reward

    Leaving the block upserts the same span id with ``ended_at``, a terminal
    status, and whatever accumulated in ``attributes``. An exception sets status
    ``failed`` and records its type and message.

    That last part is the reason this exists. A span opened with the two-call
    form and abandoned by a raise stays ``running`` forever: runs have a
    heartbeat and a server-side reaper, spans have neither, so nothing ever
    corrects it. The block closes the span on both paths.
    """

    def __new__(
        cls,
        span_id: str,
        *,
        run: "Run",
        span_type: str,
        fields: dict[str, Any],
        attributes: dict[str, Any],
    ) -> "SpanHandle":
        # str is variable-length, so no __slots__ here — these live in __dict__.
        self = super().__new__(cls, span_id)
        self._run = run
        self._span_type = span_type
        self._fields = fields
        self.attributes = attributes
        self._token: contextvars.Token | None = None
        return self

    # A span id used to be a plain ``str``, and plain strings survive being
    # copied, pickled, and shipped across a process boundary — which distributed
    # training does routinely (Ray, multiprocessing, a checkpoint state dict).
    # Reconstructing goes through ``__new__``, which needs a live Run and a
    # thread lock, so without these a copy raises. Degrade to the plain id: the
    # scope behaviour is meaningless in another process anyway.
    def __reduce__(self):
        return (str, (str(self),))

    def __copy__(self) -> str:
        return str(self)

    def __deepcopy__(self, memo: dict) -> str:
        return str(self)

    def __enter__(self) -> "SpanHandle":
        # No write here: `span()` already upserted the row as `running`, so the
        # span is visible for the whole time the body runs rather than appearing
        # only once it closes.
        self._token = _current_span.set(self)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._token is not None:
            _current_span.reset(self._token)
            self._token = None
        if exc_type is not None:
            # setdefault: an explicit attribute the body already set about the
            # failure is better information than the exception repr.
            self.attributes.setdefault("error.type", exc_type.__name__)
            message = str(exc)
            if message:
                self.attributes.setdefault("error.message", message)
        # `_fields` carries the RESOLVED parent, start time and coords, so this
        # upsert re-sends them verbatim instead of re-deriving them from a
        # contextvar that has already been reset (which would re-parent to the
        # grandparent). Re-sending an identical coords map is accepted — the
        # server's set-once rule 409s only on a DIFFERENT one.
        #
        # `attributes` is re-sent, not the copy span() sanitised — the body has
        # been assigning into it, so anything it added is unvetted. span() runs
        # it through _json_safe again on the way.
        self._run.span(
            self._span_type,
            id=str(self),
            status="failed" if exc_type is not None else "completed",
            ended_at=_now(),
            attributes=self.attributes,
            **self._fields,
        )
        # Returns None, so the exception keeps propagating. The span records that
        # it failed; it does not swallow the failure.


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
        self._hb_finalizer: weakref.finalize | None = None
        # Auto-update run lock. Held for the life of a process-bound run so an
        # upgrade cannot replace the installed tree mid-experiment; see
        # probe.cli.run_lock. None for detached runs, which use a lease instead.
        self._run_lock = None
        self._run_lock_leased = False
        # Auto-step counters, per metric kind. Guarded because logging from
        # several threads is ordinary (a sampler beside a training loop), and two
        # threads reading the same counter would put two points on one step.
        self._steps: dict[str, int] = {}
        self._steps_lock = threading.Lock()

    def _hold_run_lock(self, *, process_bound: bool) -> None:
        """Claim this box against an auto-update while the run is open.

        Two tiers, and which one applies is decided by whether a process of ours
        outlives this call. A process-bound run takes an flock the kernel
        releases on death -- including SIGKILL and OOM-kill -- so a crashed run
        can never wedge auto-update. A detached run has nothing alive to hold
        anything, so it gets a lease that its own subsequent writes renew.

        Never raises and never blocks. Failing to take the lock leaves the run
        unprotected, which is bad; failing to START the run because the lock
        could not be taken would be worse.
        """
        try:
            from probe.cli import run_lock
        except Exception:  # noqa: BLE001 -- SDK must not hard-depend on the CLI package
            return
        try:
            if process_bound:
                self._run_lock = run_lock.acquire(self.id)
            else:
                run_lock.touch_lease(self.id)
                self._run_lock_leased = True
        except Exception:  # noqa: BLE001 -- see docstring
            pass

    def _release_run_lock(self) -> None:
        """Drop the claim. Idempotent, never raises."""
        try:
            if self._run_lock is not None:
                self._run_lock.release()
                self._run_lock = None
            if self._run_lock_leased:
                from probe.cli import run_lock

                run_lock.clear_lease(self.id)
                self._run_lock_leased = False
        except Exception:  # noqa: BLE001
            pass

    def _next_step(self, kind: str) -> int:
        with self._steps_lock:
            step = self._steps.get(kind, 0)
            self._steps[kind] = step + 1
            return step

    def _note_step(self, kind: str, step: int) -> None:
        """Move the auto counter past an explicitly-given step.

        Without this, mixing ``log(step=i)`` with a bare ``log()`` would restart
        the auto sequence at 0 and stack a second set of points on steps the loop
        already used."""
        with self._steps_lock:
            self._steps[kind] = max(self._steps.get(kind, 0), step + 1)

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
    def description(self) -> str | None:
        return self._data.get("description")

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

    # -- below-run coordinates ----------------------------------------------
    def unit(
        self,
        *,
        coords: dict[str, Any] | None = None,
        labels: dict[str, Any] | None = None,
    ) -> UnitContext:
        """Ambient coordinate context for everything logged inside the block::

            with run.unit(coords={"rank": 0}, labels={"sample": 3}):
                run.log({"loss": 0.42}, step=12)   # dimensions/labels merged in
                with run.unit(labels={"uid": "p1"}):
                    ...                            # nested: child = parent ∪ child

        ``coords`` are the bounded grouping axes (SERIES identity: rank/split/...,
        never a per-sample id and never the step axis); ``labels`` are unbounded
        per-sample drill-down ids (POINT identity only). Nested units merge with
        the child winning per key; a key may not end up in both maps
        (``ValueError``, mirroring the server's 422). The context is contextvar-
        scoped: thread- and asyncio-task-local, restored on exit, and folded into
        payloads at call time so spooled writes replay with the coordinate that
        was ambient when the value was produced."""
        return UnitContext(coords=coords, labels=labels)

    # -- metrics ------------------------------------------------------------
    def log(
        self,
        metrics: dict[str, Any],
        *,
        step: int | None = _UNSET,
        kind: str = "model",
        wall_clock: str | None = None,
        dimensions: dict[str, Any] | None = None,
        labels: dict[str, Any] | None = None,
        span_id: str | None = None,
        agg: str | None = None,
        strict: bool | None = None,
    ):
        """Append metric points. Fail-open by default (spools on failure).

        ``dimensions`` is a bounded flat label map (<=8 keys); it widens the series
        identity to ``(run,kind,key,dims_hash)`` (fold #9). ``labels`` is the
        per-sample drill-down map (<=32 keys, POINT identity only) and ``span_id``
        an optional exemplar pointer to the span the value was produced under.
        Both maps merge over the ambient :meth:`unit` context (the explicit call
        site wins per key); a key in both maps raises ``ValueError``.
        Dimension-less points stay byte-identical. Built through the generated
        ``MetricBatch``/``MetricPointIn``, so schema drift fails here, not as a
        server 422.

        ``agg`` DECLARES the key's reduce fn (mean|sum|min|max|count) so a later
        grouped read can omit its own (server 0062: an omitted read-side ``agg``
        resolves to the declared one, else mean; conflicting declarations 422).
        The producer knows whether a count sums or a loss averages; declaring it
        at the write is what saves every reader from guessing."""
        numeric: dict[str, float] = {}
        other: dict[str, Any] = {}
        for key, value in metrics.items():
            as_number = _as_metric_value(value)
            if as_number is None:
                other[key] = _json_safe(key, value)
            else:
                numeric[key] = as_number

        # Split BEFORE drawing a step, so a call with nothing to write does not
        # consume one. `if metrics: run.log(metrics)` guards get written the
        # other way round, and burning an index there would drift the auto axis
        # away from the loop index — the exact failure auto-increment prevents.
        if not numeric and not other:
            return None

        if step is _UNSET:
            step = self._next_step(kind)
        elif step is not None:
            step = int(step)
            self._note_step(kind, step)

        dims, labs = unit_context.merged(dimensions, labels)
        result = None
        if not numeric:
            body = None
        else:
            batch = MetricBatch(
                points=[
                    MetricPointIn(
                        key=key,
                        kind=kind,
                        value=value,
                        step_index=step,
                        wall_clock=wall_clock,
                        dimensions=dims,
                        labels=labs or None,
                        span_id=span_id,
                    )
                    for key, value in numeric.items()
                ]
            )
            body = batch.model_dump(mode="json", exclude_none=True)
        # The generated MetricPointIn predates the 0062 `agg` field, so the
        # declaration rides in after validation; None stays off the wire so a
        # declaration-less point is byte-identical to what it always was.
        if body is not None:
            if agg is not None:
                for point in body["points"]:
                    point["agg"] = agg
            # Returned even when a step record is also written: callers key off
            # this to tell "confirmed" from "spooled" (connectors/harbor.py).
            result = self._client.write(
                "POST", f"/v1/runs/{self.id}/metrics", body, strict=strict
            )

        if other:
            if step is None:
                warnings.warn(
                    f"dropped non-numeric {sorted(other)} — a step record needs a step "
                    "index, and step=None was passed explicitly to mean no step axis. "
                    "Omit step= to auto-increment, or pass one.",
                    stacklevel=2,
                )
            elif kind != "model":
                # StepCreate has no `kind` — a step record is keyed on
                # (run, step_index) alone. So a hardware value at hardware-step 3
                # would merge into the model loop's record at step 3. Numeric
                # hardware metrics are unaffected: those are metric points, where
                # kind IS part of the series identity.
                warnings.warn(
                    f"dropped non-numeric {sorted(other)} from a {kind!r} log — step "
                    "records are keyed by step index alone, with no kind, so these "
                    "would overwrite the model loop's record at the same step.",
                    stacklevel=2,
                )
            else:
                step_result = self.step(step, attributes=other, strict=strict)
                # Only stand in when there was NOTHING numeric to write. A
                # successful step record must never mask a metrics write that
                # spooled: harbor.py reads None as "spooled".
                if not numeric:
                    result = step_result
        return result

    def log_hw(
        self,
        metrics: dict[str, Any],
        *,
        step: int | None = _UNSET,
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

    # -- metrics / coordinates (read) ---------------------------------------
    def grouped_metrics(self, key: str, **kw: Any) -> dict:
        """Server-side reduce/group over this run's points; see
        :meth:`Client.get_metrics_grouped` for the parameters and paging."""
        return self._client.get_metrics_grouped(self.id, key, **kw)

    def wide_metrics(self, **kw: Any) -> dict:
        """Step x metric table for this run; see :meth:`Client.get_metrics_wide`."""
        return self._client.get_metrics_wide(self.id, **kw)

    def export_points(self, **kw: Any):
        """Lossless raw-point generator for this run; see
        :meth:`Client.export_metric_points`."""
        return self._client.export_metric_points(self.id, **kw)

    def coordinates(self) -> list[dict]:
        """This run's coordinate catalog (0060); see
        :meth:`Client.list_run_coordinates`."""
        return self._client.list_run_coordinates(self.id)

    # -- trajectory (spans) -------------------------------------------------
    def span(
        self,
        span_type: str,
        *,
        id: str | None = None,
        parent_span_id: str | None = _UNSET,
        name: str | None = None,
        step_index: int | None = None,
        external_key: str | None = None,
        provider: str | None = None,
        status: str = "running",
        started_at: str | None = _UNSET,
        ended_at: str | None = None,
        attributes: dict | None = None,
        summary: dict | None = None,
        coords: dict[str, Any] | None = None,
        strict: bool | None = None,
    ) -> "SpanHandle":
        """Upsert one span (client-generated UUID). Returns the span id.

        ``coords`` is the span's below-run coordinate — the same bounded map
        metric points carry in ``dimensions`` — merged over the ambient
        :meth:`unit` context (call site wins per key). Sent as the dedicated
        ``coords`` field, never folded into ``attributes``: the server
        canonicalizes + hashes it (and mirrors it for display) itself. A span's
        coordinate is set-once server-side — a re-push may add one, an empty map
        keeps the existing coordinate, and a different one is a 409."""
        span_id = id or str(uuid4())
        UUID(span_id)  # validate shape early
        if parent_span_id is _UNSET:
            enclosing = _current_span.get()
            # Only adopt a parent from the SAME run. `with runA.span(...):` around
            # a `runB.span(...)` would otherwise write runB a parent_span_id that
            # does not exist in runB — spans are per-run, and a dangling FK is a
            # worse record than no parent.
            parent_span_id = (
                str(enclosing)
                if enclosing is not None and enclosing._run.id == self.id
                else None
            )
        if started_at is _UNSET:
            # Only when CREATING. An explicit `id=` means this is an upsert of an
            # existing span — the documented two-call close does exactly that —
            # and stamping now() there would rewrite the start time to the close
            # time and collapse the span's duration to zero.
            started_at = _now() if id is None else None
        # Through _json_safe for the same reason log() is: `attributes` is JSONB,
        # so an unserialisable value blows up in model_dump() BEFORE the strict/
        # spool boundary — inside the training loop. Worse in the `with` form,
        # where the raise happens during unwinding and displaces the body's own
        # exception as the visible failure.
        attrs = {key: _json_safe(key, value) for key, value in (attributes or {}).items()}
        resolved_coords = unit_context.merged_coords(coords)
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
            attributes=attrs,
            summary=summary or {},
            # Always a dict, never None: the span body serializes without
            # exclude_none, and the server's coords field is non-nullable with
            # {} meaning "no coordinate stated" (keeps any existing one).
            coords=resolved_coords,
        )
        body = SpanBatch(spans=[span]).model_dump(mode="json")
        self._client.write("POST", f"/v1/runs/{self.id}/spans", body, strict=strict)
        return SpanHandle(
            span_id,
            run=self,
            span_type=span_type,
            fields={
                "parent_span_id": parent_span_id,
                "name": name,
                "step_index": step_index,
                "external_key": external_key,
                "provider": provider,
                "started_at": started_at,
                "summary": summary,
                # The RESOLVED coordinate, so the close re-sends the same map the
                # open did. Identical coords are accepted; the server's set-once
                # rule 409s only on a different one.
                "coords": resolved_coords,
                "strict": strict,
            },
            attributes=attrs,
        )

    def step(self, step_index: int, *, name: str | None = None, strict: bool | None = None, **kw):
        """Upsert the step record. The per-step home for anything that is not a
        number, which is what :meth:`log` routes non-numeric values into.

        ``strict`` is forwarded so this obeys fail-open like every other write —
        it used to swallow the argument and always take the client default."""
        body = {"step_index": step_index, "name": name, **kw}
        return self._client.write(
            "POST", f"/v1/runs/{self.id}/steps", body, strict=strict
        )

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
        coords: dict[str, Any] | None = None,
        labels: dict[str, Any] | None = None,
        strict: bool | None = None,
    ):
        """Record an artifact.

        ``coords``/``labels`` are the below-run coordinate maps (same split as
        :meth:`log`), merged over the ambient :meth:`unit` context and sent as
        top-level ``ArtifactCreate`` fields — the server hashes coords into the
        cross-table join key and mirrors both maps into ``meta`` for display, so
        the client never folds them into ``meta`` itself. The presign *uploads*
        door does not accept them (its request model has no such fields), so a
        byte upload records them only if it falls back to a reference artifact.

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
        # Resolve the coordinate at CALL time (the fail-open spool replays this
        # payload later; the ambient unit must not be re-read at flush time).
        coords, labels = unit_context.merged(coords, labels)
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
                coords=coords,
                labels=labels,
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
            coords=coords or None,
            labels=labels or None,
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

    def reconcile_artifact(self, name: str, content_hash: str) -> dict | None:
        """This run's already-recorded artifact with ``name`` AND ``content_hash``.

        OPT-IN helper: the SDK's own ``log_artifact`` does NOT call this yet, so a
        caller that retries artifact creation must invoke it explicitly.

        A proxy in front of the API can return 502 AFTER the write has landed, so
        the response is lost while the artifact exists. A blind retry then records
        the same bytes twice, and later selection
        (``next(a for a in arts if a["kind"] == "checkpoint")``) silently chooses
        between duplicates.

        Matching on the content hash makes this exact rather than a guess:
        artifacts are content-addressed, so identical hash under the same name on
        the same run IS the same artifact.
        """
        if not content_hash:
            return None
        for row in self._client.list_run_artifacts(self.id, name=name, scope="own") or []:
            if row.get("content_hash") == content_hash:
                return row
        return None

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
        coords: dict[str, Any] | None = None,
        labels: dict[str, Any] | None = None,
        strict: bool | None = None,
    ):
        """presign -> PUT -> confirm. Fail-open: on failure (and not strict) falls
        back to recording a hash+metadata reference so the training loop is unblocked.

        ``coords``/``labels`` (already merged with the ambient unit by the caller)
        ride only the fallback ``ArtifactCreate``: the presign ``UploadRequest``
        model has no coordinate fields server-side, so sending them there would be
        silently dropped at best and a 422 on a stricter model at worst."""
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
                coords=coords or None,
                labels=labels or None,
            )
            return self._client.write(
                "POST",
                f"/v1/runs/{self.id}/artifacts",
                fallback.model_dump(mode="json", exclude_none=True),
                strict=False,
            )

    # -- tags ----------------------------------------------------------------
    @property
    def tags(self) -> list[str]:
        return list(self._data.get("tags") or [])

    def set_tags(self, tags: list[str], *, strict: bool | None = None):
        """REPLACE the run's whole tag list ([] clears). The server normalizes
        to lowercase-kebab and 422s past the caps (CONTRACT.md "tags").
        Granular add/remove is the CLI ``probe run tag`` verb's
        read-modify-write job, not a server op.

        A pre-0066 backend would silently drop the field and 200 the old row;
        the response is verified so that no-op cannot masquerade as success.
        A spooled fail-open write returns None (unverifiable until flush)."""
        data = self._client.write(
            "PATCH", f"/v1/runs/{self.id}", {"tags": list(tags)}, strict=strict
        )
        if data:
            self._client._verify_tags_written(list(tags), data, "PATCH /v1/runs/{id}")
            self._data = data
        return data

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
        (fold #7 + the RunPatch env_ref parity), the same column the ingest path sets.

        The git shadow ref is provenance, NOT the code record. It lives in the
        object database of whatever machine ran the job, so on an ephemeral box it
        stops resolving the moment that box is destroyed. ``capture_manifest``
        decides per file whether the content is already retrievable from a pushed
        remote; anything that is not gets its bytes uploaded by the caller against
        ``manifest['entries']``."""
        git = _snapshot.capture_git_snapshot(self.id, cwd)
        manifest = _snapshot.capture_manifest(cwd)
        record = ExecutionRecordCreate(
            code={"git": git, "manifest": manifest},
            deps=_snapshot.capture_env(
                strict=strict if strict is not None else not self._client.fail_open
            ) if include_env else {},
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
        # Record the shadow commit as a reference artifact for lineage. The
        # manifest travels with it so a reader can tell, without fetching
        # anything, which files this reference can actually still supply.
        self.log_artifact(
            "code-snapshot",
            uri=f"git:{git['ref']}#{git['commit']}",
            kind="code_snapshot",
            is_reference=True,
            meta={
                "branch": git.get("branch"),
                "dirty": git.get("dirty"),
                "env_ref": content_hash,
                "tree_sha256": manifest["tree_sha256"],
                "base_commit": manifest["base_commit"],
                "remote": manifest["remote"],
                "n_git_referenced": manifest["n_git_referenced"],
                "n_pending_upload": manifest["n_pending_upload"],
            },
            strict=strict,
        )
        return {
            "git": git,
            "manifest": manifest,
            "execution_record": exec_rec,
            "content_hash": content_hash,
        }

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

        Interval precedence: explicit argument, then PROBE_HEARTBEAT_SECONDS,
        then the 60s default. Non-positive disables.

        The thread also stops when this handle is garbage-collected (an
        abandoned run's process may still be alive, but nobody can ever finish
        it — letting the reaper flip it to 'crashed' is the honest outcome) and
        when the owning ``Client`` closes (beats ride its transport).
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
        # finalize holds the Event (its callback arg), never the Run, so the
        # handle stays collectable and its collection is what ends the beat.
        self._hb_finalizer = weakref.finalize(self, stop.set)
        self._client._register_run_heartbeat(stop)
        thread.start()

    def stop_heartbeat(self) -> None:
        if self._hb_stop is not None:
            self._hb_stop.set()
        if self._hb_finalizer is not None:
            self._hb_finalizer.detach()
        self._hb_stop = None
        self._hb_thread = None
        self._hb_finalizer = None

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
        """Close the run. Flushes any journaled writes first, and refuses to
        mark the run terminal while any of its own outbox ops remain
        undelivered or dead-lettered -- the CLI's run-end barrier exists in
        the SDK too (red team: finish() used to discard the drain outcome and
        close 'completed' over silently missing data)."""
        self._client.flush()
        journal = self._client.journal
        blocked = [
            op
            for _, op in journal.pending() + journal.failed()
            if op.get("run_ref") == self.id
        ]
        if blocked:
            # Deliberately BEFORE the raise is not where the lock is released:
            # a run that refuses to close is still open, and still has to keep
            # the box claimed against an upgrade.
            raise errors.RosError(
                f"run {self.id} not closed: {len(blocked)} outbox op(s) are "
                "undelivered or dead-lettered — see `probe outbox status`, "
                "fix or `probe outbox retry`, then finish again"
            )
        result = self.set_status(status, ended_at=_now(), summary=summary)
        # ONLY on success, and deliberately not in a `finally`.
        #
        # If set_status raised, the run is not closed: the caller may catch the
        # error and keep training, and releasing here would hand auto-update a
        # green light over a live process. Holding costs nothing -- an flock is
        # bound to this process and the kernel frees it at exit regardless, so
        # the failure mode of keeping it is "protected slightly too long", while
        # the failure mode of dropping it is an upgrade landing mid-run.
        self._release_run_lock()
        return result

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
