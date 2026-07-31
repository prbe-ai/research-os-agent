"""SDK-owned capture facade for Harbor bridges — attach()/finalize() in ~3 lines.

Every Harbor bridge/server that wants Probe capture used to re-implement the
same glue inline: hook install, sandbox-identifier retention, sandbox-state
recording, staging via :func:`probe.connectors.harbor.stage_trial_export`, and
capture-mode gating.  This module is that glue, owned once:

.. code-block:: python

    from probe.connectors import harbor_capture

    handle = harbor_capture.attach(
        trial,
        correlation={"session_id": sid, "task_id": task},
        context={"mix": "swe"},
        capture_mode="shadow",
        sandbox_state=SandboxStateOptions(),   # or None to skip snapshots
    )
    try:
        result = await trial.run()
    finally:
        capture = await handle.finalize(trial_dir)

Capture modes (``off`` / ``shadow`` / ``required``):

* ``off`` — ``attach`` installs nothing and ``finalize`` returns a
  ``not_attempted`` result without touching the filesystem.
* ``shadow`` — best-effort.  Staging failures are recorded on the returned
  :class:`HarborCaptureResult` (``status="failed"``, ``error=...``), never
  raised, and must never influence the trial's own outcome.
* ``required`` — staging behaves exactly like ``shadow`` (record, don't
  raise: the trial result still has to make it back to the caller), but the
  caller is expected to gate on the result — ``capture.complete`` or
  ``capture.raise_if_incomplete()`` — and fail its response when the capture
  did not complete.

Correlation discipline: Harbor keeps provider handles private, so
:class:`SandboxCorrelationCapture` only ever reads *stable string identifiers*
from the per-backend private attributes (Daytona/E2B ``_sandbox``, Modal
``_sandbox.object_id``, Runloop ``_devbox.id``) while the environment is
alive, and retains the resulting strings so they survive Harbor nulling the
environment handle after the trial.  It never calls provider methods and never
keeps the handle.  These reads are deliberately best-effort (``None`` when a
backend has nothing) and therefore NOT part of ``verify_harbor_contract()``.

Harbor stays an optional lazy dependency: ``attach`` with a non-``off`` mode
runs :func:`probe.connectors.harbor_runner.verify_harbor_contract` first —
loud at setup (``HarborNotInstalledError`` / ``HarborContractError``), fail-
open ever after — and only then imports ``harbor.trial.hooks``.  The staging
path itself (``finalize``) is pure trial-directory bytes and needs no harbor.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .harbor import stage_trial_export
from .harbor_runner import (
    HARBOR_TESTED_AGAINST,
    HarborContractError,
    SandboxStateOptions,
    SandboxStateRecorder,
    verify_harbor_contract,
)

logger = logging.getLogger(__name__)

CAPTURE_MODES = frozenset({"off", "shadow", "required"})

#: The trial-tree files a Harbor trial always writes; staging marks the
#: capture ``partial`` when any of them is missing.
EXPECTED_TRIAL_FILES = ("config.json", "lock.json", "result.json")


class CaptureIncompleteError(RuntimeError):
    """A ``required``-mode capture did not complete; fail the trial response."""


# ---------------------------------------------------------------------------
# Sandbox correlation: logical session id + best-effort provider id.
# ---------------------------------------------------------------------------
def _nonempty_identifier(value: Any) -> str | None:
    """Normalize an SDK identifier without invoking provider methods."""
    if value is None or callable(value):
        return None
    text = str(value).strip()
    return text or None


def _safe_identifier_attr(value: Any, attribute: str) -> str | None:
    try:
        return _nonempty_identifier(getattr(value, attribute, None))
    except Exception:  # noqa: BLE001 — provider SDK properties are best-effort
        return None


def _provider_sandbox_id(environment: Any) -> str | None:
    """Read a live provider ID across Harbor's supported cloud backends.

    Harbor intentionally keeps provider handles private.  This only reads
    stable SDK identifiers while the environment is alive and retains the
    resulting string; it never calls provider methods or keeps the handle.
    """
    if environment is None:
        return None

    for attribute in ("provider_sandbox_id", "provider_ref"):
        if provider_id := _safe_identifier_attr(environment, attribute):
            return provider_id

    # Daytona and E2B use _sandbox (id and sandbox_id respectively), Modal
    # uses _sandbox.object_id, and Runloop uses _devbox.id.
    for handle_attribute in ("_sandbox", "_devbox"):
        try:
            handle = getattr(environment, handle_attribute, None)
        except Exception:  # noqa: BLE001 — provider SDK properties are best-effort
            continue
        if handle is None:
            continue
        for identifier_attribute in ("sandbox_id", "object_id", "id"):
            if provider_id := _safe_identifier_attr(handle, identifier_attribute):
                return provider_id
    return None


def sandbox_correlation(trial: Any) -> tuple[str | None, str | None]:
    """Return Harbor's logical sandbox ID and a live best-effort provider ID."""
    environment = getattr(trial, "agent_environment", None)
    return (
        _safe_identifier_attr(environment, "session_id"),
        _provider_sandbox_id(environment),
    )


