"""Harbor trial capture — Phase 1 of the Harbor-native ownership plan
(docs/2026-07-15-harbor-native-ownership-plan.md).

``parse_trial`` reads a Harbor trial directory using only the on-disk output
contract (``config.json`` / ``lock.json`` / ``result.json`` / ``reward.json`` /
``trajectory.json`` / ``logs/**`` / ``output/**``) — the layer Harbor's trial
runner owns regardless of which environment provider (Docker, Daytona, Modal,
E2B, GKE, Runloop, or a private fork) produced the sandbox. Everything is
optional; unrecognized files pass through with ``role: "other"`` so forked
layouts are captured, never rejected.

``capture_trial`` turns one trial into run-native records:

  rollout span (trial identity + phases)   -> POST /v1/runs/{id}/spans
  verifier reward at the training step     -> POST /v1/runs/{id}/metrics
  every file, CAS-uploaded + labeled       -> presign flow (kind/meta, Phase 0)
  one ``kind="harbor_trial"`` manifest     -> POST /v1/runs/{id}/artifacts

This is the join Osmosis is missing: ``step_index`` ties the trial (and every
byte in it) to the training step, so "look at the sandbox at steps 599..601"
becomes ``client.list_run_artifacts(run_id, kind="harbor_trial", step_from=599,
step_to=601)``.

Trajectory contents are stored raw always; recognized formats (ATIF built in,
forks register their own — see ``probe.connectors.atif``) are additionally
expanded into turn/tool_call spans under the rollout span at capture time.
Unknown formats stay raw-only and can be expanded retroactively once a parser
exists (``probe trial expand``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tarfile
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from . import atif
from ..sdk.capture import (
    CaptureLedger,
    CaptureState,
    stable_external_key,
    stable_span_id,
)
from ..sdk.durable import (
    fsync_directory as _fsync_directory,
    now_iso as _utc_now,
    write_text_atomic,
)

if TYPE_CHECKING:
    from ..sdk.run import Run

SCHEMA_VERSION = "1.0"
MANIFEST_KIND = "harbor_trial"
CAPTURE_LEDGER_NAME = ".probe-capture.json"
EXPORT_SCHEMA_VERSION = "probe-harbor-export/1"
EXPORT_CONNECTOR = "probe.connectors.harbor.capture_trial"
EXPORT_MANIFEST_NAME = "capture-manifest.json"
EXPORT_REQUEST_NAME = "export-request.json"
EXPORT_TRIAL_DIR_NAME = "trial"
EXPORT_ARCHIVE_NAME = "trial.tar.gz"

#: Known manifest roles (plan schema v1). Anything else is "other".
ROLES = (
    "config",
    "lock",
    "result",
    "trajectory",
    "reward",
    "agent_log",
    "verifier",
    "output",
    "other",
)

_TOP_LEVEL_ROLES = {
    "config.json": "config",
    "lock.json": "lock",
    "result.json": "result",
    "reward.json": "reward",
    "trajectory.json": "trajectory",
}


def role_for(relative_path: str | PurePosixPath) -> str:
    """Map a trial-relative path to a manifest role. Fork-tolerant: unknown -> other."""
    rel = PurePosixPath(relative_path)
    parts = rel.parts
    if len(parts) == 1 and parts[0] in _TOP_LEVEL_ROLES:
        return _TOP_LEVEL_ROLES[parts[0]]
    head = parts[0]
    if head == "logs" and len(parts) > 1:
        if parts[1] == "agent":
            return "agent_log"
        if parts[1] == "verifier":
            return "verifier"
    if head == "agent":
        return "agent_log"
    if head == "verifier":
        return "verifier"
    if head == "output":
        return "output"
    return "other"


def _load_json(path: Path) -> Any | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _trial_files(root: Path) -> list[Path]:
    """The byte scope captured by both parsing and durable staging.

    Symlinks and our own mutable ledger are inventoried but never uploaded.
    Other dotfiles are ordinary producer output: private Harbor forks may use
    them for state that is not represented anywhere else.
    """

    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name != CAPTURE_LEDGER_NAME
    )


@dataclass
class ParsedTrial:
    """A trial directory reduced to its contract fields. Every field except
    ``name``/``files`` may be None — private forks owe us nothing."""

    trial_dir: Path
    name: str
    files: list[Path] = field(default_factory=list)
    config: dict | None = None
    result: dict | None = None
    reward: float | None = None
    trajectory_format: str | None = None
    trajectory: dict | None = None

    @property
    def task_name(self) -> str | None:
        if isinstance(self.result, dict) and self.result.get("task_name"):
            return self.result["task_name"]
        if isinstance(self.config, dict):
            task = self.config.get("task")
            if isinstance(task, dict):
                return task.get("name") or task.get("task_name")
        return None

    @property
    def agent_info(self) -> dict | None:
        if isinstance(self.result, dict) and isinstance(
            self.result.get("agent_info"), dict
        ):
            return self.result["agent_info"]
        return None

    @property
    def phases(self) -> dict:
        """The four TrialResult phase timings (whatever subset exists)."""
        if not isinstance(self.result, dict):
            return {}
        out = {}
        for phase in (
            "environment_setup",
            "agent_setup",
            "agent_execution",
            "verifier",
        ):
            timing = self.result.get(phase)
            if isinstance(timing, dict):
                out[phase] = {k: timing.get(k) for k in ("started_at", "finished_at")}
        return out

    @property
    def exception(self) -> dict | None:
        if isinstance(self.result, dict) and isinstance(
            self.result.get("exception_info"), dict
        ):
            return self.result["exception_info"]
        return None

    @property
    def started_at(self) -> str | None:
        return self.result.get("started_at") if isinstance(self.result, dict) else None

    @property
    def ended_at(self) -> str | None:
        return self.result.get("finished_at") if isinstance(self.result, dict) else None


@dataclass
class StagedTrial:
    """A Harbor trial whose scoped bytes have been copied to durable storage."""

    trial_dir: Path
    ledger: CaptureLedger

    @property
    def durable_collection_complete(self) -> bool:
        return self.ledger.report()["collection"]["state"] == "complete"


@dataclass(frozen=True)
class StagedTrialExport:
    """An atomically published producer bundle ready for the export watcher."""

    root: Path
    staged_trial: StagedTrial
    capture_manifest_path: Path
    request_path: Path
    descriptor: dict[str, Any]
    archive_path: Path | None = None

    @property
    def durable_collection_complete(self) -> bool:
        return self.staged_trial.durable_collection_complete


def _relative_path(value: str | PurePosixPath) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"expected a trial-relative path, got {str(value)!r}")
    return path.as_posix()


def _artifact_key(trial: str, relative_path: str) -> str:
    return stable_external_key("harbor", "artifact", trial, relative_path)


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    write_text_atomic(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def _json_object(value: dict[str, Any] | None, *, field_name: str) -> dict[str, Any]:
    result = dict(value or {})
    try:
        json.dumps(result)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON serializable") from exc
    return result


def _copy_and_hash(source: Path, destination: Path) -> tuple[str, int]:
    """Copy one stable source snapshot, returning its SHA-256 and byte count."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(2):
        before = source.stat()
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            with source.open("rb") as src, temporary.open("xb") as dst:
                while chunk := src.read(1024 * 1024):
                    dst.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
                dst.flush()
                os.fsync(dst.fileno())
            after = source.stat()
            identity_before = (before.st_ino, before.st_size, before.st_mtime_ns)
            identity_after = (after.st_ino, after.st_size, after.st_mtime_ns)
            if identity_before != identity_after or size != after.st_size:
                if attempt == 0:
                    continue
                raise RuntimeError(f"source changed while staging: {source}")
            os.replace(temporary, destination)
            try:
                shutil.copystat(source, destination, follow_symlinks=False)
            except OSError:
                pass
            _fsync_directory(destination.parent)
            return digest.hexdigest(), size
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    raise RuntimeError(f"source changed while staging: {source}")


