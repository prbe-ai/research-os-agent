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

if TYPE_CHECKING:
    from ..sdk.run import Run

SCHEMA_VERSION = "1.0"
MANIFEST_KIND = "harbor_trial"
CAPTURE_LEDGER_NAME = ".probe-capture.json"

#: Known manifest roles (plan schema v1). Anything else is "other".
ROLES = ("config", "lock", "result", "trajectory", "reward",
         "agent_log", "verifier", "output", "other")

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
        if isinstance(self.result, dict) and isinstance(self.result.get("agent_info"), dict):
            return self.result["agent_info"]
        return None

    @property
    def phases(self) -> dict:
        """The four TrialResult phase timings (whatever subset exists)."""
        if not isinstance(self.result, dict):
            return {}
        out = {}
        for phase in ("environment_setup", "agent_setup", "agent_execution", "verifier"):
            timing = self.result.get(phase)
            if isinstance(timing, dict):
                out[phase] = {k: timing.get(k) for k in ("started_at", "finished_at")}
        return out

    @property
    def exception(self) -> dict | None:
        if isinstance(self.result, dict) and isinstance(self.result.get("exception_info"), dict):
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


def _relative_path(value: str | PurePosixPath) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError(f"expected a trial-relative path, got {str(value)!r}")
    return path.as_posix()


def _artifact_key(trial: str, relative_path: str) -> str:
    return stable_external_key("harbor", "artifact", trial, relative_path)


def _fsync_directory(path: Path) -> None:
    try:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        pass


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
        if (
            (before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_ino, after.st_size, after.st_mtime_ns)
            and size == after.st_size
        ):
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
        raise ValueError("staging destination must be outside the source trial directory")
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
        ledger.expect(
            key,
            role=role_for(rel),
            relative_path=rel,
            required=False,
        )
        ledger.mark(
            key,
            CaptureState.intentionally_skipped,
            error="symlink" if path.is_symlink() else "Probe capture ledger",
        )

    ledger.finish_inventory()
    return StagedTrial(target, ledger)


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
        _relative_path(str(item["path"])): item for item in expected_files if item.get("path")
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
        ledger.expect(key, role=role_for(rel), relative_path=rel, required=False)
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
    staged = trial_dir if isinstance(trial_dir, StagedTrial) else open_staged_trial(trial_dir)
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
        metric_result = run.log({reward_key: parsed.reward}, step=step_index, strict=strict)
        if ledger is not None:
            ledger.update_context(
                reward_logged={
                    "state": "confirmed" if metric_result is not None else "spooled",
                    "key": reward_key,
                    "value": parsed.reward,
                    "step_index": step_index,
                }
            )

    trajectory_report = {"format": parsed.trajectory_format, "expanded": False, "spans": 0}
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
                    CaptureState.confirmed if entry["uploaded"] else CaptureState.upload_failed,
                    artifact_id=entry["artifact_id"],
                    content_hash=entry["content_hash"],
                    size_bytes=entry["size_bytes"],
                    error=None
                    if entry["uploaded"]
                    else "Probe storage did not confirm artifact bytes; staged bytes retained",
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
            manifest_publication={"state": "confirmed", "artifact_id": manifest.get("id")},
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

    staged = trial_dir if isinstance(trial_dir, StagedTrial) else open_staged_trial(trial_dir)
    if staged is None:
        raise ValueError(f"{trial_dir} is not a staged trial (missing {CAPTURE_LEDGER_NAME})")
    kwargs.setdefault("step_index", staged.ledger.context.get("step_index"))
    kwargs.setdefault("expand", False)
    return capture_trial(run, staged, log_reward=False, **kwargs)
