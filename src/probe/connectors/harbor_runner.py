"""Instrumented Harbor trials in library mode — the fork-free bridge.

Harbor's per-trial hook surface (``Trial.add_hook`` with ``AGENT_START`` /
``AGENT_END``) is source-public but CLI-invisible: the stock ``harbor run``
command's only extension point is the job-level ``--plugin``, whose callbacks
never see an environment handle.  The sandbox begin/end state capture design
(``docs/2026-07-23-sandbox-state-capture.md``) therefore assumed a "bridge"
process that owns the ``Trial`` object in its own asyncio loop — originally
Miles' private-fork server.  This module is that bridge, shipped in the SDK so
the hooks and sandbox capture work out of the box:

  ``instrument_trial(trial)``   attach the sandbox-state hooks to a Trial you
                                construct yourself — the drop-in for an
                                existing bridge/server loop; and
  ``run_trial(task_path)``      construct + instrument + run ONE trial from a
                                task directory, mirroring ``harbor run`` for
                                the single-trial library case.

The hooks implement ``probe.sandbox-state/1`` (see the design doc): at
``AGENT_START`` a static snapshot binary is uploaded, scans the begin state,
its manifest is downloaded and hash-verified against the stdout trailer, and
every trace is removed before the agent's first action; at ``AGENT_END``
(fires in Harbor's ``finally`` — success, timeout, or agent crash, always
before verification) a FRESH binary computes the end manifest + delta tar,
everything is verified and removed, and the bundle is authored HOST-side into
``<trial_dir>/artifacts/probe-sandbox-state/`` with ``meta.json`` written
last.  From there it is ordinary trial-tree bytes: ``parse_trial`` /
``capture_trial`` / ``stage_trial_export`` carry it with no pipeline changes.

Fail-open is this module's responsibility: Harbor's ``_emit`` propagates hook
exceptions, and a raise inside the ``AGENT_END`` finally would mask the
trial's own result.  Every exception except ``asyncio.CancelledError`` is
swallowed and recorded in the recorder's status and the bundle's meta.json
(``CancelledError`` must unwind: the trial is already dead and losing the end
snapshot there is correct).

Version coupling: harbor is an OPTIONAL dependency (``probe-agent[harbor]``),
imported lazily so the SDK itself never requires it.  The exact API surface
this module touches is asserted by ``verify_harbor_contract()`` — called at
attach time and by the CI canary test — so a harbor upgrade that moves the
undocumented hook API breaks loudly at setup, never silently inside a hook.
Trial-DIRECTORY parsing (``parse_trial``) stays version-agnostic either way.
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..sdk.durable import now_iso as _utc_now
from .sandbox_state import (
    BEGIN_BYTES,
    BEGIN_MANIFEST,
    BUNDLE_DIRNAME,
    END_DELTA,
    END_MANIFEST,
    SCHEMA,
    build_meta,
    machine_to_arch,
    parse_trailer,
    sha256_file,
    write_bundle,
)

#: The harbor release line this module's contract assertions were written
#: against.  Informational — the real gate is verify_harbor_contract().
HARBOR_TESTED_AGAINST = ">=0.20,<0.22"

_CONTAINER_WORKDIR_PREFIX = "/tmp/.psbx-"
_BEGIN_UPLOAD_NAME = "begin.jsonl.gz"


class HarborNotInstalledError(RuntimeError):
    """harbor is not importable; install the ``probe-agent[harbor]`` extra."""


class HarborContractError(RuntimeError):
    """The installed harbor no longer matches the API surface we pin."""


def verify_harbor_contract() -> list[str]:
    """Feature-detect every harbor API this module touches.

    Returns a list of human-readable problems (empty = contract holds).  This
    is the loud-upgrade canary the sandbox-state design doc requires: the
    hook API is load-bearing upstream but undocumented, so we assert its
    shape instead of trusting semver.
    """
    problems: list[str] = []
    try:
        from harbor.environments.base import BaseEnvironment, ExecResult
        from harbor.models.trial.config import (
            AgentConfig,
            EnvironmentConfig,
            TaskConfig,
            TrialConfig,
        )
        from harbor.trial.hooks import TrialEvent
        from harbor.trial.trial import Trial
    except ImportError as exc:
        raise HarborNotInstalledError(
            "harbor is not installed; `pip install 'probe-agent[harbor]'` "
            "(or `pip install harbor`) to use the instrumented trial runner"
        ) from exc

    for event in ("AGENT_START", "AGENT_END"):
        if not hasattr(TrialEvent, event):
            problems.append(f"TrialEvent.{event} is missing")
    for method in ("add_hook", "create", "run"):
        if not callable(getattr(Trial, method, None)):
            problems.append(f"Trial.{method} is missing or not callable")
    for method in ("exec", "upload_file", "download_file"):
        if not callable(getattr(BaseEnvironment, method, None)):
            problems.append(f"BaseEnvironment.{method} is missing or not callable")
    if callable(getattr(BaseEnvironment, "exec", None)):
        import inspect

        params = set(inspect.signature(BaseEnvironment.exec).parameters)
        for name in ("command", "user", "timeout_sec"):
            if name not in params:
                problems.append(f"BaseEnvironment.exec lost its `{name}` parameter")
    exec_fields = getattr(ExecResult, "model_fields", {})
    for name in ("stdout", "return_code"):
        if name not in exec_fields:
            problems.append(f"ExecResult.{name} field is missing")
    for model, names in (
        (TrialConfig, ("task", "trial_name", "trials_dir", "agent", "environment")),
        (TaskConfig, ("path",)),
        (AgentConfig, ("name", "model_name", "kwargs")),
        (EnvironmentConfig, ("type", "kwargs")),
    ):
        model_fields = getattr(model, "model_fields", {})
        for name in names:
            if name not in model_fields:
                problems.append(f"{model.__name__}.{name} field is missing")
    return problems


@dataclass(frozen=True)
class SandboxStateOptions:
    """Knobs for the ``probe.sandbox-state/1`` capture; defaults per the design doc.

    ``begin_timeout_sec=None`` resolves to 120 s, or 600 s when ``begin_bytes``
    is on (archiving + downloading GiBs cannot fit 120 s); explicit values are
    always honored.  ``begin_bytes``/``begin_bytes_ref`` implement the per-task
    shared begin archive (docs/2026-07-29-begin-state-bytes.md): the caller owns
    the first-trial-per-task ledger and passes the opaque sharing key (Harbor's
    ``task_checksum``) on every trial of the task, captured or not.
    """

    begin_timeout_sec: float | None = None
    end_timeout_sec: float = 300.0
    hash_files: bool = False  # sha256 every file (closes mtime-preserving edits)
    root: str = "/"  # scan root for manifests AND the begin archive
    exclude: tuple[str, ...] = ()  # extra path prefixes beyond /proc /sys /dev /logs
    max_files: int | None = None
    max_delta_bytes: int | None = None
    begin_bytes: bool = False  # archive begin bytes of the scanned scope
    begin_bytes_ref: str | None = None  # opaque sharing key stamped into meta.json
    max_begin_bytes: int | None = None

    def resolved_begin_timeout_sec(self) -> float:
        if self.begin_timeout_sec is not None:
            return self.begin_timeout_sec
        return 600.0 if self.begin_bytes else 120.0


@dataclass
class InstrumentedTrialOutcome:
    """What ``run_trial`` returns: harbor's own result plus our capture state."""

    result: Any  # harbor TrialResult
    trial_dir: Path
    sandbox_state: SandboxStateRecorder | None