def _hash_stable(path: Path) -> tuple[str, int]:
    """Fingerprint an already-durable file and reject a moving source."""

    for attempt in range(2):
        before = path.stat()
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ) and size == after.st_size:
            return digest.hexdigest(), size
        if attempt == 1:
            raise RuntimeError(f"file changed while inventorying: {path}")
    raise RuntimeError(f"file changed while inventorying: {path}")


def stage_trial(
    trial_dir: str | Path,
    destination: str | Path,
    *,
    expected_paths: list[str] | tuple[str, ...] = (),
) -> StagedTrial:
    """Copy Harbor's host-side trial output to a durable directory.

    This is not itself a sandbox lifecycle hook.  Public Harbor ``Trial.run()``
    tears the environment down before returning, so a normal post-run caller can
    guarantee only the host trial directory Harbor materialized.  A Harbor fork
    may invoke this function before teardown and use
    :attr:`StagedTrial.durable_collection_complete` as its local barrier.

    No network operation occurs: every visible regular file is copied and hashed
    under ``destination`` and progress is fsync'd to ``.probe-capture.json``.

    ``expected_paths`` makes producer-specific guarantees explicit.  Missing
    declared paths make collection partial; undeclared state outside the Harbor
    trial directory is unknowable and is never claimed as captured.
    """

    source = Path(trial_dir).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"{source} is not a trial directory")
    if target == source or source in target.parents:
        raise ValueError(
            "staging destination must be outside the source trial directory"
        )
    target.mkdir(parents=True, exist_ok=True)
    _fsync_directory(target.parent)
    ledger_path = target / CAPTURE_LEDGER_NAME
    if any(target.iterdir()) and not ledger_path.exists():
        raise FileExistsError(
            f"staging destination {target} is non-empty and has no {CAPTURE_LEDGER_NAME}"
        )

    trial = parse_trial(source).name
    trial_key = stable_external_key("harbor", "trial", trial)
    ledger = CaptureLedger(
        ledger_path,
        source="harbor",
        external_key=trial_key,
        context={
            "scope": "host_trial_directory",
            "unknown": [
                "undeclared sandbox state",
                "sandbox files Harbor did not materialize into the host trial directory",
            ],
            "trial_name": trial,
            "source_root": str(source),
            "staged_root": str(target),
        },
    )
    ledger.begin_inventory()

    declared = {_relative_path(path) for path in expected_paths}
    for rel in sorted(declared):
        ledger.expect(
            _artifact_key(trial, rel),
            role=role_for(rel),
            relative_path=rel,
            required=True,
            meta={"declared": True},
        )

    before = _trial_files(source)
    discovered = {path.relative_to(source).as_posix() for path in before}
    for path in before:
        rel = path.relative_to(source).as_posix()
        key = _artifact_key(trial, rel)
        previous = ledger.get(key)
        ledger.expect(key, role=role_for(rel), relative_path=rel, required=True)
        ledger.mark(key, CaptureState.discovered)
        try:
            content_hash, size = _copy_and_hash(path, target / rel)
        except (OSError, RuntimeError) as exc:
            ledger.mark(key, CaptureState.collection_failed, error=str(exc))
            raise
        if (
            previous
            and previous.get("state") == CaptureState.confirmed.value
            and previous.get("content_hash") == content_hash
        ):
            ledger.mark(
                key,
                CaptureState.confirmed,
                content_hash=content_hash,
                size_bytes=size,
                error=None,
            )
        else:
            ledger.mark(
                key,
                CaptureState.hashed,
                content_hash=content_hash,
                size_bytes=size,
                error=None,
            )

    after = {path.relative_to(source).as_posix() for path in _trial_files(source)}
    if after != discovered:
        changed = sorted(after.symmetric_difference(discovered))
        for rel in changed:
            key = _artifact_key(trial, rel)
            ledger.expect(key, role=role_for(rel), relative_path=rel, required=True)
            ledger.mark(
                key,
                CaptureState.collection_failed,
                error="trial inventory changed while staging",
            )
        raise RuntimeError(f"trial inventory changed while staging: {changed}")

    previously_expected = {
        entry.get("relative_path")
        for entry in ledger.entries()
        if entry.get("required") and entry.get("relative_path")
    }
    for rel in sorted((declared | previously_expected) - discovered):
        ledger.mark(
            _artifact_key(trial, rel),
            CaptureState.missing,
            error="declared path was absent from the trial directory",
        )

    # Symlinks and our mutable local ledger are outside the byte-upload policy,
    # but recording them prevents "not captured" from becoming "never knew."
    for path in sorted(source.rglob("*")):
        if not (path.is_symlink() or path.name == CAPTURE_LEDGER_NAME):
            continue
        rel = path.relative_to(source).as_posix()
        key = _artifact_key(trial, rel)
        symlink_target = os.readlink(path) if path.is_symlink() else None
        if symlink_target is not None:
            destination = target / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(
                f".{destination.name}.{uuid.uuid4().hex}.tmp"
            )
            try:
                os.symlink(symlink_target, temporary)
                os.replace(temporary, destination)
                _fsync_directory(destination.parent)
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
        ledger.expect(
            key,
            role=role_for(rel),
            relative_path=rel,
            required=False,
            meta={"target": symlink_target} if symlink_target is not None else None,
        )
        ledger.mark(
            key,
            CaptureState.intentionally_skipped,
            error="symlink" if path.is_symlink() else "Probe capture ledger",
        )

    ledger.finish_inventory()
    return StagedTrial(target, ledger)


