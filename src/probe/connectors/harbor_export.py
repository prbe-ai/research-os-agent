"""Consume retryable SDK-owned ``probe-harbor-export/1`` Harbor requests.

``probe.connectors.harbor.stage_trial_export`` owns durable collection and both
wire manifests.  The consumer owns network publication: it inventories and
verifies ``trial/``, calls the normal Harbor connector, and atomically records
completion or a retryable error in the same descriptor.  It never deletes the
request, archive, manifest, or trial bytes.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterator

from .harbor import (
    EXPORT_CONNECTOR,
    EXPORT_SCHEMA_VERSION,
    adopt_staged_trial,
    capture_trial,
)

if TYPE_CHECKING:
    from ..sdk.client import Client

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HarborExportError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise HarborExportError(f"invalid JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise HarborExportError(f"{path} must contain a JSON object")
    return value


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    payload = json.dumps(value, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def _request_lock(path: Path) -> Iterator[None]:
    lock_path = path.with_name(f".{path.name}.lock")
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _descriptor_relative(base: Path, raw: Any, *, field: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise HarborExportError(f"{field} must be a non-empty descriptor-relative path")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise HarborExportError(f"{field} must stay inside the descriptor directory")
    resolved = (base / Path(*relative.parts)).resolve()
    base_resolved = base.resolve()
    if resolved != base_resolved and base_resolved not in resolved.parents:
        raise HarborExportError(f"{field} escapes the descriptor directory")
    return resolved


def _validate_descriptor(
    descriptor: dict[str, Any],
    path: Path,
    *,
    fallback_run_id: str | None = None,
) -> tuple[str, dict, dict]:
    if descriptor.get("schema_version") != EXPORT_SCHEMA_VERSION:
        raise HarborExportError(
            f"unsupported export schema {descriptor.get('schema_version')!r}; "
            f"expected {EXPORT_SCHEMA_VERSION!r}"
        )
    if descriptor.get("connector") != EXPORT_CONNECTOR:
        raise HarborExportError(
            f"unsupported export connector {descriptor.get('connector')!r}"
        )
    target = descriptor.get("target")
    arguments = descriptor.get("arguments")
    correlation = descriptor.get("correlation")
    if not isinstance(target, dict) or target.get("kind") != "probe_run":
        raise HarborExportError("target.kind must be 'probe_run'")
    if not isinstance(arguments, dict) or not isinstance(correlation, dict):
        raise HarborExportError("arguments and correlation must be objects")
    target_run = target.get("run_id")
    correlated_run = correlation.get("probe_run_id")
    if target_run and correlated_run and str(target_run) != str(correlated_run):
        raise HarborExportError("target.run_id and correlation.probe_run_id disagree")
    descriptor_run_id = target_run or correlated_run
    if (
        fallback_run_id
        and descriptor_run_id
        and str(fallback_run_id) != str(descriptor_run_id)
    ):
        raise HarborExportError(
            f"fallback run {fallback_run_id} disagrees with descriptor run {descriptor_run_id}"
        )
    run_id = descriptor_run_id or fallback_run_id
    if not run_id:
        raise HarborExportError("export request has no Probe run id yet")
    if not correlation.get("external_key"):
        raise HarborExportError("correlation.external_key is required")
    if arguments.get("trial_dir_base") != "descriptor_dir":
        raise HarborExportError("arguments.trial_dir_base must be 'descriptor_dir'")
    _descriptor_relative(
        path.parent, arguments.get("trial_dir"), field="arguments.trial_dir"
    )
    if descriptor.get("archive"):
        archive = _descriptor_relative(
            path.parent, descriptor["archive"], field="archive"
        )
        if not archive.is_file():
            raise HarborExportError(f"declared recovery archive is missing: {archive}")
    return str(run_id), arguments, correlation


def _capture_declarations(
    descriptor: dict[str, Any], request_path: Path
) -> tuple[Path, dict[str, Any], list[dict[str, Any]]]:
    raw = descriptor.get("capture_manifest")
    if not raw:
        raise HarborExportError("capture_manifest is required")
    manifest_path = _descriptor_relative(
        request_path.parent, raw, field="capture_manifest"
    )
    manifest = _read_json(manifest_path)
    if manifest.get("schema_version") != "1.0":
        raise HarborExportError(
            f"unsupported capture manifest schema {manifest.get('schema_version')!r}"
        )
    files = manifest.get("files")
    if not isinstance(files, list):
        raise HarborExportError("capture manifest files must be a list")
    declarations: list[dict[str, Any]] = []
    for index, item in enumerate(files):
        if not isinstance(item, dict) or not item.get("path"):
            raise HarborExportError(f"capture manifest files[{index}] has no path")
        content_hash = item.get("content_hash")
        size_bytes = item.get("size_bytes")
        if not isinstance(content_hash, str) or not _SHA256.fullmatch(content_hash):
            raise HarborExportError(
                f"capture manifest files[{index}].content_hash must be lowercase SHA-256"
            )
        if not isinstance(size_bytes, int) or size_bytes < 0:
            raise HarborExportError(
                f"capture manifest files[{index}].size_bytes must be a non-negative integer"
            )
        declarations.append(item)
    completeness = (manifest.get("capture") or {}).get("completeness") or {}
    for item in completeness.get("expected") or []:
        if isinstance(item, dict) and item.get("required") and item.get("path"):
            if not any(
                declaration.get("path") == item["path"] for declaration in declarations
            ):
                declarations.append({"path": item["path"], "role": item.get("role")})
    return manifest_path, manifest, declarations


def consume_export_request(
    client: "Client",
    request_path: str | Path,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Publish one descriptor exactly once, or leave it retryable on failure."""

    path = Path(request_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    with _request_lock(path):
        descriptor = _read_json(path)
        if descriptor.get("status") == "completed":
            return descriptor

        descriptor["status"] = "exporting"
        descriptor["attempts"] = int(descriptor.get("attempts") or 0) + 1
        descriptor["last_error"] = None
        descriptor["updated_at"] = _now()
        _write_json_atomic(path, descriptor)

        try:
            run_id, arguments, correlation = _validate_descriptor(
                descriptor,
                path,
                fallback_run_id=run_id,
            )
            # Persist a later-resolved run identity so subsequent retries no
            # longer depend on the repair command's argument.
            descriptor["target"]["run_id"] = run_id
            descriptor["correlation"]["probe_run_id"] = run_id
            _write_json_atomic(path, descriptor)
            trial_dir = _descriptor_relative(
                path.parent, arguments["trial_dir"], field="arguments.trial_dir"
            )
            capture_manifest_path, producer_manifest, declarations = (
                _capture_declarations(descriptor, path)
            )
            staged = adopt_staged_trial(trial_dir, expected_files=declarations)
            collection = staged.ledger.report()["collection"]
            if collection["state"] != "complete":
                raise HarborExportError(
                    f"durable trial collection is {collection['state']}: {collection['missing']}"
                )

            # Crash recovery: capture_trial persists confirmation before the
            # descriptor's final atomic write. If that write was interrupted,
            # finish the descriptor without duplicating a reward or manifest.
            ledger_report = staged.ledger.report()
            ledger_context = staged.ledger.context
            if ledger_context.get("run_id") and str(ledger_context["run_id"]) != run_id:
                raise HarborExportError(
                    "staged ledger is already bound to a different Probe run"
                )
            if ledger_context.get("rollout_external_key") and ledger_context[
                "rollout_external_key"
            ] != correlation.get("external_key"):
                raise HarborExportError(
                    "staged ledger is already bound to a different rollout"
                )
            publication = ledger_context.get("manifest_publication") or {}
            main_capture_confirmed = (
                (ledger_report.get("capture") or {}).get("state") == "complete"
                and publication.get("state") == "confirmed"
                and str(ledger_context.get("run_id")) == run_id
                and ledger_context.get("rollout_external_key")
                == correlation.get("external_key")
            )

            from ..sdk.run import Run

            run = Run(client, client.get_run(run_id))
            argument_step = arguments.get("step_index")
            correlation_step = correlation.get("step_index")
            if (
                argument_step is not None
                and correlation_step is not None
                and argument_step != correlation_step
            ):
                raise HarborExportError(
                    "arguments.step_index and correlation.step_index disagree"
                )
            step_index = (
                argument_step if argument_step is not None else correlation_step
            )
            if main_capture_confirmed:
                capture = ledger_report
                trial_name = ledger_context.get("trial_name")
                span_id = ledger_context.get("span_id")
                manifest_artifact_id = publication.get("artifact_id")
            else:
                result = capture_trial(
                    run,
                    staged,
                    step_index=step_index,
                    environment=arguments.get("environment"),
                    source_mode=arguments.get("source_mode") or "bridge-hook",
                    source_context=correlation,
                    external_key=correlation.get("external_key"),
                    reward_key=arguments.get("reward_key") or "reward",
                    expand=bool(arguments.get("expand", False)),
                    max_trajectory_spans=arguments.get("max_trajectory_spans"),
                    strict=True,
                )
                capture = result.get("capture") or {}
                if (capture.get("capture") or {}).get("state") != "complete":
                    raise HarborExportError(
                        f"artifact byte capture is incomplete: {capture}"
                    )
                if (capture.get("manifest_publication") or {}).get(
                    "state"
                ) != "confirmed":
                    raise HarborExportError(
                        f"manifest publication is not confirmed: {capture}"
                    )
                manifest = result.get("manifest") or {}
                trial_name = result.get("trial")
                span_id = result.get("span_id")
                manifest_artifact_id = (
                    manifest.get("id") if isinstance(manifest, dict) else None
                )

            evidence = staged.ledger.context.get("producer_capture_manifest") or {}
            if evidence.get("state") != "confirmed":
                staged.ledger.update_context(
                    producer_capture_manifest={"state": "upload_pending"}
                )
                try:
                    evidence_artifact = run.log_artifact(
                        f"{trial_name}/capture-manifest.json",
                        path=str(capture_manifest_path),
                        kind="harbor_capture_manifest",
                        content_type="application/json",
                        span_id=span_id,
                        step_index=step_index,
                        meta={
                            "role": "capture_manifest",
                            "schema_version": producer_manifest.get("schema_version"),
                            "request_id": descriptor.get("request_id"),
                        },
                        strict=True,
                    )
                except Exception as exc:
                    staged.ledger.update_context(
                        producer_capture_manifest={"state": "failed", "error": str(exc)}
                    )
                    raise
                evidence = {
                    "state": "confirmed",
                    "artifact_id": evidence_artifact.get("id"),
                    "content_hash": evidence_artifact.get("content_hash"),
                }
                staged.ledger.update_context(producer_capture_manifest=evidence)

            descriptor["status"] = "completed"
            descriptor["last_error"] = None
            descriptor["completed_at"] = _now()
            descriptor["updated_at"] = descriptor["completed_at"]
            descriptor["result"] = {
                "run_id": run_id,
                "trial": trial_name,
                "span_id": span_id,
                "manifest_artifact_id": manifest_artifact_id,
                "producer_capture_manifest_artifact_id": evidence.get("artifact_id"),
                "capture": capture,
                "ledger": str(staged.ledger.path.relative_to(path.parent)),
            }
            _write_json_atomic(path, descriptor)
            return descriptor
        except Exception as exc:
            descriptor["status"] = "failed"
            descriptor["last_error"] = f"{type(exc).__name__}: {exc}"
            descriptor["updated_at"] = _now()
            _write_json_atomic(path, descriptor)
            raise


def drain_export_requests(
    client: "Client",
    capture_root: str | Path,
    *,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Attempt every non-completed export request below a durable capture root."""

    root = Path(capture_root).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    completed: list[str] = []
    failed: list[dict[str, str]] = []
    skipped: list[str] = []
    for path in sorted(root.rglob("export-request.json")):
        before = _read_json(path)
        if before.get("status") == "completed":
            skipped.append(str(path))
            continue
        try:
            consume_export_request(client, path, run_id=run_id)
            completed.append(str(path))
        except Exception as exc:
            failed.append({"path": str(path), "error": f"{type(exc).__name__}: {exc}"})
    return {
        "root": str(root),
        "completed": completed,
        "failed": failed,
        "skipped": skipped,
        "counts": {
            "completed": len(completed),
            "failed": len(failed),
            "skipped": len(skipped),
        },
    }


__all__ = [
    "EXPORT_CONNECTOR",
    "EXPORT_SCHEMA_VERSION",
    "HarborExportError",
    "consume_export_request",
    "drain_export_requests",
]
