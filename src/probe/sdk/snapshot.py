"""SDK non-disruptive code + environment capture (Execution Record).

``capture_git_snapshot`` records the exact working state (tracked + untracked +
uncommitted) into a private shadow ref ``refs/probe/snapshots/<run_id>`` WITHOUT
touching HEAD, the real index, the branch, or the working tree. It does this with
a throwaway ``GIT_INDEX_FILE``, so there is nothing to restore afterward: nothing
moved. This is the concrete form of the ``/experiment`` launch snapshot.

GPU capture is best-effort ambient context. Dependency capture is STRICT by
default: it records the resolved package list and raises rather than storing an
empty set that reads downstream as a successful capture.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
import tempfile
from typing import Any

from .errors import RosError


class SnapshotError(RosError):
    """Git plumbing failed or the cwd is not a git repository."""


# `ls-remote` is the only git call here that touches the network. Left unbounded
# it can block the start of a training run indefinitely -- on an unreachable host,
# or worse, waiting forever on a credential prompt nobody is there to answer.
_LS_REMOTE_TIMEOUT = 10.0


def _NONINTERACTIVE_ENV() -> dict:
    """Git env that fails fast instead of prompting for credentials."""
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": os.environ.get(
            "GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
        ),
    }


def _git(
    cwd: str,
    *args: str,
    env: dict | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        if check:
            raise SnapshotError(f"git {' '.join(args)}: timed out after {timeout}s") from None
        return ""
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


_SHA_RE = re.compile(r"[0-9a-f]{40,64}")
_CREDENTIAL_URI = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")
# ssh scp-syntax: user@host:path -- strip the userinfo, keep host:path.
_SCP_USERINFO = re.compile(r"^[^/@\s]+@(?=[^/\s]+:)")


def _scrub_remote(url: str | None) -> str | None:
    """Drop credentials from a remote URL before it is recorded anywhere.

    Git remotes routinely carry tokens: GitHub Actions' ``persist-credentials``
    writes ``https://x-access-token:<TOKEN>@github.com/...`` and CI clones use
    ``https://oauth2:$TOKEN@...``. This value lands in run metadata and artifact
    meta, which are readable by anyone with access to the run, so an unscrubbed
    URL copies a live credential into durable storage.
    """
    if not url:
        return None
    scrubbed = _CREDENTIAL_URI.sub(r"\g<scheme><redacted>@", url)
    return _SCP_USERINFO.sub("<redacted>@", scrubbed)


def _remote_url(cwd: str, remote: str) -> str | None:
    return _scrub_remote(_git(cwd, "remote", "get-url", remote, check=False) or None)


def pushed_base(cwd: str) -> tuple[str | None, str | None]:
    """A commit that is an ancestor of HEAD *and* present on a remote.

    Returns ``(commit, scrubbed_remote_url)``, or ``(None, None)`` when nothing
    about the local history can be proven to exist anywhere else. HEAD itself is
    preferred; otherwise the first merge-base found against a remote head is
    used, which is a sound but not necessarily newest common ancestor.

    This is the check whose absence made every prior snapshot a dangling
    pointer. ``git remote -v`` printing a URL proves nothing -- commits can be
    local-only forever. Remote refs are read with ``ls-remote`` (authoritative,
    unlike a possibly-stale remote-tracking ref), and a remote head we do not
    have locally is treated as NOT pushed, so the failure mode is "upload the
    bytes", never "assume they are retrievable".

    The network call is bounded and non-interactive: an unreachable or
    credential-prompting remote must not hang the start of a training run.
    """
    remotes = [r for r in _git(cwd, "remote", check=False).splitlines() if r.strip()]
    if not remotes:
        return None, None
    remote = "origin" if "origin" in remotes else remotes[0]

    head = _git(cwd, "rev-parse", "HEAD", check=False)
    if not head:
        return None, None

    listing = _git(
        cwd, "ls-remote", "--heads", remote,
        check=False, timeout=_LS_REMOTE_TIMEOUT, env=_NONINTERACTIVE_ENV(),
    )
    if not listing:
        return None, None

    shas = []
    for line in listing.splitlines():
        sha = line.split("\t", 1)[0].strip()
        # Advertised object ids are hex; anything else (notably a leading '-')
        # would be parsed as a git option rather than a rev.
        if sha and _SHA_RE.fullmatch(sha):
            shas.append(sha)

    best: str | None = None
    for sha in shas:
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
        if best is None:
            best = _git(cwd, "merge-base", head, sha, check=False) or None
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
    """Classify each captured file as retrievable-from-git or needing upload.

    CLASSIFICATION ONLY. Nothing here moves bytes: a ``source="blob"`` entry
    carries ``path``/``mode``/``sha256``/``size`` and says "git cannot supply
    this, someone must upload it". ``n_pending_upload`` is a work count, not a
    record of work done.

    Per FILE, not per tree: a working tree is normally mostly-committed with a
    few edited or untracked files, and an all-or-nothing tree check would mark
    all of them for upload because one changed.

    A file is referenced only when its content is byte-identical to what a
    PUSHED commit holds at that path. Edited, untracked, unpushed, or no-remote
    all fall through to ``source="blob"``.

    ``tree_sha256`` hashes ``(path, mode, sha256)`` and deliberately excludes the
    source. The same content must produce the same identity whether it came from
    a git reference or an uploaded blob, or an otherwise-identical run with one
    dirty file would compare as different code. Symlinks participate as their
    target so a retarget is visible.

    Raises :class:`SnapshotError` if ``cwd`` is not a git repository -- matching
    ``capture_git_snapshot``. Walking an arbitrary directory would have no
    ``.gitignore`` to honour, and the first thing it would sweep up is the
    ``.env`` the git path is careful to exclude.
    """
    cwd = cwd or os.getcwd()
    if not is_git_repo(cwd):
        raise SnapshotError(f"{cwd} is not a git repository")

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

    # One `git diff` instead of one `git hash-object` per file. Anything tracked
    # in base and absent from this set is byte-identical to base by definition,
    # so the per-file subprocess (measured at ~11ms each) disappears entirely.
    changed: set[str] = set()
    if base:
        changed = {
            p for p in _git(cwd, "diff", "--name-only", base, "--", check=False).splitlines()
            if p.strip()
        }

    entries: list[dict[str, Any]] = []
    for path in paths:
        full = os.path.join(cwd, path)
        if os.path.islink(full):
            target = os.readlink(full)
            entries.append({
                "path": path,
                "mode": "120000",
                "sha256": hashlib.sha256(target.encode()).hexdigest(),
                "size": len(target.encode()),
                "source": "blob",
                "symlink_target": target,
            })
            continue
        # `ls-files --cached` still lists tracked files deleted from the worktree.
        if not os.path.isfile(full):
            continue
        sha, size = _file_sha256(full)
        mode = "100755" if os.access(full, os.X_OK) else "100644"
        entry: dict[str, Any] = {"path": path, "mode": mode, "sha256": sha, "size": size}
        if base and path in base_blobs and path not in changed:
            entry["source"] = "git"
            entry["blob"] = base_blobs[path]
        else:
            entry["source"] = "blob"
        entries.append(entry)

    digest = hashlib.sha256()
    for e in entries:
        digest.update(f"{e['path']}\0{e['mode']}\0{e['sha256']}\n".encode())

    return {
        "entries": entries,
        "tree_sha256": digest.hexdigest(),
        "base_commit": base,
        # Recorded once, not per entry: the URL is identical for every git entry.
        "remote": remote,
        "n_git_referenced": sum(1 for e in entries if e["source"] == "git"),
        "n_pending_upload": sum(1 for e in entries if e["source"] == "blob"),
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
        name = getattr(dist, "name", None)
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
    # NOTE: the digest domain changed in this version -- it now covers sorted
    # `name==version` lines from importlib.metadata, not raw `pip freeze` stdout.
    # The key survives, but its value differs for an unchanged environment, so
    # do not compare values across this upgrade boundary.
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
