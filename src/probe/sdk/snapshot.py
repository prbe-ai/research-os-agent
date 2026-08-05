"""SDK non-disruptive code + environment capture (Execution Record).

``capture_git_snapshot`` records the exact working state (tracked + untracked +
uncommitted) into a private shadow ref ``refs/probe/snapshots/<run_id>`` WITHOUT
touching HEAD, the real index, the branch, or the working tree. It does this with
a throwaway ``GIT_INDEX_FILE``, so there is nothing to restore afterward: nothing
moved. This is the concrete form of the ``/experiment`` launch snapshot.

GPU capture is best-effort ambient context. Dependency capture is STRICT by
default, in both directions that matter: it raises rather than storing an empty
package set, AND rather than storing the WRONG one. The second guard exists
because the first is not enough -- an out-of-process caller (the CLI is a
uv-tool install with its own interpreter) that enumerates itself produces a
full, plausible, entirely wrong dependency list, which is indistinguishable
downstream from a correct capture. See ``capture_env``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from functools import lru_cache
from typing import Any

from .errors import RosError
from .hashing import local_file_uri


class SnapshotError(RosError):
    """Git plumbing failed or the cwd is not a git repository."""


# `ls-remote` is the only git call here that touches the network. Left unbounded
# it can block the start of a training run indefinitely -- on an unreachable host,
# or worse, waiting forever on a credential prompt nobody is there to answer.
_LS_REMOTE_TIMEOUT = 10.0
# A verify fetch pulls one commit at depth 1. Bounded for the same reason as
# ls-remote: an audit must not wedge on one unreachable remote.
_VERIFY_TIMEOUT = 20.0


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
    preferred; otherwise the NEWEST merge-base across all remote heads is used,
    so an unrelated stale branch cannot drag the base backwards.

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

    if not shas:
        return None, None

    # One `cat-file --batch-check` for every advertised SHA instead of one
    # `cat-file -e` process per branch. A remote head we do not have locally is
    # treated as NOT pushed, same rule as before. (~6000 branches would hit
    # ARG_MAX on the rev-list below; switch to `rev-list --stdin` if that
    # ever becomes real.)
    probe = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        cwd=cwd, input="\n".join(shas) + "\n",
        capture_output=True, text=True,
    )
    present = []
    for line in probe.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "commit":
            present.append(parts[0])
    if not present:
        return None, None

    # `--boundary` computes the pushed frontier directly, which is merge-safe;
    # the old max-over-pairwise-merge-bases only approximated it. Boundary
    # lines are emitted newest-first, so the first is the newest pushed
    # ancestor. Empty output means HEAD is reachable from an advertised head.
    out = subprocess.run(
        ["git", "rev-list", "--boundary", "HEAD", "--not", *present],
        cwd=cwd, capture_output=True, text=True,
    )
    if out.returncode != 0:
        return None, None
    lines = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    if not lines:
        return head, _remote_url(cwd, remote)
    boundary = [ln[1:] for ln in lines if ln.startswith("-")]
    if not boundary:
        return None, None
    return boundary[0], _remote_url(cwd, remote)


@lru_cache(maxsize=256)
def commit_on_remote(remote: str, commit: str, timeout: float = _VERIFY_TIMEOUT) -> bool:
    """Can this commit actually be fetched from this remote, right now?

    The question a recorded reference is making a claim about, and the one nothing
    asked for 19 runs. `ls-remote` cannot answer it: it lists ref tips, while a
    capture base is usually an ancestor of one. So ask the server for the object
    itself -- a depth-1 fetch of the bare SHA into a throwaway repo, which is
    exactly what a reproduction would do.

    False means "could not prove it", not "definitely gone": an unreachable host,
    a timeout, or a private repo we lack credentials for all land here. That is
    the safe direction -- the alternative is calling a run reproducible on a guess.

    MEMOIZED on (remote, commit), which is what makes a bulk audit affordable.
    Runs from one machine share a base commit, so auditing 200 runs is a handful
    of fetches, not 200. This is never called during a run -- only by an explicit
    `check(verify=True)` -- so it cannot slow training or artifact upload.
    """
    if not remote or not commit:
        return False
    with tempfile.TemporaryDirectory(prefix="probe-verify-") as tmp:
        _git(tmp, "init", "-q", ".", check=False, timeout=timeout)
        try:
            proc = subprocess.run(
                ["git", "fetch", "--depth", "1", "--quiet", remote, commit],
                cwd=tmp, capture_output=True, text=True,
                env=_NONINTERACTIVE_ENV(), timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False
        return proc.returncode == 0


def _file_sha256(path: str) -> tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
            n += len(chunk)
    return h.hexdigest(), n


#: Above this, an included file is RECORDED rather than uploaded. A base
#: checkpoint or a dataset shard is an input whose identity matters and whose
#: bytes are already sitting on a shared volume; copying tens of GB per run to
#: re-store what is already there is not reproducibility, it is duplication.
DEFAULT_REFERENCE_OVER_BYTES = 100 * 1024 * 1024


def _include_entries(
    cwd: str,
    include: list[str],
    already: set[str],
    *,
    reference_over_bytes: int,
) -> list[dict[str, Any]]:
    """Manifest entries for explicitly named paths git would not offer.

    ``.gitignore`` is right about build output and wrong about a downloaded
    dataset, a base checkpoint, or a config deliberately kept out of the repo.
    Those are INPUTS, and the manifest had no way to name them -- so they were
    recorded nowhere, not even as a hash.

    Two outcomes, decided by size:

    ``source="blob"``      small enough to store; travels in the code-bytes archive.
    ``source="reference"`` too large; the path, host and sha256 are recorded so the
                           file is identified and verifiable, and restore reports
                           where it lives instead of pretending it can rebuild it.

    Deliberately NOT recursive by default: a glob names what it names. Passing a
    directory captures it whole, which is the caller's explicit choice.
    """
    import glob as _glob
    import socket

    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for pattern in include:
        matches = _glob.glob(os.path.join(cwd, pattern), recursive=True)
        if not matches:
            raise SnapshotError(f"--include {pattern!r} matched no files under {cwd}")
        for match in sorted(matches):
            targets = [match]
            if os.path.isdir(match):
                targets = sorted(
                    os.path.join(root, name)
                    for root, dirs, files in os.walk(match)
                    for name in files
                    if not any(part in SKIP_DIRS for part in root.split(os.sep))
                )
            for full in targets:
                rel = os.path.relpath(full, cwd)
                if rel.startswith("..") or os.path.isabs(rel):
                    raise SnapshotError(f"--include {pattern!r} escapes {cwd}: {rel}")
                if rel in already or rel in seen or not os.path.isfile(full):
                    continue
                seen.add(rel)
                sha, size = _file_sha256(full)
                entry: dict[str, Any] = {
                    "path": rel,
                    "mode": "100755" if os.access(full, os.X_OK) else "100644",
                    "sha256": sha,
                    "size": size,
                    "included": True,
                }
                if size > reference_over_bytes:
                    entry["source"] = "reference"
                    entry["uri"] = local_file_uri(os.path.abspath(full))
                    entry["host"] = socket.gethostname()
                else:
                    entry["source"] = "blob"
                found.append(entry)
    return found


# Directories that are rebuilt from a lockfile or a cache, never authored. Left
# in, the first snapshot of an ordinary Python project uploads a few hundred MB
# of `.venv` and calls it the experiment's code.
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn",
    ".venv", "venv", "env", "virtualenv",
    "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".ipynb_checkpoints", ".tox", ".eggs", "site-packages",
})

# Secret-shaped names. A directory walk with no `.gitignore` to honour would
# otherwise ship credentials off the machine as a side effect of tracking an
# experiment -- the exact hazard the git path avoids for free. Excluded by
# default and REPORTED, so a caller who genuinely needs one knows it is absent.
SKIP_SECRETS = (
    ".env", ".env.local", ".netrc", ".npmrc", ".pypirc",
    "credentials", "credentials.json", "secrets.json", "service-account.json",
)
SKIP_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore")
SKIP_SECRET_PREFIXES = ("id_rsa", "id_ed25519", "id_ecdsa", ".env.")
SKIP_FILE_SUFFIXES = (".pyc", ".pyo", ".so", ".o", ".DS_Store")


def _skip_reason(name: str) -> str | None:
    """Why this filename is excluded from a non-git capture, or None."""
    lowered = name.lower()
    if name in SKIP_SECRETS or lowered.startswith(SKIP_SECRET_PREFIXES):
        return "secret"
    if lowered.endswith(SKIP_SECRET_SUFFIXES):
        return "secret"
    if lowered.endswith(SKIP_FILE_SUFFIXES) or name == ".DS_Store":
        return "generated"
    return None


def capture_directory_manifest(
    cwd: str | None = None,
    *,
    include: list[str] | None = None,
    reference_over_bytes: int = DEFAULT_REFERENCE_OVER_BYTES,
) -> dict[str, Any]:
    """Manifest for a directory that is NOT a git repository.

    Same shape as :func:`capture_manifest`, with two differences that follow from
    there being no git: every entry is ``source="blob"`` (nothing is retrievable
    from anywhere, so everything must be uploaded), and ``base_commit``/``remote``
    are None.

    The exclusions are the whole design. Git gave the classifier `.gitignore` for
    free; a bare directory has nothing, so the defaults have to be conservative in
    the one direction that matters. ``SKIP_DIRS`` drops what a lockfile rebuilds,
    and ``SKIP_SECRETS`` drops credential-shaped files -- auto-uploading a working
    directory must not be how a `.env` leaves the machine.

    Everything skipped is REPORTED in ``skipped``, because once a filter exists,
    absence from the manifest stops being informative on its own: a reader has to
    be able to tell "not an input" from "excluded by policy".
    """
    cwd = os.path.abspath(cwd or os.getcwd())
    if not os.path.isdir(cwd):
        raise SnapshotError(f"{cwd} is not a directory")

    entries: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for root, dirnames, filenames in os.walk(cwd):
        for name in sorted(dirnames):
            if name in SKIP_DIRS:
                skipped.append(
                    {
                        "path": os.path.relpath(os.path.join(root, name), cwd),
                        "reason": "generated",
                    }
                )
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)

        for name in sorted(filenames):
            full = os.path.join(root, name)
            rel = os.path.relpath(full, cwd)
            reason = _skip_reason(name)
            if reason is not None:
                skipped.append({"path": rel, "reason": reason})
                continue
            if os.path.islink(full):
                target = os.readlink(full)
                entries.append({
                    "path": rel,
                    "mode": "120000",
                    "sha256": hashlib.sha256(target.encode()).hexdigest(),
                    "size": len(target.encode()),
                    "source": "blob",
                    "symlink_target": target,
                })
                continue
            if not os.path.isfile(full):
                continue
            sha, size = _file_sha256(full)
            entries.append({
                "path": rel,
                "mode": "100755" if os.access(full, os.X_OK) else "100644",
                "sha256": sha,
                "size": size,
                "source": "blob",
            })

    if include:
        entries.extend(
            _include_entries(
                cwd,
                include,
                {e["path"] for e in entries},
                reference_over_bytes=reference_over_bytes,
            )
        )
    entries.sort(key=lambda e: e["path"])
    digest = hashlib.sha256()
    for e in entries:
        digest.update(f"{e['path']}\0{e['mode']}\0{e['sha256']}\n".encode())

    return {
        "entries": entries,
        "tree_sha256": digest.hexdigest(),
        "base_commit": None,
        "remote": None,
        "n_git_referenced": 0,
        "n_pending_upload": sum(1 for e in entries if e["source"] == "blob"),
        "n_referenced_offsite": sum(1 for e in entries if e["source"] == "reference"),
        "vcs": None,
        "skipped": skipped,
    }


def capture_manifest(
    cwd: str | None = None,
    *,
    include: list[str] | None = None,
    reference_over_bytes: int = DEFAULT_REFERENCE_OVER_BYTES,
) -> dict[str, Any]:
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

    Outside a git repository this delegates to :func:`capture_directory_manifest`
    rather than raising. There is no reference half without git -- no pushed base,
    no blob ids -- so every file is ``source="blob"`` and every file gets uploaded.
    That used to raise, which was defensible only while no uploader existed: the
    directory with NOTHING retrievable anywhere was the one case refused outright.
    """
    cwd = cwd or os.getcwd()
    if not is_git_repo(cwd):
        return capture_directory_manifest(cwd)

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

    if include:
        entries.extend(
            _include_entries(
                cwd,
                include,
                {e["path"] for e in entries},
                reference_over_bytes=reference_over_bytes,
            )
        )
        entries.sort(key=lambda e: e["path"])

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
        "n_referenced_offsite": sum(1 for e in entries if e["source"] == "reference"),
    }