class SandboxStateRecorder:
    """Per-trial host-side state for the begin/end snapshot protocol.

    The hook callbacks close over the Trial (the hook event carries no
    environment handle), hold the begin manifest in a host tempdir between
    the two hooks, and author the bundle host-side at ``AGENT_END``.  The
    methods themselves never import harbor, so the protocol logic is unit
    testable against fakes without harbor installed.
    """

    def __init__(self, trial: Any, options: SandboxStateOptions) -> None:
        self._trial = trial
        self._options = options
        self._host_dir = Path(tempfile.mkdtemp(prefix="probe-sandbox-state-"))
        self._begin_manifest_host: Path | None = None
        self._begin_trailer: dict[str, Any] | None = None
        self._end_trailer: dict[str, Any] | None = None
        self._begin_at: str | None = None
        self._end_at: str | None = None
        self._arch: str | None = None
        self.status: dict[str, str] = {"begin": "pending", "end": "pending"}
        self.integrity: dict[str, bool] = {
            "begin_verified": False,
            "end_verified": False,
        }
        self.errors: list[str] = []
        self.bundle_dir: Path | None = None

    # -- hook callbacks (harbor awaits these inline) -------------------------
    async def on_agent_start(self, event: Any = None) -> None:
        try:
            await asyncio.wait_for(
                self._begin(), timeout=self._options.resolved_begin_timeout_sec()
            )
            self.status["begin"] = "ok"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — fail-open by design (see module doc)
            self.status["begin"] = "failed"
            self.errors.append(f"begin: {exc}")

    async def on_agent_end(self, event: Any = None) -> None:
        try:
            try:
                await asyncio.wait_for(self._end(), timeout=self._options.end_timeout_sec)
                self.status["end"] = "ok"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — a raise here would mask the trial result
                self.status["end"] = "failed"
                self.errors.append(f"end: {exc}")
            try:
                self._write_bundle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"bundle: {exc}")
        finally:
            shutil.rmtree(self._host_dir, ignore_errors=True)

    # -- protocol phases ------------------------------------------------------
    async def _begin(self) -> None:
        env = self._environment()
        self._arch = await self._detect_arch(env)
        workdir = _CONTAINER_WORKDIR_PREFIX + uuid.uuid4().hex
        try:
            await self._upload_tool(env, workdir)
            result = await env.exec(
                self._snapshot_command(workdir, phase="begin"),
                user="root",
                timeout_sec=int(self._options.resolved_begin_timeout_sec()),
            )
            if result.return_code != 0:
                raise RuntimeError(
                    f"begin exec rc={result.return_code}: {(result.stderr or '')[-500:]}"
                )
            trailer = parse_trailer(result.stdout or "")
            # Download every begin output the trailer names (manifest, and the
            # begin-bytes archive when --bytes ran) — same generic loop as _end,
            # so the archive inherits the manifests' tamper-evidence.
            verified = True
            for name in trailer.get("files", {}):
                host_path = self._host_dir / name
                await env.download_file(f"{workdir}/{name}", host_path)
                verified = self._verify(trailer, name, host_path) and verified
            self.integrity["begin_verified"] = verified
            manifest_host = self._host_dir / BEGIN_MANIFEST
            if not manifest_host.is_file():
                raise RuntimeError("begin trailer names no begin manifest")
            self._begin_trailer = trailer
            self._begin_manifest_host = manifest_host
            self._begin_at = _utc_now()
        finally:
            await self._cleanup(env, workdir)

    async def _end(self) -> None:
        if self._begin_manifest_host is None:
            # Design-doc failure table: begin failed -> recorded, no bundle.
            raise RuntimeError("no begin manifest held; skipping end snapshot")
        env = self._environment()
        workdir = _CONTAINER_WORKDIR_PREFIX + uuid.uuid4().hex
        try:
            await self._upload_tool(env, workdir)
            await env.upload_file(
                self._begin_manifest_host, f"{workdir}/{_BEGIN_UPLOAD_NAME}"
            )
            result = await env.exec(
                self._snapshot_command(workdir, phase="end"),
                user="root",
                timeout_sec=int(self._options.end_timeout_sec),
            )
            if result.return_code != 0:
                raise RuntimeError(
                    f"end exec rc={result.return_code}: {(result.stderr or '')[-500:]}"
                )
            trailer = parse_trailer(result.stdout or "")
            verified = True
            for name in trailer.get("files", {}):
                host_path = self._host_dir / name
                await env.download_file(f"{workdir}/{name}", host_path)
                verified = self._verify(trailer, name, host_path) and verified
            self.integrity["end_verified"] = verified
            self._end_trailer = trailer
            self._end_at = _utc_now()
        finally:
            await self._cleanup(env, workdir)

    # -- pieces ---------------------------------------------------------------
    def _environment(self) -> Any:
        env = getattr(self._trial, "agent_environment", None)
        if env is None:
            raise RuntimeError("trial has no agent_environment")
        return env

    async def _detect_arch(self, env: Any) -> str:
        try:
            result = await env.exec("uname -m", timeout_sec=15)
            arch = (
                machine_to_arch((result.stdout or "").strip())
                if result.return_code == 0
                else None
            )
        except Exception:  # noqa: BLE001 — wrong-arch exec fails fast and is recorded
            arch = None
        if arch is None:
            self.errors.append("arch detection failed; assuming amd64")
            arch = "amd64"
        return arch

    async def _upload_tool(self, env: Any, workdir: str) -> None:
        from .sandbox_state import snapshot_binary_path

        quoted = shlex.quote(workdir)
        result = await env.exec(f"mkdir -p {quoted}", user="root", timeout_sec=30)
        if result.return_code != 0:
            raise RuntimeError(f"mkdir {workdir} rc={result.return_code}")
        await env.upload_file(snapshot_binary_path(self._arch or "amd64"), f"{workdir}/snap")
        result = await env.exec(f"chmod +x {quoted}/snap", user="root", timeout_sec=30)
        if result.return_code != 0:
            raise RuntimeError(f"chmod snap rc={result.return_code}")

    def _snapshot_command(self, workdir: str, *, phase: str) -> str:
        opts = self._options
        quoted = shlex.quote(workdir)
        parts = [f"{quoted}/snap", phase, "--workdir", quoted]
        if phase == "end":
            parts += ["--begin-manifest", f"{quoted}/{_BEGIN_UPLOAD_NAME}"]
        if phase == "begin" and opts.begin_bytes:
            parts.append("--bytes")
            if opts.max_begin_bytes is not None:
                parts += ["--max-begin-bytes", str(opts.max_begin_bytes)]
        if opts.root != "/":
            parts += ["--root", shlex.quote(opts.root)]
        if opts.hash_files:
            parts.append("--hash")
        if opts.exclude:
            parts += ["--exclude", shlex.quote(":".join(opts.exclude))]
        if opts.max_files is not None:
            parts += ["--max-files", str(opts.max_files)]
        if opts.max_delta_bytes is not None:
            parts += ["--max-delta-bytes", str(opts.max_delta_bytes)]
        # Self-imposed deadline just under the exec timeout so the binary
        # exits itself instead of being killed mid-write.
        timeout = (
            opts.resolved_begin_timeout_sec()
            if phase == "begin"
            else opts.end_timeout_sec
        )
        parts += ["--max-seconds", str(max(timeout - 10.0, 5.0))]
        return " ".join(parts)

    def _verify(self, trailer: dict[str, Any], name: str, host_path: Path) -> bool:
        expected = (trailer.get("files", {}).get(name) or {}).get("sha256")
        if not expected or not host_path.is_file():
            self.errors.append(f"{name}: missing from trailer or download")
            return False
        if sha256_file(host_path) != expected:
            self.errors.append(f"{name}: sha256 mismatch vs stdout trailer (kept)")
            return False
        return True

    async def _cleanup(self, env: Any, workdir: str) -> None:
        # Guarded separately so a failed download still leaves the container
        # probe-free; never raises.
        with contextlib.suppress(Exception):
            await env.exec(f"rm -rf {shlex.quote(workdir)}", user="root", timeout_sec=30)

    def _write_bundle(self) -> None:
        if self._begin_manifest_host is None:
            return  # begin never succeeded: recorded on the recorder, no bundle
        files: dict[str, Path] = {BEGIN_MANIFEST: self._begin_manifest_host}
        for name in (END_MANIFEST, END_DELTA, BEGIN_BYTES):
            candidate = self._host_dir / name
            if candidate.is_file():
                files[name] = candidate
        meta = build_meta(
            begin_trailer=self._begin_trailer,
            end_trailer=self._end_trailer,
            status=self.status,
            begin_at=self._begin_at,
            end_at=self._end_at,
            arch=self._arch,
            integrity=self.integrity,
            errors=self.errors,
            begin_bytes_ref=self._options.begin_bytes_ref,
        )
        bundle_dir = self._trial_dir() / "artifacts" / BUNDLE_DIRNAME
        write_bundle(bundle_dir, files, meta)
        self.bundle_dir = bundle_dir

    def _trial_dir(self) -> Path:
        trial_dir = getattr(getattr(self._trial, "paths", None), "trial_dir", None)
        if trial_dir is not None:
            return Path(trial_dir)
        config = self._trial.config
        return Path(config.trials_dir) / config.trial_name

    # -- reporting ------------------------------------------------------------
    def record_install_failure(self, exc: BaseException) -> None:
        """Record that the hooks never attached; ``summary()`` then says why.

        Used by callers that install the hooks fail-open (the capture facade):
        the trial still runs, no phase ever fires, and the summary carries the
        reason instead of a silent ``not_attempted``.
        """
        self.errors.append(f"hook install failed: {type(exc).__name__}: {exc}")

    def attempted(self) -> bool:
        """True once either hook has actually fired."""
        return any(value != "pending" for value in self.status.values())

    def summary(self) -> dict[str, Any]:
        """JSON-safe capture verdict for a bridge's ``context.sandbox_state``.

        ``status`` collapses to the string ``"not_attempted"`` when no hook
        ever fired (environment never started, install failed) so consumers
        need no phase-by-phase probing to tell "off" from "broken" — the
        ``errors`` list carries the why either way.
        """
        return {
            "schema": SCHEMA,
            "status": dict(self.status) if self.attempted() else "not_attempted",
            "arch": self._arch,
            "integrity": dict(self.integrity),
            "errors": list(self.errors),
        }


