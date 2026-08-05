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
# the command line is not verbatim.
_SECRET_FLAG = re.compile(
    r"^--?[\w-]*(key|token|secret|password|passwd|credential)[\w-]*(=.*)?$", re.IGNORECASE
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
