"""Per-launch ephemera: what was true of THIS process start on THIS machine.

Everything here lands in the run's ``metadata["launch"]`` block, never in the
content-addressed execution record -- argv, hostnames, and env values differ per
launch, and hashing them would mint a unique execution record per run and
destroy dedup (design doc D1, docs/2026-08-04-reproducibility-capture-enforcement-design.md).

Every capture is best-effort: failures collect into an ``errors`` list and
capture functions never raise. Absence is recorded, not guessed.

``scrub_argv`` is a separate implementation from ``probe.sdk.redaction``:
that module scrubs structured payloads (dicts/lists) by KEY NAME
(``is_sensitive_key``) and de-credentials URI strings -- it has no notion of a
bare positional argv token "looking like" a secret. Command lines are flat,
positional, and flag-shaped (``--api-key sk-...`` or ``--token=ghp_...``), so
this module matches by VALUE SHAPE and flag name instead of reusing that
module's key-based contract.
"""
from __future__ import annotations

import getpass
import os
import re
import socket
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

LAUNCH_SCHEMA = "probe.launch/1"

# Secret-shaped flags and bare values. Redaction is marked, so a reader knows
# the command line is not verbatim. Secret words match as exact dash/underscore
# segments, not substrings -- `--tokenizer` and `--max-tokens` are reproduction
# data, not secrets.
_SECRET_FLAG = re.compile(
    r"^--?(?:\w+[-_])*?(key|token|secret|password|passwd|credential)(?:[-_]\w+)*?(=.*)?$",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"^(sk-[A-Za-z0-9]{8,}|ghp_[A-Za-z0-9]{20,}|gho_[A-Za-z0-9]{20,}"
    r"|AKIA[A-Z0-9]{12,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]+)"
)


def scrub_argv(argv: list[str]) -> tuple[list[str], bool]:
    """Redact secret-shaped tokens; return (argv, whether anything was redacted)."""
    out: list[str] = []
    scrubbed = False
    redact_next = False
    for arg in argv:
        if redact_next:
            out.append("[redacted]")
            scrubbed = True
            redact_next = False
            continue
        if _SECRET_FLAG.match(arg):
            if "=" in arg:
                out.append(arg.split("=", 1)[0] + "=[redacted]")
                scrubbed = True
            else:
                out.append(arg)
                redact_next = True
            continue
        if _SECRET_VALUE.match(arg):
            out.append("[redacted]")
            scrubbed = True
            continue
        out.append(arg)
    return out, scrubbed


#: Process names that identify HOW a run was launched, when seen in the
#: parent chain. Shells are recorded in the chain but are not "launchers".
_LAUNCHERS = ("sbatch", "srun", "slurmstepd", "kubelet", "containerd-shim", "dockerd")


