"""SDK non-disruptive code + environment capture (Execution Record).

``capture_git_snapshot`` records the exact working state (tracked + untracked +
uncommitted) into a private shadow ref ``refs/probe/snapshots/<run_id>`` WITHOUT
touching HEAD, the real index, the branch, or the working tree. It does this with
a throwaway ``GIT_INDEX_FILE``, so there is nothing to restore afterward: nothing
moved. This is the concrete form of the ``/experiment`` launch snapshot.

Environment and GPU capture are best-effort ambient context (deps, in-container
``nvidia-smi``) for the reproducibility manifest.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
from typing import Any

from .errors import RosError


class SnapshotError(RosError):
    """Git plumbing failed or the cwd is not a git repository."""


def _git(cwd: str, *args: str, env: dict | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise SnapshotError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def is_git_repo(cwd: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def capture_git_snapshot(run_id: str, cwd: str | None = None) -> dict[str, Any]:
    """Capture full working state into ``refs/probe/snapshots/<run_id>``.

    Returns metadata about the snapshot. Never mutates HEAD / index / worktree.
    Raises :class:`SnapshotError` if ``cwd`` is not a git repo.
    """
    cwd = cwd or os.getcwd()
    if not is_git_repo(cwd):
        raise SnapshotError(f"{cwd} is not a git repository")

    head = _git(cwd, "rev-parse", "HEAD", check=False) or None
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD", check=False) or None
    dirty = bool(_git(cwd, "status", "--porcelain", check=False))

    tmp = tempfile.NamedTemporaryFile(prefix="probe-index-", delete=False)
    tmp.close()
    index_file = tmp.name
    try:
        env = {**os.environ, "GIT_INDEX_FILE": index_file}
        # Seed the temp index from HEAD so tracked deletions/renames are captured,
        # then stage everything (tracked + untracked + uncommitted) into it.
        if head is not None:
            _git(cwd, "read-tree", "HEAD", env=env)
        _git(cwd, "add", "-A", env=env)
        tree = _git(cwd, "write-tree", env=env)

        msg = f"probe snapshot for run {run_id}"
        if head is not None:
            commit = _git(cwd, "commit-tree", tree, "-p", head, "-m", msg)
        else:
            commit = _git(cwd, "commit-tree", tree, "-m", msg)
        ref = f"refs/probe/snapshots/{run_id}"
        _git(cwd, "update-ref", ref, commit)
    finally:
        try:
            os.unlink(index_file)
        except OSError:
            pass

    return {
        "commit": commit,
        "ref": ref,
        "branch": branch,
        "head": head,
        "dirty": dirty,
    }


def capture_env() -> dict[str, Any]:
    """Best-effort dependency fingerprint (pip freeze -> sha256 + count)."""
    info: dict[str, Any] = {"python": sys.version.split()[0]}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if proc.returncode == 0:
            frozen = proc.stdout
            info["packages_sha256"] = hashlib.sha256(frozen.encode()).hexdigest()
            info["package_count"] = len([ln for ln in frozen.splitlines() if ln.strip()])
    except (OSError, subprocess.SubprocessError):
        pass
    return info


def capture_gpu() -> list[dict[str, Any]]:
    """Best-effort in-container GPU inventory via nvidia-smi (RunPod path)."""
    query = "index,name,memory.total,driver_version"
    try:
        proc = subprocess.run(
            ["nvidia-smi", f"--query-gpu={query}", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    gpus: list[dict[str, Any]] = []
    for line in proc.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 4:
            gpus.append(
                {
                    "index": parts[0],
                    "name": parts[1],
                    "memory_total_mib": parts[2],
                    "driver_version": parts[3],
                }
            )
    return gpus