class SandboxCorrelationCapture:
    """Retain sandbox identifiers before Harbor clears provider handles.

    ``AGENT_START``/``AGENT_END`` hooks read the identifiers while the
    environment is live and keep the strings; :meth:`resolved` prefers the
    retained values with a post-run read as fallback for Harbors that leave
    the handle in place.
    """

    def __init__(self, trial: Any) -> None:
        self._trial = trial
        self.sandbox_id: str | None = None
        self.provider_sandbox_id: str | None = None

    def install(self) -> None:
        from harbor.trial.hooks import TrialEvent

        self._trial.add_hook(TrialEvent.AGENT_START, self._capture)
        self._trial.add_hook(TrialEvent.AGENT_END, self._capture)

    async def _capture(self, _event: Any = None) -> None:
        sandbox_id, provider_sandbox_id = sandbox_correlation(self._trial)
        self.sandbox_id = sandbox_id or self.sandbox_id
        self.provider_sandbox_id = provider_sandbox_id or self.provider_sandbox_id

    def resolved(self) -> tuple[str | None, str | None]:
        """Prefer retained live values, with a post-run fallback read."""
        sandbox_id, provider_sandbox_id = sandbox_correlation(self._trial)
        return (
            self.sandbox_id or sandbox_id,
            self.provider_sandbox_id or provider_sandbox_id,
        )


# ---------------------------------------------------------------------------
# Capture result + handle.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HarborCaptureResult:
    """What one ``finalize`` produced — identifiers, staging output, verdict.

    ``status`` is ``complete`` / ``partial`` (the staged capture manifest's
    completeness state), ``failed`` (staging raised; see ``error``), or
    ``not_attempted`` (capture mode ``off``).
    """

    status: str = "not_attempted"
    staged_trial_dir: str | None = None
    archive_path: str | None = None
    manifest_path: str | None = None
    export_descriptor_path: str | None = None
    archive_content_hash: str | None = None
    external_key: str | None = None
    file_count: int = 0
    size_bytes: int = 0
    sandbox_id: str | None = None
    provider_sandbox_id: str | None = None
    sandbox_state: dict[str, Any] | None = None
    # True iff this trial archived (and verified) begin-state bytes — the signal
    # a bridge's per-task election reads to decide whether the shared archive for
    # this task was produced (no need to re-read the authored bundle from disk).
    begin_bytes_captured: bool = False
    error: str | None = None

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    def raise_if_incomplete(self) -> None:
        """The ``required``-mode gate: raise unless the capture completed."""
        if not self.complete:
            raise CaptureIncompleteError(
                f"harbor capture status is {self.status!r}"
                + (f": {self.error}" if self.error else "")
            )


def _capture_directory_name(trial_id: str) -> str:
    """Return a collision-resistant basename even for unusual provider IDs."""
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", trial_id).strip("._-")[:80] or "trial"
    suffix = hashlib.sha256(trial_id.encode()).hexdigest()[:12]
    return f"{slug}-{suffix}"