def _producer_capture_manifest(
    staged: StagedTrial,
    *,
    environment: dict[str, Any],
    correlation: dict[str, Any],
    source_mode: str,
    archive: dict[str, Any] | None,
) -> dict[str, Any]:
    parsed = parse_trial(staged.trial_dir)
    result = parsed.result or {}
    verifier_result = result.get("verifier_result")
    verifier_result = verifier_result if isinstance(verifier_result, dict) else {}
    rewards = verifier_result.get("rewards")
    rewards = rewards if isinstance(rewards, dict) else None
    source_context = correlation.get("context")
    source_context = source_context if isinstance(source_context, dict) else {}
    report = staged.ledger.report()
    entries = staged.ledger.entries()
    files = [
        {
            "role": entry.get("role")
            or role_for(str(entry.get("relative_path") or "")),
            "path": entry["relative_path"],
            "content_hash": entry["content_hash"],
            "size_bytes": entry["size_bytes"],
        }
        for entry in entries
        if entry.get("required", True)
        and entry.get("relative_path")
        and entry.get("content_hash")
        and isinstance(entry.get("size_bytes"), int)
    ]
    expected = [
        {
            "path": entry["relative_path"],
            "role": entry.get("role") or "other",
            "required": bool(entry.get("required", True)),
            "state": (
                "present"
                if entry.get("content_hash")
                and isinstance(entry.get("size_bytes"), int)
                else entry.get("state") or "missing"
            ),
        }
        for entry in entries
        if entry.get("required", True) and entry.get("relative_path")
    ]
    missing_required = [
        item.get("relative_path")
        for item in report["collection"]["missing"]
        if item.get("relative_path")
    ]
    capture: dict[str, Any] = {
        "captured_at": _utc_now(),
        "completeness": {
            "status": report["collection"]["state"],
            "scope": report.get("scope"),
            "capture_scope": report.get("capture_scope"),
            "inventory_complete": report.get("inventory_complete", False),
            "expected": expected,
            "missing_required": missing_required,
            "unknown": report.get("unknown") or [],
            "sandbox_state_outside_harbor_outputs": "unknown",
        },
        "ledger": report,
        "symlinks": [
            {
                "path": entry["relative_path"],
                "target": (entry.get("meta") or {}).get("target"),
            }
            for entry in entries
            if entry.get("state") == CaptureState.intentionally_skipped.value
            and entry.get("error") == "symlink"
            and entry.get("relative_path")
        ],
    }
    if archive is not None:
        capture["archive"] = archive
    return {
        "schema_version": SCHEMA_VERSION,
        "trial": {
            "id": correlation.get("trial_id"),
            "name": parsed.name,
            "task_name": parsed.task_name,
            "task_id": correlation.get("task_id") or source_context.get("task_id"),
            "task_checksum": result.get("task_checksum"),
            "trial_uri": result.get("trial_uri"),
        },
        "agent": parsed.agent_info,
        "verifier": (
            {"reward": parsed.reward, "rewards": rewards}
            if parsed.reward is not None or rewards is not None
            else None
        ),
        "phases": parsed.phases,
        "environment": environment,
        "exception": parsed.exception,
        "source": {"mode": source_mode, **correlation},
        "files": files,
        "capture": capture,
    }


