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


def _remote_url(cwd: str, remote: str) -> str | None:
    return _git(cwd, "remote", "get-url", remote, check=False) or None


def pushed_base(cwd: str) -> tuple[str | None, str | None]:
    """Newest commit that is an ancestor of HEAD *and* present on a remote.

    Returns ``(commit, remote_url)``, or ``(None, None)`` when nothing about the
    local history can be proven to exist anywhere else.

    This is the check whose absence made every prior snapshot a dangling
    pointer. ``git remote -v`` printing a URL proves nothing -- commits can be
    local-only forever. The remote's refs are read with ``ls-remote`` (the
    authoritative answer, not a possibly-stale remote-tracking ref), and a remote
    head we do not have locally is treated as NOT pushed, so the failure mode is
    "upload the bytes", never "assume they are retrievable".
    """
    remotes = [r for r in _git(cwd, "remote", check=False).splitlines() if r.strip()]
    if not remotes:
        return None, None
    remote = "origin" if "origin" in remotes else remotes[0]

    head = _git(cwd, "rev-parse", "HEAD", check=False)
    if not head:
        return None, None

    listing = _git(cwd, "ls-remote", "--heads", remote, check=False)
    if not listing:
        return None, None

    best: str | None = None
    for line in listing.splitlines():
        sha = line.split("\t", 1)[0].strip()
        if not sha:
            continue
        # An object we never fetched cannot be reasoned about; skip it rather
        # than assuming reachability.
        if subprocess.run(
            ["git", "cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=cwd, capture_output=True, text=True,
        ).returncode != 0:
            continue
        if subprocess.run(
            ["git", "merge-base", "--is-ancestor", head, sha],
            cwd=cwd, capture_output=True, text=True,
        ).returncode == 0:
            return head, _remote_url(cwd, remote)
        mb = _git(cwd, "merge-base", head, sha, check=False)
        if mb and best is None:
            best = mb
    return best, (_remote_url(cwd, remote) if best else None)


def _file_sha256(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


def capture_manifest(cwd: str | None = None) -> dict[str, Any]:
    """Classify every captured file as retrievable-from-git or needs-upload.

    Per FILE, not per tree: a working tree is normally mostly-committed with a
    few edited or untracked files, and an all-or-nothing tree check would upload
    all of them because one changed.

    A file is referenced only when its content is byte-identical to what a
    PUSHED commit holds at that path. Everything else -- edited, untracked,
    unpushed, no remote, not a repo -- carries its bytes.

    ``tree_sha256`` hashes ``(path, mode, sha256)`` and deliberately does NOT
    include the source. The same content must produce the same identity whether
    it arrived as a git reference or an uploaded blob, or an otherwise-identical
    run with one dirty file would compare as different code.
    """
    cwd = cwd or os.getcwd()
    entries: list[dict[str, Any]] = []

    if not is_git_repo(cwd):
        base, remote = None, None
        paths = sorted(
            os.path.relpath(os.path.join(root, f), cwd)
            for root, _dirs, files in os.walk(cwd)
            for f in files
        )
    else:
        base, remote = pushed_base(cwd)
        paths = sorted(
            p for p in _git(
                cwd, "ls-files", "--cached", "--others", "--exclude-standard"
            ).splitlines() if p.strip()
        )

    base_blobs: dict[str, str] = {}
    if base:
        for line in _git(cwd, "ls-tree", "-r", base, check=False).splitlines():
            meta, _, path = line.partition("\t")
            bits = meta.split()
            if len(bits) >= 3 and path:
                base_blobs[path] = bits[2]

    for path in paths:
        full = os.path.join(cwd, path)
        # `ls-files --cached` still lists tracked files deleted from the worktree.
        if not os.path.isfile(full) or os.path.islink(full):
            continue
        sha, size = _file_sha256(full)
        mode = "100755" if os.access(full, os.X_OK) else "100644"
        entry: dict[str, Any] = {"path": path, "mode": mode, "sha256": sha, "size": size}
        if base and path in base_blobs:
            working_blob = _git(cwd, "hash-object", "--", full, check=False)
            if working_blob and working_blob == base_blobs[path]:
                entry["source"] = "git"
                entry["blob"] = working_blob
                entry["commit"] = base
                entry["remote"] = remote
                entries.append(entry)
                continue
        entry["source"] = "blob"
        entries.append(entry)

    digest = hashlib.sha256()
    for e in entries:
        digest.update(f"{e['path']}\0{e['mode']}\0{e['sha256']}\n".encode())

    return {
        "entries": entries,
        "tree_sha256": digest.hexdigest(),
        "base_commit": base,
        "remote": remote,
        "n_referenced": sum(1 for e in entries if e["source"] == "git"),
        "n_uploaded": sum(1 for e in entries if e["source"] == "blob"),
    }


def _installed_distributions() -> list[str]:
    """``name==version`` for every installed distribution, sorted.

    Uses ``importlib.metadata`` rather than shelling out to ``pip freeze``. A
    ``uv venv`` ships no pip, so ``sys.executable -m pip`` returns non-zero there
    and the old implementation silently recorded nothing at all.
    """
    from importlib import metadata

    seen: dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata["Name"] if dist.metadata else None
        if not name:
            continue
        seen[name] = dist.version or "0"
    return sorted(f"{n}=={v}" for n, v in seen.items())


def capture_env(strict: bool = True) -> dict[str, Any]:
    """Resolved dependency list for the reproducibility manifest.

    Stores the PACKAGES THEMSELVES, not just a digest of them. The previous
    implementation kept only ``packages_sha256`` + a count, which can tell you
    that two runs differed and can never tell you what either one used -- the
    same failure shape as recording a commit SHA whose objects are gone.

    ``strict`` (the default) raises when the dependency set cannot be resolved.
    Silently returning ``{"python": ...}`` is how real runs ended up with no
    dependency record and nobody noticed.
    """
    info: dict[str, Any] = {"python": sys.version.split()[0]}
    try:
        packages = _installed_distributions()
    except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
        if strict:
            raise SnapshotError(f"could not resolve installed distributions: {exc}") from exc
        return info
    if not packages and strict:
        raise SnapshotError(
            "resolved zero installed distributions; refusing to record an empty "
            "dependency set as if it were captured"
        )
    joined = "\n".join(packages)
    info["packages"] = packages
    info["package_count"] = len(packages)
    # Retained so existing consumers keyed on the digest keep working.
    info["packages_sha256"] = hashlib.sha256(joined.encode()).hexdigest()
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