def _parent_chain(max_hops: int = 10) -> tuple[list[str], str | None]:
    """Walk parent processes to a recognized launcher. Best-effort, bounded."""
    chain: list[str] = []
    launcher: str | None = None
    pid = os.getppid()
    for _ in range(max_hops):
        if pid <= 1:
            break
        name = None
        try:  # Linux
            with open(f"/proc/{pid}/comm") as fh:
                name = fh.read().strip()
            with open(f"/proc/{pid}/stat") as fh:
                pid = int(fh.read().split()[3])
        except OSError:
            try:  # macOS / no procfs
                out = subprocess.run(
                    ["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                    capture_output=True, text=True, timeout=2,
                )
                parts = out.stdout.split(None, 1)
                if len(parts) != 2:
                    break
                name = os.path.basename(parts[1].strip())
                pid = int(parts[0])
            except (OSError, ValueError, subprocess.TimeoutExpired):
                break
        if not name:
            break
        chain.append(name)
        if launcher is None and any(name.startswith(prefix) for prefix in _LAUNCHERS):
            launcher = name
    return chain, launcher


def capture_process(
    argv: list[str] | None = None, cwd: str | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Identity of this launch: command, place, machine, user, lineage."""
    errors: list[str] = []
    raw = list(argv) if argv is not None else list(sys.argv)
    scrubbed_argv, scrubbed = scrub_argv(raw)
    info: dict[str, Any] = {
        "argv": scrubbed_argv,
        "argv_scrubbed": scrubbed,
        "cwd": os.path.abspath(cwd or os.getcwd()),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    entry = raw[0] if raw else None
    if entry:
        info["entrypoint"] = os.path.abspath(entry) if os.path.exists(entry) else entry
    try:
        info["hostname"] = socket.gethostname()
    except OSError as exc:
        errors.append(f"hostname: {exc}")
    try:
        info["user"] = getpass.getuser()
    except Exception as exc:  # getuser can raise KeyError on daemon UIDs
        errors.append(f"user: {exc}")
    chain, launcher = _parent_chain()
    if chain:
        info["parent_chain"] = chain
    if launcher:
        info["launcher"] = launcher
    return info, errors


#: Values captured only for these names — everything else is name-only.
#: Extensible per-site via PROBE_ENV_ALLOWLIST="+VAR1,VAR2". NCCL_* is
#: value-captured wholesale: knob names are the reproduction surface there.
ENV_ALLOWLIST = frozenset({
    "CUDA_VISIBLE_DEVICES", "WORLD_SIZE", "RANK", "LOCAL_RANK",
    "MASTER_ADDR", "MASTER_PORT", "OMP_NUM_THREADS", "PYTHONHASHSEED",
    "SLURM_JOB_ID", "SLURM_ARRAY_TASK_ID",
})


def _allowlist() -> frozenset[str]:
    extra = os.environ.get("PROBE_ENV_ALLOWLIST", "")
    names = {v.strip() for v in extra.lstrip("+").split(",") if v.strip()}
    return ENV_ALLOWLIST | names


def _detect_container(_dockerenv_path: str = "/.dockerenv") -> dict[str, Any] | None:
    """Container context, provenance-tagged. Image identity is PROVENANCE, not
    correctness (design doc D1 / the 2026-07-29 begin-state-bytes decision)."""
    via = None
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        via = "kubernetes"
    elif os.path.exists(_dockerenv_path):
        via = "dockerenv"
    else:
        try:
            with open("/proc/1/cgroup") as fh:
                text = fh.read()
            if any(marker in text for marker in ("docker", "containerd", "kubepods")):
                via = "cgroup"
        except OSError:
            pass
    if via is None:
        return None
    info: dict[str, Any] = {"detected_via": via}
    image = os.environ.get("PROBE_CONTAINER_IMAGE") or os.environ.get("IMAGE")
    if image:
        info["image"] = image
    if via == "kubernetes" and os.environ.get("HOSTNAME"):
        info["pod"] = os.environ["HOSTNAME"]
    return info


def capture_runtime(
    _dockerenv_path: str = "/.dockerenv",
) -> tuple[dict[str, Any], list[str]]:
    """Env-var names (all), values (allowlist + NCCL_* only), container context."""
    errors: list[str] = []
    allow = _allowlist()
    names = sorted(os.environ)
    info: dict[str, Any] = {
        "env_names": names,
        "env_values": {
            k: os.environ[k] for k in names if k in allow or k.startswith("NCCL_")
        },
    }
    container = _detect_container(_dockerenv_path)
    if container:
        info["container"] = container
    return info, errors


_ARGV_SEED = re.compile(r"^--?([\w-]*seed[\w-]*?)(?:=(.*))?$", re.IGNORECASE)


def capture_determinism(
    argv: list[str] | None = None, config: dict[str, Any] | None = None
) -> tuple[dict[str, Any], list[str]]:
    """Seed evidence with provenance. `detected` = observed from the outside
    (env, argv, an already-imported framework); `declared` = the caller put it
    in config. Absence of seeds is recorded as an empty list — honest, not an
    error."""
    errors: list[str] = []
    seeds: list[dict[str, Any]] = []
    # Declared (config) entries are appended before detected (env/argv/framework)
    # ones, so a name shared by both -- e.g. a `seed` in config that also shows
    # up on the command line -- resolves to the detected value when a caller
    # dedupes by name: what actually governed the run outranks what was merely
    # configured for it.
    for key, value in sorted((config or {}).items()):
        if key == "seeds" and isinstance(value, dict):
            for sub, sval in sorted(value.items()):
                seeds.append({
                    "name": f"seeds.{sub}", "value": sval,
                    "provenance": "declared", "source": "config",
                })
        elif re.search(r"(^|_)seed(s)?$", key, re.IGNORECASE):
            seeds.append({
                "name": key, "value": value,
                "provenance": "declared", "source": "config",
            })
    hashseed = os.environ.get("PYTHONHASHSEED")
    if hashseed is not None:
        seeds.append({
            "name": "PYTHONHASHSEED", "value": hashseed,
            "provenance": "detected", "source": "env",
        })
    args = list(argv) if argv is not None else list(sys.argv)
    for i, arg in enumerate(args):
        m = _ARGV_SEED.match(arg)
        if not m:
            continue
        value = m.group(2)
        if value is None and i + 1 < len(args) and not args[i + 1].startswith("-"):
            value = args[i + 1]
        seeds.append({
            "name": m.group(1), "value": value,
            "provenance": "detected", "source": "argv",
        })
    # Frameworks are read from sys.modules, NEVER imported: importing torch
    # costs seconds and pulls CUDA context into a process that may not want it.
    torch = sys.modules.get("torch")
    if torch is not None:
        try:
            seeds.append({
                "name": "torch.initial_seed", "value": int(torch.initial_seed()),
                "provenance": "detected", "source": "torch",
            })
        except Exception as exc:
            errors.append(f"torch seed: {exc}")
    if "numpy" in sys.modules:
        seeds.append({
            "name": "numpy.random", "value": None,
            "provenance": "detected", "source": "numpy",
            "note": "generator present; state not captured",
        })
    return {"seeds": seeds}, errors


def build_launch_block(
    *,
    argv: list[str] | None = None,
    cwd: str | None = None,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose the launch block. NEVER raises — a capture bug must not take
    down the run it is documenting (design principle: block claims, not runs)."""
    from probe import __version__

    errors: list[str] = []
    block: dict[str, Any] = {"schema": LAUNCH_SCHEMA, "probe_version": __version__}
    try:
        block["process"], errs = capture_process(argv=argv, cwd=cwd)
        errors += errs
    except Exception as exc:
        errors.append(f"process: {exc}")
    try:
        block["runtime"], errs = capture_runtime()
        errors += errs
    except Exception as exc:
        errors.append(f"runtime: {exc}")
    try:
        block["determinism"], errs = capture_determinism(argv=argv, config=config)
        errors += errs
    except Exception as exc:
        errors.append(f"determinism: {exc}")
    if errors:
        block["errors"] = errors
    return block
