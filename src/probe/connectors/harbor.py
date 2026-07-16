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

import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from . import atif

if TYPE_CHECKING:
    from ..sdk.run import Run

SCHEMA_VERSION = "1.0"
MANIFEST_KIND = "harbor_trial"

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


def parse_trial(trial_dir: str | Path) -> ParsedTrial:
    """Read a Harbor trial directory, tolerating any subset of the contract."""
    root = Path(trial_dir).expanduser()
    if not root.is_dir():
        raise FileNotFoundError(f"{root} is not a trial directory")
    files = sorted(
        p for p in root.rglob("*")
        if p.is_file() and not p.is_symlink() and not p.name.startswith(".")
    )
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
    trial_dir: str | Path,
    *,
    step_index: int | None = None,
    environment: dict | None = None,
    source_mode: str = "local",
    reward_key: str = "reward",
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
    parsed = parse_trial(trial_dir)
    status = "failed" if parsed.exception else "completed"
    span_id = run.span(
        "rollout",
        name=parsed.name,
        step_index=step_index,
        external_key=parsed.name,
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
    if parsed.reward is not None:
        run.log({reward_key: parsed.reward}, step=step_index, strict=strict)

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
        uploaded = run.log_artifact(
            f"{parsed.name}/{rel}",
            path=str(path),
            kind="file",
            meta={"role": role, "trial": parsed.name, "path": rel},
            span_id=span_id,
            step_index=step_index,
            strict=strict,
        )
        entry: dict[str, Any] = {"role": role, "path": rel}
        if isinstance(uploaded, dict):
            entry["artifact_id"] = uploaded.get("id")
            entry["content_hash"] = uploaded.get("content_hash")
            entry["size_bytes"] = uploaded.get("size_bytes")
            entry["uploaded"] = not uploaded.get("is_reference", False)
        else:  # spooled fail-open: no row yet, replayed at flush()
            entry["artifact_id"] = None
            entry["uploaded"] = False
        file_entries.append(entry)

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
        "source": {"mode": source_mode, "rollout_id": step_index},
        "files": file_entries,
    }
    manifest = run.log_artifact(
        parsed.name,
        kind=MANIFEST_KIND,
        meta=manifest_meta,
        span_id=span_id,
        step_index=step_index,
        strict=strict,
    )
    return {
        "trial": parsed.name,
        "span_id": span_id,
        "reward": parsed.reward,
        "manifest": manifest,
        "files": file_entries,
        "trajectory": trajectory_report,
    }