def _create_recovery_archive(staged: StagedTrial, path: Path) -> dict[str, Any]:
    with tarfile.open(path, "w:gz") as archive:
        archive.add(staged.trial_dir, arcname=staged.trial_dir.name, recursive=True)
    with path.open("rb") as handle:
        os.fsync(handle.fileno())
    content_hash, size_bytes = _hash_stable(path)
    return {
        "path": path.name,
        "content_hash": content_hash,
        "size_bytes": size_bytes,
    }


def _open_existing_trial_export(
    root: Path,
    *,
    request_id: str,
    run_id: str | None,
    step_index: int | None,
    external_key: str,
    correlation: dict[str, Any],
) -> StagedTrialExport:
    request_path = root / EXPORT_REQUEST_NAME
    manifest_path = root / EXPORT_MANIFEST_NAME
    trial_path = root / EXPORT_TRIAL_DIR_NAME
    if (
        not request_path.is_file()
        or not manifest_path.is_file()
        or not trial_path.is_dir()
    ):
        raise FileExistsError(f"incomplete Harbor export bundle already exists: {root}")
    try:
        descriptor = json.loads(request_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise FileExistsError(
            f"invalid Harbor export bundle already exists: {root}"
        ) from exc
    if not isinstance(descriptor, dict):
        raise FileExistsError(
            f"invalid Harbor export descriptor already exists: {request_path}"
        )
    existing_correlation = descriptor.get("correlation") or {}
    existing_target = descriptor.get("target") or {}
    existing_arguments = descriptor.get("arguments") or {}
    conflicts = (
        descriptor.get("schema_version") != EXPORT_SCHEMA_VERSION
        or descriptor.get("request_id") != request_id
        or existing_correlation.get("external_key") != external_key
        or (run_id is not None and str(existing_target.get("run_id")) != str(run_id))
        or (
            step_index is not None
            and existing_arguments.get("step_index") != step_index
        )
        or any(
            value is not None and existing_correlation.get(key) != value
            for key, value in correlation.items()
            if key != "probe_run_id"
        )
    )
    if conflicts:
        raise FileExistsError(
            f"conflicting Harbor export bundle already exists: {root}"
        )
    staged = open_staged_trial(trial_path)
    if staged is None:
        raise FileExistsError(
            f"Harbor export bundle has no capture ledger: {trial_path}"
        )
    archive_path = root / EXPORT_ARCHIVE_NAME
    return StagedTrialExport(
        root=root,
        staged_trial=staged,
        capture_manifest_path=manifest_path,
        request_path=request_path,
        descriptor=descriptor,
        archive_path=archive_path if archive_path.is_file() else None,
    )


def stage_trial_export(
    trial_dir: str | Path,
    destination: str | Path,
    *,
    run_id: str | None = None,
    step_index: int | None = None,
    environment: dict[str, Any] | None = None,
    correlation: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    expected_paths: list[str] | tuple[str, ...] = (),
    external_key: str | None = None,
    request_id: str | None = None,
    source_mode: str = "bridge-hook",
    reward_key: str = "reward",
    expand: bool = False,
    max_trajectory_spans: int | None = None,
    create_archive: bool = True,
) -> StagedTrialExport:
    """Atomically publish an SDK-owned ``probe-harbor-export/1`` bundle.

    The caller supplies native identifiers and opaque context, not a Probe wire
    manifest.  Probe copies and hashes Harbor's host trial tree through
    :func:`stage_trial`, writes the capture manifest, and publishes the retryable
    request last by renaming the completed bundle into ``destination``.  No
    network calls occur on this producer path.

    ``run_id`` may be absent while the API is offline; ``probe trial drain
    --run`` can bind it later.  Repeating the same request against an existing
    destination is an idempotent read, while a conflicting identity is rejected.
    """

    source = Path(trial_dir).expanduser().resolve()
    target = Path(destination).expanduser().resolve()
    if not source.is_dir():
        raise FileNotFoundError(f"{source} is not a trial directory")
    if target == source or source in target.parents:
        raise ValueError(
            "export destination must be outside the source trial directory"
        )

    parsed = parse_trial(source)
    environment_value = _json_object(environment, field_name="environment")
    correlation_value = _json_object(correlation, field_name="correlation")
    context_value = _json_object(context, field_name="context")
    embedded_context = correlation_value.get("context")
    if embedded_context is not None and not isinstance(embedded_context, dict):
        raise ValueError("correlation.context must be an object")
    merged_context = {**(embedded_context or {}), **context_value}
    if merged_context:
        correlation_value["context"] = merged_context

    correlated_run = correlation_value.get("probe_run_id")
    if (
        run_id is not None
        and correlated_run is not None
        and str(run_id) != str(correlated_run)
    ):
        raise ValueError("run_id and correlation.probe_run_id disagree")
    resolved_run_id = (
        str(run_id or correlated_run) if run_id or correlated_run else None
    )

    correlated_step = correlation_value.get("step_index")
    if (
        step_index is not None
        and correlated_step is not None
        and step_index != correlated_step
    ):
        raise ValueError("step_index and correlation.step_index disagree")
    resolved_step = step_index if step_index is not None else correlated_step
    if resolved_step is not None and not isinstance(resolved_step, int):
        raise ValueError("step_index must be an integer")

    correlated_key = correlation_value.get("external_key")
    if (
        external_key is not None
        and correlated_key is not None
        and external_key != correlated_key
    ):
        raise ValueError("external_key and correlation.external_key disagree")
    resolved_key = external_key or correlated_key
    if not resolved_key:
        resolved_key = stable_external_key(
            "harbor",
            "rollout",
            correlation_value.get("trial_id") or parsed.name,
            resolved_step if resolved_step is not None else "stepless",
            (
                correlation_value.get("sample_id")
                if correlation_value.get("sample_id") is not None
                else "single"
            ),
        )
    resolved_key = str(resolved_key)
    resolved_request_id = str(request_id or resolved_key)

    correlation_value.update(
        {
            "external_key": resolved_key,
            "probe_run_id": resolved_run_id,
            "step_index": resolved_step,
            "trial_id": correlation_value.get("trial_id") or parsed.name,
        }
    )
    # Verify the final merged shape too: reserved values may have introduced an
    # object a custom JSON encoder would otherwise silently coerce.
    _json_object(correlation_value, field_name="correlation")

    if target.exists():
        return _open_existing_trial_export(
            target,
            request_id=resolved_request_id,
            run_id=resolved_run_id,
            step_index=resolved_step,
            external_key=resolved_key,
            correlation=correlation_value,
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}.", dir=target.parent))
    try:
        staged = stage_trial(
            source,
            temporary / EXPORT_TRIAL_DIR_NAME,
            expected_paths=expected_paths,
        )
        staged.ledger.update_context(
            probe_run_id=resolved_run_id,
            step_index=resolved_step,
            environment=environment_value,
            correlation=correlation_value,
            export_context=merged_context,
            export_request_id=resolved_request_id,
        )

        archive_path: Path | None = None
        archive: dict[str, Any] | None = None
        if create_archive:
            archive_path = temporary / EXPORT_ARCHIVE_NAME
            archive = _create_recovery_archive(staged, archive_path)

        manifest = _producer_capture_manifest(
            staged,
            environment=environment_value,
            correlation=correlation_value,
            source_mode=source_mode,
            archive=archive,
        )
        manifest_path = temporary / EXPORT_MANIFEST_NAME
        _write_json_atomic(manifest_path, manifest)

        descriptor: dict[str, Any] = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "request_id": resolved_request_id,
            "status": "pending",
            "created_at": _utc_now(),
            "attempts": 0,
            "last_error": None,
            "target": {"kind": "probe_run", "run_id": resolved_run_id},
            "connector": EXPORT_CONNECTOR,
            "arguments": {
                "trial_dir": EXPORT_TRIAL_DIR_NAME,
                "trial_dir_base": "descriptor_dir",
                "step_index": resolved_step,
                "environment": environment_value,
                "source_mode": source_mode,
                "reward_key": reward_key,
                "expand": bool(expand),
                "max_trajectory_spans": max_trajectory_spans,
            },
            "correlation": correlation_value,
            "capture_manifest": EXPORT_MANIFEST_NAME,
        }
        if archive_path is not None:
            descriptor["archive"] = EXPORT_ARCHIVE_NAME
        request_path = temporary / EXPORT_REQUEST_NAME
        _write_json_atomic(request_path, descriptor)

        _fsync_directory(temporary)
        os.replace(temporary, target)
        _fsync_directory(target.parent)
        published = open_staged_trial(target / EXPORT_TRIAL_DIR_NAME)
        if (
            published is None
        ):  # defensive: the request is never returned without its ledger
            raise RuntimeError("published Harbor export bundle lost its capture ledger")
        return StagedTrialExport(
            root=target,
            staged_trial=published,
            capture_manifest_path=target / EXPORT_MANIFEST_NAME,
            request_path=target / EXPORT_REQUEST_NAME,
            descriptor=descriptor,
            archive_path=(
                target / EXPORT_ARCHIVE_NAME if archive_path is not None else None
            ),
        )
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def open_staged_trial(trial_dir: str | Path) -> StagedTrial | None:
    """Open a staged trial when its adjacent ledger marker is present."""

    root = Path(trial_dir).expanduser()
    ledger_path = root / CAPTURE_LEDGER_NAME
    if not ledger_path.is_file():
        return None
    return StagedTrial(root, CaptureLedger.open(ledger_path))