class CaptureHandle:
    """One trial's attached capture state; produced by :func:`attach`.

    Exposes the pieces a bridge folds into its own response: the retained
    ``sandbox_ids``, the live ``sandbox_state`` recorder, install-time
    ``errors``, and — after :meth:`finalize` — the ``result``.
    """

    def __init__(
        self,
        trial: Any,
        *,
        capture_mode: str,
        correlation: dict[str, Any],
        context: dict[str, Any],
        sandbox_state: SandboxStateRecorder | None = None,
        correlation_capture: SandboxCorrelationCapture | None = None,
    ) -> None:
        self._trial = trial
        self.capture_mode = capture_mode
        self.correlation = correlation
        self.context = context
        self.sandbox_state = sandbox_state
        self.correlation_capture = correlation_capture
        self.errors: list[str] = []
        self.result: HarborCaptureResult | None = None

    @property
    def sandbox_ids(self) -> tuple[str | None, str | None]:
        """``(sandbox_id, provider_sandbox_id)`` — retained values preferred."""
        if self.correlation_capture is not None:
            return self.correlation_capture.resolved()
        return sandbox_correlation(self._trial)

    async def finalize(
        self,
        trial_dir: str | Path,
        *,
        capture_dir: str | Path | None = None,
        run_id: str | None = None,
        step_index: int | None = None,
        environment: dict[str, Any] | None = None,
        expected_paths: tuple[str, ...] = EXPECTED_TRIAL_FILES,
        external_key: str | None = None,
        create_archive: bool = True,
    ) -> HarborCaptureResult:
        """Stage the trial tree into a ``probe-harbor-export/1`` bundle.

        Runs after ``trial.run()`` returned (or died) — call it from the same
        ``finally`` that owns the trial.  Never raises except
        ``asyncio.CancelledError``: a staging failure comes back as
        ``status="failed"`` so a ``shadow``-mode bridge stays untouched and a
        ``required``-mode bridge can still return the trial result while
        failing its response (``result.raise_if_incomplete()``).

        ``capture_dir`` defaults to a ``<trials_dir>-captures`` sibling of the
        trial's parent directory; the destination inside it is derived from the
        trial id, collision-resistant for unusual provider IDs.
        """
        if self.capture_mode == "off":
            self.result = HarborCaptureResult()
            return self.result

        trial_path = Path(trial_dir).expanduser().resolve()
        sandbox_id, provider_sandbox_id = self.sandbox_ids
        summary = (
            self.sandbox_state.summary() if self.sandbox_state is not None else None
        )
        begin_bytes_captured = (
            self.sandbox_state.begin_bytes_captured()
            if self.sandbox_state is not None
            else False
        )

        trial_id = str(
            self.correlation.get("trial_id")
            or _nonempty_identifier(getattr(self._trial, "id", None))
            or trial_path.name
        )
        correlation = {**self.correlation, "trial_id": trial_id}
        context = dict(self.context)
        if summary is not None:
            context["sandbox_state"] = summary
        environment_value = dict(environment or {})
        environment_value.setdefault("sandbox_id", sandbox_id)
        environment_value.setdefault("provider_sandbox_id", provider_sandbox_id)
        environment_value.setdefault(
            "collected",
            {
                "native_trial_directory": True,
                "staged_after_trial_run_returned": True,
            },
        )

        root = (
            Path(capture_dir).expanduser().resolve()
            if capture_dir is not None
            else trial_path.parent.with_name(f"{trial_path.parent.name}-captures")
        )
        destination = root / _capture_directory_name(trial_id)

        try:
            staged = await asyncio.to_thread(
                stage_trial_export,
                trial_path,
                destination,
                run_id=run_id,
                step_index=step_index,
                environment=environment_value,
                correlation=correlation,
                context=context,
                expected_paths=expected_paths,
                external_key=external_key,
                # Expansion happens in the network-side watcher after durable
                # staging. Request it by default so recognized trajectories
                # become dashboard turn/tool spans without a manual repair;
                # the raw trajectory artifact remains authoritative.
                expand=True,
                create_archive=create_archive,
            )
            manifest = json.loads(staged.capture_manifest_path.read_text())
            files = (
                manifest.get("files")
                if isinstance(manifest.get("files"), list)
                else []
            )
            capture = (
                manifest.get("capture")
                if isinstance(manifest.get("capture"), dict)
                else {}
            )
            archive = (
                capture.get("archive")
                if isinstance(capture.get("archive"), dict)
                else {}
            )
            completeness = (
                capture.get("completeness")
                if isinstance(capture.get("completeness"), dict)
                else {}
            )
            descriptor_correlation = staged.descriptor.get("correlation") or {}
            result = HarborCaptureResult(
                status=str(completeness.get("status") or "partial"),
                staged_trial_dir=str(staged.staged_trial.trial_dir),
                archive_path=(
                    str(staged.archive_path)
                    if staged.archive_path is not None
                    else None
                ),
                manifest_path=str(staged.capture_manifest_path),
                export_descriptor_path=str(staged.request_path),
                archive_content_hash=archive.get("content_hash"),
                external_key=descriptor_correlation.get("external_key"),
                file_count=len(files),
                size_bytes=sum(
                    item.get("size_bytes", 0)
                    for item in files
                    if isinstance(item, dict)
                    and isinstance(item.get("size_bytes"), int)
                ),
                sandbox_id=sandbox_id,
                provider_sandbox_id=provider_sandbox_id,
                sandbox_state=summary,
                begin_bytes_captured=begin_bytes_captured,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — recorded, never raised (see docstring)
            logger.exception("failed to stage harbor trial capture %s", trial_id)
            result = HarborCaptureResult(
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                sandbox_id=sandbox_id,
                provider_sandbox_id=provider_sandbox_id,
                sandbox_state=summary,
                begin_bytes_captured=begin_bytes_captured,
            )
        self.result = result
        return result


# ---------------------------------------------------------------------------
# The facade entry point.
# ---------------------------------------------------------------------------
def attach(
    trial: Any,
    *,
    correlation: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    capture_mode: str = "shadow",
    sandbox_state: SandboxStateOptions | None = None,
) -> CaptureHandle:
    """Attach Probe capture to a harbor ``Trial`` you own; returns the handle.

    ``correlation`` and ``context`` are opaque JSON-safe dicts folded into the
    staged export (native run/rollout/sample identifiers belong in
    ``correlation``; free-form producer context in ``context``).  With
    ``capture_mode="off"`` this is a no-op handle: nothing is installed,
    harbor is never imported, and ``finalize`` returns ``not_attempted``.

    Otherwise the pinned harbor API surface is verified first — loudly, per
    :func:`probe.connectors.harbor_runner.verify_harbor_contract` — then the
    correlation hooks and (when ``sandbox_state`` options are given) the
    ``probe.sandbox-state/1`` recorder hooks are installed fail-open: an
    install failure is recorded on ``handle.errors`` / the recorder's summary
    and must never block the trial.
    """
    if capture_mode not in CAPTURE_MODES:
        raise ValueError(
            "capture_mode must be one of: " + ", ".join(sorted(CAPTURE_MODES))
        )
    correlation_value = dict(correlation or {})
    context_value = dict(context or {})
    if capture_mode == "off":
        return CaptureHandle(
            trial,
            capture_mode=capture_mode,
            correlation=correlation_value,
            context=context_value,
        )

    problems = verify_harbor_contract()
    if problems:
        raise HarborContractError(
            "installed harbor does not match the pinned API surface "
            f"(tested against {HARBOR_TESTED_AGAINST}): " + "; ".join(problems)
        )
    from harbor.trial.hooks import TrialEvent

    handle = CaptureHandle(
        trial,
        capture_mode=capture_mode,
        correlation=correlation_value,
        context=context_value,
    )

    correlation_capture = SandboxCorrelationCapture(trial)
    try:
        correlation_capture.install()
        handle.correlation_capture = correlation_capture
    except Exception as exc:  # noqa: BLE001 — metadata must never block a trial
        logger.exception("sandbox-correlation hook install failed")
        handle.errors.append(
            f"correlation hook install failed: {type(exc).__name__}: {exc}"
        )

    if sandbox_state is not None:
        recorder = SandboxStateRecorder(trial, sandbox_state)
        handle.sandbox_state = recorder
        try:
            trial.add_hook(TrialEvent.AGENT_START, recorder.on_agent_start)
            trial.add_hook(TrialEvent.AGENT_END, recorder.on_agent_end)
        except Exception as exc:  # noqa: BLE001 — capture is best-effort, trial must run
            logger.exception("sandbox-state hook install failed")
            recorder.record_install_failure(exc)
            handle.errors.append(
                f"sandbox-state hook install failed: {type(exc).__name__}: {exc}"
            )
    return handle


__all__ = [
    "CAPTURE_MODES",
    "EXPECTED_TRIAL_FILES",
    "CaptureHandle",
    "CaptureIncompleteError",
    "HarborCaptureResult",
    "SandboxCorrelationCapture",
    "attach",
    "sandbox_correlation",
]