def instrument_trial(
    trial: Any, *, options: SandboxStateOptions | None = None
) -> SandboxStateRecorder:
    """Attach the sandbox-state hooks to a harbor ``Trial`` you already own.

    Raises :class:`HarborContractError` (loudly, before anything runs) if the
    installed harbor no longer exposes the pinned API surface.  After this,
    everything is fail-open: a broken snapshot never affects the trial.
    """
    problems = verify_harbor_contract()
    if problems:
        raise HarborContractError(
            "installed harbor does not match the pinned API surface "
            f"(tested against {HARBOR_TESTED_AGAINST}): " + "; ".join(problems)
        )
    from harbor.trial.hooks import TrialEvent

    recorder = SandboxStateRecorder(trial, options or SandboxStateOptions())
    trial.add_hook(TrialEvent.AGENT_START, recorder.on_agent_start)
    trial.add_hook(TrialEvent.AGENT_END, recorder.on_agent_end)
    return recorder


async def run_trial(
    task_path: str | Path,
    *,
    trials_dir: str | Path,
    agent: str = "oracle",
    model_name: str | None = None,
    environment: str = "docker",
    trial_name: str | None = None,
    sandbox_state: bool = True,
    options: SandboxStateOptions | None = None,
    agent_kwargs: dict[str, Any] | None = None,
    environment_kwargs: dict[str, Any] | None = None,
) -> InstrumentedTrialOutcome:
    """Run ONE harbor trial in library mode with sandbox capture attached.

    The out-of-the-box door: mirrors ``harbor run -p <task> -a <agent>`` for a
    single trial, except the per-trial hooks (which the CLI cannot attach) are
    wired before ``Trial.run``.  ``agent`` accepts a built-in name (``oracle``)
    or a custom import path (``my.module:MyAgentClass``), same as the CLI's
    ``-a``.  Returns harbor's own ``TrialResult`` plus the trial directory and
    the capture recorder; feed ``outcome.trial_dir`` to ``capture_trial`` to
    publish everything — bundle included — to a run.
    """
    problems = verify_harbor_contract()
    if problems:
        raise HarborContractError(
            "installed harbor does not match the pinned API surface "
            f"(tested against {HARBOR_TESTED_AGAINST}): " + "; ".join(problems)
        )
    from harbor.models.trial.config import (
        AgentConfig,
        EnvironmentConfig,
        TaskConfig,
        TrialConfig,
    )
    from harbor.trial.trial import Trial

    task_path = Path(task_path)
    name = trial_name or f"{task_path.name}__probe-{uuid.uuid4().hex[:8]}"
    agent_field = "import_path" if ":" in agent else "name"
    config = TrialConfig(
        task=TaskConfig(path=task_path),
        trial_name=name,
        trials_dir=Path(trials_dir),
        agent=AgentConfig(
            **{agent_field: agent}, model_name=model_name, kwargs=agent_kwargs or {}
        ),
        environment=EnvironmentConfig(type=environment, kwargs=environment_kwargs or {}),
    )
    trial = await Trial.create(config)
    recorder = instrument_trial(trial, options=options) if sandbox_state else None
    result = await trial.run()
    return InstrumentedTrialOutcome(
        result=result,
        trial_dir=Path(config.trials_dir) / name,
        sandbox_state=recorder,
    )


def run_trial_sync(task_path: str | Path, **kwargs: Any) -> InstrumentedTrialOutcome:
    """Blocking wrapper over :func:`run_trial` for sync call sites."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_trial(task_path, **kwargs))
    raise RuntimeError(
        "run_trial_sync called from inside a running event loop; "
        "await run_trial(...) instead"
    )