def adopt_staged_trial(
    trial_dir: str | Path,
    *,
    expected_paths: list[str] | tuple[str, ...] = (),
    expected_files: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> StagedTrial:
    """Inventory a trial directory that its producer already placed durably.

    Unlike :func:`stage_trial`, this performs no copy.  It is the exporter entry
    point for Miles' ``probe-harbor-export/1`` bundle, whose bridge has already
    placed ``trial/`` on the capture volume before writing the request.
    """

    root = Path(trial_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a trial directory")
    existing = open_staged_trial(root)
    parsed = parse_trial(root)
    trial = (
        str(existing.ledger.context.get("trial_name"))
        if existing is not None and existing.ledger.context.get("trial_name")
        else parsed.name
    )
    ledger = (
        existing.ledger
        if existing is not None
        else CaptureLedger(
            root / CAPTURE_LEDGER_NAME,
            source="harbor",
            external_key=stable_external_key("harbor", "trial", trial),
            context={
                "scope": "host_trial_directory",
                "unknown": [
                    "undeclared sandbox state",
                    "sandbox files Harbor did not materialize into the host trial directory",
                ],
                "trial_name": trial,
                "staged_root": str(root),
                "staged_by": "producer",
            },
        )
    )
    ledger.begin_inventory()

    declared_files = {
        _relative_path(str(item["path"])): item
        for item in expected_files
        if item.get("path")
    }
    declared = {_relative_path(path) for path in expected_paths} | set(declared_files)
    for rel in sorted(declared):
        declaration = declared_files.get(rel) or {}
        ledger.expect(
            _artifact_key(trial, rel),
            role=role_for(rel),
            relative_path=rel,
            required=True,
            meta={
                "declared": True,
                "declared_content_hash": declaration.get("content_hash"),
                "declared_size_bytes": declaration.get("size_bytes"),
            },
        )
    files = _trial_files(root)
    discovered = {path.relative_to(root).as_posix() for path in files}
    for path in files:
        rel = path.relative_to(root).as_posix()
        key = _artifact_key(trial, rel)
        previous = ledger.get(key)
        ledger.expect(key, role=role_for(rel), relative_path=rel, required=True)
        ledger.mark(key, CaptureState.discovered)
        try:
            content_hash, size = _hash_stable(path)
        except (OSError, RuntimeError) as exc:
            ledger.mark(key, CaptureState.collection_failed, error=str(exc))
            raise
        state = (
            CaptureState.confirmed
            if previous
            and previous.get("state") == CaptureState.confirmed.value
            and previous.get("content_hash") == content_hash
            else CaptureState.hashed
        )
        ledger.mark(
            key,
            state,
            content_hash=content_hash,
            size_bytes=size,
            error=None,
        )
        declaration = declared_files.get(rel) or {}
        declared_hash = declaration.get("content_hash")
        declared_size = declaration.get("size_bytes")
        if declared_hash is not None and declared_hash != content_hash:
            ledger.mark(
                key,
                CaptureState.collection_failed,
                error=f"capture manifest hash mismatch: {declared_hash} != {content_hash}",
            )
        elif declared_size is not None and int(declared_size) != size:
            ledger.mark(
                key,
                CaptureState.collection_failed,
                error=f"capture manifest size mismatch: {declared_size} != {size}",
            )
    after = {path.relative_to(root).as_posix() for path in _trial_files(root)}
    if after != discovered:
        raise RuntimeError("trial inventory changed while it was being fingerprinted")
    previously_expected = {
        entry.get("relative_path")
        for entry in ledger.entries()
        if entry.get("required") and entry.get("relative_path")
    }
    for rel in sorted((declared | previously_expected) - discovered):
        ledger.mark(
            _artifact_key(trial, rel),
            CaptureState.missing,
            error="declared path was absent from the trial directory",
        )
    for path in sorted(root.rglob("*")):
        if not (path.is_symlink() or path.name == CAPTURE_LEDGER_NAME):
            continue
        rel = path.relative_to(root).as_posix()
        key = _artifact_key(trial, rel)
        ledger.expect(
            key,
            role=role_for(rel),
            relative_path=rel,
            required=False,
            meta={"target": os.readlink(path)} if path.is_symlink() else None,
        )
        ledger.mark(
            key,
            CaptureState.intentionally_skipped,
            error="symlink" if path.is_symlink() else "Probe capture ledger",
        )
    ledger.finish_inventory()
    return StagedTrial(root, ledger)


def parse_trial(trial_dir: str | Path) -> ParsedTrial:
    """Read a Harbor trial directory, tolerating any subset of the contract."""
    root = Path(trial_dir).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a trial directory")
    files = _trial_files(root)
    result = _load_json(root / "result.json")
    config = _load_json(root / "config.json")

    # Reward: reward.json ({"reward": x} or a bare number) beats result.verifier_result.
    reward = None
    reward_doc = _load_json(root / "reward.json")
    if isinstance(reward_doc, dict):
        reward = _as_float(reward_doc.get("reward"))
    elif reward_doc is not None:
        reward = _as_float(reward_doc)
    if reward is None and isinstance(result, dict):
        verifier = result.get("verifier_result")
        if isinstance(verifier, dict):
            reward = _as_float(verifier.get("reward"))
            rewards = verifier.get("rewards")
            if reward is None and isinstance(rewards, dict) and rewards:
                reward = _as_float(rewards.get("reward", next(iter(rewards.values()))))

    trajectory = _load_json(root / "trajectory.json")
    trajectory_format = atif.detect_trajectory_format(trajectory)

    name = None
    if isinstance(result, dict):
        name = result.get("trial_name")
    return ParsedTrial(
        trial_dir=root,
        name=str(name) if name else root.name,
        files=files,
        config=config if isinstance(config, dict) else None,
        result=result if isinstance(result, dict) else None,
        reward=reward,
        trajectory_format=trajectory_format,
        trajectory=trajectory if isinstance(trajectory, dict) else None,
    )


def capture_trial(
    run: "Run",
    trial_dir: str | Path | StagedTrial,
    *,
    step_index: int | None = None,
    environment: dict | None = None,
    source_mode: str = "local",
    source_context: dict[str, Any] | None = None,
    reward_key: str = "reward",
    external_key: str | None = None,
    log_reward: bool = True,
    expand: bool = True,
    max_trajectory_spans: int | None = None,
    strict: bool | None = None,
) -> dict:
    """Capture one Harbor trial into ``run``, keyed by ``step_index``.

    - ``step_index`` is the training step / Miles ``rollout_id`` — the join
      Osmosis is missing. Optional, but pass it whenever the trainer knows it.
    - ``environment`` is recorded opaquely on the manifest (e.g. ``{"type":
      "skypilot-fork"}``) — never structural, per the plan's agnosticism rule.
    - Uploads are fail-open like every SDK data write: a file that cannot reach
      storage right now falls back to a labeled reference and the manifest marks
      it ``uploaded: false`` — the training loop is never blocked.
    - ``expand`` turns a recognized trajectory format (ATIF or a registered
      fork parser) into turn/tool_call spans under the rollout span.
      ``max_trajectory_spans`` bounds the eager window only (0 = unlimited);
      raw bytes are always stored regardless.

    Returns ``{trial, span_id, reward, manifest, files, trajectory}``.
    """
    staged = (
        trial_dir
        if isinstance(trial_dir, StagedTrial)
        else open_staged_trial(trial_dir)
    )
    root = staged.trial_dir if staged is not None else Path(trial_dir)
    ledger = staged.ledger if staged is not None else None
    parsed = parse_trial(root)
    if ledger is not None and ledger.context.get("trial_name"):
        parsed.name = str(ledger.context["trial_name"])
    status = "failed" if parsed.exception else "completed"
    rollout_key = external_key or stable_external_key("harbor", "rollout", parsed.name)
    rollout_id = stable_span_id(run.id, rollout_key)
    if ledger is not None:
        ledger.update_context(
            run_id=run.id,
            span_id=rollout_id,
            rollout_external_key=rollout_key,
            step_index=step_index,
        )
    span_id = run.span(
        "rollout",
        id=rollout_id,
        name=parsed.name,
        step_index=step_index,
        external_key=rollout_key,
        status=status,
        started_at=parsed.started_at,
        ended_at=parsed.ended_at,
        attributes={
            "harbor_trial": parsed.name,
            "task_name": parsed.task_name,
            "agent": parsed.agent_info,
            "reward": parsed.reward,
        },
        strict=strict,
    )
    reward_already_logged = bool(ledger and ledger.context.get("reward_logged"))
    if log_reward and parsed.reward is not None and not reward_already_logged:
        metric_result = run.log(
            {reward_key: parsed.reward}, step=step_index, strict=strict
        )
        if ledger is not None:
            ledger.update_context(
                reward_logged={
                    "state": "confirmed" if metric_result is not None else "spooled",
                    "key": reward_key,
                    "value": parsed.reward,
                    "step_index": step_index,
                }
            )

    trajectory_report = {
        "format": parsed.trajectory_format,
        "expanded": False,
        "spans": 0,
    }
    if expand and parsed.trajectory is not None:
        trajectory_report = atif.expand_trajectory(
            run,
            parsed.trajectory,
            root_span_id=span_id,
            trial=parsed.name,
            step_index=step_index,
            fmt=parsed.trajectory_format,
            max_spans=max_trajectory_spans,
            strict=strict,
        )

    file_entries: list[dict] = []
    for path in parsed.files:
        rel = path.relative_to(parsed.trial_dir).as_posix()
        role = role_for(rel)
        ledger_key = _artifact_key(parsed.name, rel)
        previous = ledger.get(ledger_key) if ledger is not None else None
        if previous and previous.get("state") == CaptureState.confirmed.value:
            uploaded = {
                "id": previous.get("artifact_id"),
                "content_hash": previous.get("content_hash"),
                "size_bytes": previous.get("size_bytes"),
                "status": "complete",
                "is_reference": False,
            }
        else:
            if ledger is not None:
                ledger.mark(ledger_key, CaptureState.upload_pending, error=None)
            try:
                uploaded = run.log_artifact(
                    f"{parsed.name}/{rel}",
                    path=str(path),
                    kind="file",
                    meta={"role": role, "trial": parsed.name, "path": rel},
                    span_id=span_id,
                    step_index=step_index,
                    strict=strict,
                )
            except Exception as exc:
                if ledger is not None:
                    ledger.mark(ledger_key, CaptureState.upload_failed, error=str(exc))
                raise
        entry: dict[str, Any] = {"role": role, "path": rel}
        if isinstance(uploaded, dict):
            entry["artifact_id"] = uploaded.get("id")
            entry["content_hash"] = uploaded.get("content_hash")
            entry["size_bytes"] = uploaded.get("size_bytes")
            entry["uploaded"] = not uploaded.get("is_reference", False)
            if ledger is not None:
                ledger.mark(
                    ledger_key,
                    (
                        CaptureState.confirmed
                        if entry["uploaded"]
                        else CaptureState.upload_failed
                    ),
                    artifact_id=entry["artifact_id"],
                    content_hash=entry["content_hash"],
                    size_bytes=entry["size_bytes"],
                    error=(
                        None
                        if entry["uploaded"]
                        else "Probe storage did not confirm artifact bytes; staged bytes retained"
                    ),
                )
        else:  # spooled fail-open: no row yet, replayed at flush()
            entry["artifact_id"] = None
            entry["uploaded"] = False
            if ledger is not None:
                ledger.mark(
                    ledger_key,
                    CaptureState.upload_failed,
                    error="upload did not return a confirmed artifact; staged bytes retained",
                )
        file_entries.append(entry)

    if ledger is not None:
        ledger.update_context(manifest_publication={"state": "upload_pending"})
    capture_report = ledger.report() if ledger is not None else None
    # A manifest that can be read from the API is its own confirmation.  The
    # local ledger is updated after the write for reconciliation diagnostics;
    # the manifest body uses this self-evident state instead of claiming its
    # publication is part of file-byte completeness.
    manifest_capture = dict(capture_report) if capture_report is not None else None
    if manifest_capture is not None:
        manifest_capture["manifest_publication"] = {"state": "confirmed_by_presence"}
    source_meta: dict[str, Any] = {"mode": source_mode, "rollout_id": step_index}
    if source_context:
        source_meta["context"] = dict(source_context)
    manifest_meta = {
        "schema_version": SCHEMA_VERSION,
        "trial": {
            "name": parsed.name,
            "task_name": parsed.task_name,
            "task_checksum": (parsed.result or {}).get("task_checksum"),
            "trial_uri": (parsed.result or {}).get("trial_uri"),
        },
        "agent": parsed.agent_info,
        "verifier": {"reward": parsed.reward} if parsed.reward is not None else None,
        "phases": parsed.phases,
        "environment": environment or {},
        "exception": parsed.exception,
        "trajectory_format": parsed.trajectory_format,
        "trajectory": trajectory_report,
        "source": source_meta,
        "files": file_entries,
        "capture": manifest_capture,
    }
    try:
        manifest = run.log_artifact(
            parsed.name,
            kind=MANIFEST_KIND,
            meta=manifest_meta,
            span_id=span_id,
            step_index=step_index,
            strict=strict,
        )
    except Exception as exc:
        if ledger is not None:
            ledger.update_context(
                manifest_publication={"state": "failed", "error": str(exc)}
            )
        raise
    if ledger is not None and isinstance(manifest, dict):
        ledger.update_context(
            run_id=run.id,
            span_id=span_id,
            manifest_artifact_id=manifest.get("id"),
            manifest_publication={
                "state": "confirmed",
                "artifact_id": manifest.get("id"),
            },
            step_index=step_index,
        )
    elif ledger is not None:
        ledger.update_context(manifest_publication={"state": "pending"})
    return {
        "trial": parsed.name,
        "span_id": span_id,
        "reward": parsed.reward,
        "manifest": manifest,
        "files": file_entries,
        "trajectory": trajectory_report,
        "capture": ledger.report() if ledger is not None else None,
    }


def reconcile_staged_trial(
    run: "Run",
    trial_dir: str | Path | StagedTrial,
    **kwargs: Any,
) -> dict:
    """Retry a staged trial's unconfirmed uploads without duplicating reward points.

    Confirmed files are skipped using the durable ledger.  The rollout span uses
    the same deterministic id and is therefore an upsert.  A fresh
    ``harbor_trial`` manifest carries the latest completeness report; the ledger
    records that manifest id for subsequent reconciliation.
    """

    staged = (
        trial_dir
        if isinstance(trial_dir, StagedTrial)
        else open_staged_trial(trial_dir)
    )
    if staged is None:
        raise ValueError(
            f"{trial_dir} is not a staged trial (missing {CAPTURE_LEDGER_NAME})"
        )
    kwargs.setdefault("step_index", staged.ledger.context.get("step_index"))
    kwargs.setdefault("expand", False)
    return capture_trial(run, staged, log_reward=False, **kwargs)