# Refuse rather than silently ship a partial archive. Raise it with
# `--max-upload-mb` when a repo genuinely needs to.
DEFAULT_MAX_UPLOAD_BYTES = 256 * 1024 * 1024

CODE_BYTES_ARTIFACT = "code-bytes"


class SnapshotTooLarge(SnapshotError):
    """The pending bytes exceed the cap. Never truncated to fit."""


def pending_entries(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The files git cannot supply -- edited, untracked, unpushed, or no remote.

    These are exactly the files that make a run unreproducible on any other
    machine: the manifest records a sha256 for them, and a sha256 verifies a file
    you already have rather than producing one you do not.

    ``source="reference"`` is deliberately excluded: those are the deliberately
    off-platform ones (a base checkpoint on a shared volume), identified and
    verifiable but not copied.
    """
    return [e for e in (manifest.get("entries") or []) if e.get("source") == "blob"]


def build_pending_archive(
    cwd: str,
    manifest: dict[str, Any],
    dest: str,
    *,
    max_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
) -> dict[str, Any]:
    """Tar+gzip exactly the pending files into ``dest``. Returns a summary.

    DETERMINISTIC: mtime, uid/gid, owner names and member order are all
    normalised, and gzip's own mtime header is zeroed. Two identical trees
    therefore produce byte-identical archives, which is what lets the upload's
    content-addressed ``have`` check short-circuit -- a 200-run sweep over
    unchanged code uploads once and skips the other 199.

    Modes and symlinks survive: the manifest already distinguishes ``100755``
    from ``100644`` and records ``symlink_target`` for ``120000`` entries, and a
    restored tree that lost the executable bit does not run.

    Raises :class:`SnapshotTooLarge` rather than dropping files. A silently
    partial archive that reports success is the exact failure this whole path
    exists to remove.
    """
    pending = pending_entries(manifest)
    total = sum(int(e.get("size") or 0) for e in pending)
    if total > max_bytes:
        raise SnapshotTooLarge(
            f"pending code bytes are {total / 1e6:.1f} MB, over the "
            f"{max_bytes / 1e6:.0f} MB cap; raise --max-upload-mb or commit and "
            "push the large files so git can supply them instead"
        )

    import gzip
    import tarfile

    # gzip mtime=0 AND filename="" so the container header is deterministic too,
    # not just the tar. Without the explicit filename, GzipFile copies the output
    # file's own name into the header -- so writing the same tree to two paths
    # produced two different hashes and the upload's content-addressed dedup
    # never fired. A test pins this.
    with open(dest, "wb") as raw, gzip.GzipFile(
        filename="", fileobj=raw, mode="wb", mtime=0
    ) as gz:
        with tarfile.open(fileobj=gz, mode="w") as archive:
            for entry in sorted(pending, key=lambda e: e["path"]):
                path = entry["path"]
                full = os.path.join(cwd, path)
                if entry.get("mode") == "120000":
                    info = tarfile.TarInfo(path)
                    info.type = tarfile.SYMTYPE
                    info.linkname = entry.get("symlink_target") or os.readlink(full)
                else:
                    if not os.path.isfile(full):
                        continue  # deleted between classify and archive
                    info = tarfile.TarInfo(path)
                    info.size = os.path.getsize(full)
                    info.mode = 0o755 if entry.get("mode") == "100755" else 0o644
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                if info.type == tarfile.SYMTYPE:
                    archive.addfile(info)
                else:
                    with open(full, "rb") as fh:
                        archive.addfile(info, fh)

    sha, size = _file_sha256(dest)
    return {
        "path": dest,
        "sha256": sha,
        "size_bytes": size,
        "n_files": len(pending),
        "uncompressed_bytes": total,
    }




# THE enumeration -- there is deliberately only one, and it always runs inside
# the TARGET interpreter, even when that is this process.
#
# An in-process variant used to exist alongside this for the SDK path. It was
# deleted rather than kept-and-tested: two implementations of one algorithm
# whose outputs are HASHED into `env_ref` will drift, and the drift surfaces as
# two identical environments comparing unequal -- indistinguishable from a real
# dependency change. Its only unique capability was seeing runtime `sys.path`
# mutations, and those are derivable: the `sys.path.insert` line lives in the
# code the snapshot already captures. The spawn costs ~50ms ONCE per run (a
# snapshot is a launch-time act, not a training-loop one), so there is no hot
# path to protect here -- that constraint belongs to `probe log`.
#
# Written for the oldest interpreter a project venv might hold: `dist.name` is
# 3.10+, `dist.metadata['Name']` is not. First occurrence wins, matching
# `sys.path` shadowing order. No pip required -- `uv venv` installs none.
_ENUMERATE_PROGRAM = r"""
import json, sys
from importlib import metadata

seen = {}
for dist in metadata.distributions():
    try:
        name = dist.metadata["Name"]
    except Exception:
        name = None
    if not name:
        continue
    if name not in seen:
        seen[name] = dist.version or "0"
json.dump(
    {
        "python": ".".join(str(p) for p in sys.version_info[:3]),
        "executable": sys.executable,
        "packages": sorted("%s==%s" % (n, v) for n, v in seen.items()),
    },
    sys.stdout,
)
"""

_ENUMERATE_TIMEOUT = 60.0

# Layout of a venv, POSIX and Windows.
_VENV_BIN = ("bin", "Scripts")
_VENV_PYTHON = ("python", "python3", "python.exe")
# Ordered: a project that has both `.venv` and `venv` almost always maintains
# the first (uv, poetry and pdm all create `.venv`) and keeps the other stale.
_VENV_DIR_NAMES = (".venv", "venv", "env")


def venv_python(venv: str) -> str | None:
    """The interpreter inside ``venv``, or None if it does not look like a venv."""
    for bindir in _VENV_BIN:
        for name in _VENV_PYTHON:
            candidate = os.path.join(venv, bindir, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def find_venv(cwd: str | None = None) -> tuple[str | None, str | None]:
    """Locate the virtualenv whose packages belong to the code at ``cwd``.

    Returns ``(venv_root, resolved_via)``, or ``(None, None)``.

    Order, strongest tie to the snapshotted code first:

    1. a venv directory inside the project -- ``.venv`` / ``venv`` / ``env``,
       walking ``cwd`` upward and stopping AT the git toplevel. The snapshot is
       of a repository, so the search is bounded by that repository; without the
       bound a project with no venv would silently adopt one from a parent
       directory that has nothing to do with it.
    2. ``VIRTUAL_ENV`` -- an activated env, including the one ``uv run`` exports.
    3. ``CONDA_PREFIX`` -- conda envs live outside the project by design.
    """
    cwd = os.path.abspath(cwd or os.getcwd())

    top = _git(cwd, "rev-parse", "--show-toplevel", check=False)
    ceiling = os.path.abspath(top) if top else cwd

    directory = cwd
    while True:
        for name in _VENV_DIR_NAMES:
            candidate = os.path.join(directory, name)
            if venv_python(candidate):
                return candidate, "project-venv"
        if os.path.normcase(directory) == os.path.normcase(ceiling):
            break
        parent = os.path.dirname(directory)
        if parent == directory:
            break
        directory = parent

    for var in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        value = os.environ.get(var)
        if value and venv_python(value):
            return os.path.abspath(value), var

    return None, None


def _enumerate_foreign(python: str) -> tuple[str, list[str]]:
    """``(python_version, packages)`` as reported BY ``python`` itself.

    ``PYTHONHOME`` is dropped because it repoints an interpreter's stdlib at
    another installation's, which breaks a foreign interpreter outright. The
    rest of the environment is inherited on purpose: ``PYTHONPATH`` and user
    site-packages genuinely contribute modules to the run being recorded, so
    isolating them would record an environment nobody actually uses.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONHOME"}
    try:
        proc = subprocess.run(
            [python, "-c", _ENUMERATE_PROGRAM],
            capture_output=True,
            text=True,
            timeout=_ENUMERATE_TIMEOUT,
            env=env,
        )
    except subprocess.TimeoutExpired:
        raise SnapshotError(
            f"{python} did not report its packages within {_ENUMERATE_TIMEOUT}s"
        ) from None
    except OSError as exc:
        raise SnapshotError(f"could not run {python}: {exc}") from exc
    if proc.returncode != 0:
        raise SnapshotError(
            f"{python} could not enumerate its packages: {proc.stderr.strip()[:400]}"
        )
    try:
        payload = json.loads(proc.stdout)
        return payload["python"], list(payload["packages"])
    except (ValueError, KeyError, TypeError) as exc:
        raise SnapshotError(f"{python} returned an unreadable package list: {exc}") from exc


def _is_inside(path: str, root: str) -> bool:
    try:
        return not os.path.relpath(os.path.realpath(path), os.path.realpath(root)).startswith("..")
    except ValueError:  # different drives on Windows
        return False


def capture_env(
    cwd: str | None = None,
    *,
    venv: str | None = None,
    detect_venv: bool = False,
    strict: bool = True,
) -> dict[str, Any]:
    """Resolved dependency list for the reproducibility manifest.

    Stores the PACKAGES THEMSELVES, not just a digest of them. An earlier
    implementation kept only ``packages_sha256`` + a count, which can tell you
    that two runs differed and can never tell you what either one used -- the
    same failure shape as recording a commit SHA whose objects are gone.

    WHICH environment gets recorded is the whole problem this signature exists to
    solve. Enumerating the calling process is right for the SDK, whose
    ``run.snapshot()`` runs inside the training venv, and wrong for the CLI,
    which is a uv-tool install with its own interpreter and its own ~40
    packages. Recording those is not a degraded capture; it is a confident,
    plausible, WRONG one, and it is exactly the "different venvs" failure the
    execution record exists to eliminate.

    Resolution order:

    - ``venv`` -- an explicit path always wins.
    - ``detect_venv`` -- resolve the project's venv via :func:`find_venv`. This
      is the out-of-process caller's mode: the CLI is not the thing that runs
      the code, so its own interpreter is evidence of nothing.
    - neither -- the current interpreter, enumerated in-process. The default,
      because an in-process caller IS the environment being recorded.

    A resolved venv is read by running ``importlib.metadata`` under ITS
    interpreter, so the answer comes from the environment itself rather than
    from a guess about its layout, and no ``pip`` is required (``uv venv``
    installs none).

    ``strict`` (the default) covers two distinct failures, both of which used to
    pass silently:

    - the dependency set cannot be resolved, or resolves to nothing;
    - ``detect_venv`` found no project venv and the running interpreter is
      foreign to ``cwd`` -- i.e. we are about to record the wrong environment.

    The returned mapping carries ``venv`` / ``python_executable`` /
    ``resolved_via`` so that a capture which picked the wrong environment is
    visible rather than indistinguishable from a correct one. Those three are
    PROVENANCE, not identity: split them off with
    :func:`split_env_provenance` before the result reaches an execution record.
    """
    resolved_via = "interpreter"
    venv_root: str | None = None

    if venv is not None:
        venv_root, resolved_via = os.path.abspath(venv), "explicit"
        if venv_python(venv_root) is None:
            raise SnapshotError(f"{venv} is not a virtualenv (no bin/python inside)")
    elif detect_venv:
        venv_root, found_via = find_venv(cwd)
        if venv_root is not None:
            resolved_via = found_via or "project-venv"

    python_exe = sys.executable
    if venv_root is not None:
        python_exe = venv_python(venv_root) or sys.executable
    elif detect_venv:
        # No project venv, so the only candidate left is the interpreter running
        # this code -- acceptable only when it lives in the tree being recorded
        # (probe pip-installed into the project's own env). Anything else is the
        # caller's own toolchain, and recording it is the bug, not a fallback.
        root = cwd or os.getcwd()
        top = _git(root, "rev-parse", "--show-toplevel", check=False) or root
        if _is_inside(sys.prefix, top):
            resolved_via = "interpreter"
        elif strict:
            raise SnapshotError(
                f"no virtualenv found for {os.path.abspath(root)}, and the running "
                f"interpreter ({sys.prefix}) is outside it -- refusing to record "
                "this process's own packages as the project's. Pass --venv PATH "
                "(SDK: venv=...), or activate the environment the code runs in."
            )
        else:
            resolved_via = "unresolved-fallback"

    info: dict[str, Any] = {
        "python": sys.version.split()[0],
        "python_executable": python_exe,
        "venv": venv_root,
        "resolved_via": resolved_via,
    }

    # A frozen build (PyInstaller and friends) rewrites sys.executable to the
    # bundled application, which does not understand `-c` -- it would re-run the
    # app. Refuse instead of enumerating whatever that produces.
    if getattr(sys, "frozen", False) and python_exe == sys.executable:
        raise SnapshotError(
            "cannot enumerate packages from a frozen interpreter "
            f"({sys.executable}); pass --venv PATH (SDK: venv=...) naming the "
            "environment whose packages should be recorded"
        )

    try:
        info["python"], packages = _enumerate_foreign(python_exe)
    except SnapshotError:
        raise
    except Exception as exc:  # noqa: BLE001 - surfaced, never swallowed
        if strict:
            raise SnapshotError(f"could not resolve installed distributions: {exc}") from exc
        return info

    if not packages and strict:
        raise SnapshotError(
            f"resolved zero installed distributions from {python_exe}; refusing to "
            "record an empty dependency set as if it were captured"
        )

    info["packages"] = packages
    info["package_count"] = len(packages)
    # NOTE: the digest domain covers sorted `name==version` lines from
    # importlib.metadata, not raw `pip freeze` stdout. Do not compare values
    # across that upgrade boundary.
    info["packages_sha256"] = hashlib.sha256("\n".join(packages).encode()).hexdigest()
    return info


# How the environment was FOUND, as against what the environment IS. Machine
# -specific by nature: the same packages live at /Users/x/p/.venv on a laptop
# and /workspace/p/.venv on a training box.
ENV_PROVENANCE_KEYS = ("venv", "python_executable", "resolved_via")


def split_env_provenance(info: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split :func:`capture_env` output into ``(identity, provenance)``.

    The execution record's ``content_hash`` covers the WHOLE ``deps`` section
    (research-os ``app/execution/service.py`` ``canonical_hash``, which
    json-dumps every section sorted). Any machine-specific value inside it makes
    two genuinely identical environments at different paths hash differently --
    and ``env_ref`` equality is precisely what a reader compares to ask "did
    these two runs use the same environment?". Recording the venv path there
    would answer "no" to a "yes", which is the same confidently-wrong shape this
    module exists to eliminate.

    So identity (python + packages) is hashed, and provenance rides on the
    ``code-snapshot`` artifact meta, which ``Client.check_run`` already reads.
    """
    provenance = {k: info[k] for k in ENV_PROVENANCE_KEYS if k in info}
    identity = {k: v for k, v in info.items() if k not in ENV_PROVENANCE_KEYS}
    return identity, provenance


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
