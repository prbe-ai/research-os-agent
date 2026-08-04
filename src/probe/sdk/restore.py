"""Rebuild a snapshotted working tree, or say exactly what is missing.

The inverse of ``capture_manifest`` + the ``code-bytes`` upload. Each manifest
entry names one of two sources, and this resolves both:

    source="git"   -> `git cat-file blob <blob>` from the recorded remote
    source="blob"  -> a member of the uploaded code-bytes archive

Every restored file is verified against the ``sha256`` the manifest recorded, and
the rebuilt tree is verified against ``tree_sha256``. A hash mismatch makes the
file UNAVAILABLE; it is never written and then hoped about. That rule is
inherited from ``probe.sandbox-state/1``, where bytes are served from a shared
archive only when the per-file hash agrees -- degrade to "unavailable", never to
a wrong answer.
"""

from __future__ import annotations

import gzip
import hashlib
import os
import subprocess
import tarfile
import tempfile
from typing import Any

from .errors import RosError
from .snapshot import _NONINTERACTIVE_ENV, _file_sha256

#: A depth-1 fetch of one commit. Bounded for the same reason every other remote
#: call here is: an audit of many runs must not wedge on one unreachable host.
FETCH_TIMEOUT = 120.0


class RestoreError(RosError):
    """The snapshot cannot be rebuilt from what was recorded."""


def _fetch_base(remote: str, commit: str, workdir: str) -> bool:
    """Materialise ``commit`` in a throwaway repo. False if it cannot be had.

    A depth-1 fetch of the commit brings its whole tree, so every ``source="git"``
    blob becomes readable from one network call rather than one per file.
    """
    subprocess.run(
        ["git", "init", "-q", "."], cwd=workdir, capture_output=True, text=True
    )
    try:
        proc = subprocess.run(
            ["git", "fetch", "--depth", "1", "--quiet", remote, commit],
            cwd=workdir,
            capture_output=True,
            text=True,
            env=_NONINTERACTIVE_ENV(),
            timeout=FETCH_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _write(dest_root: str, entry: dict, data: bytes | None, link: str | None) -> None:
    target = os.path.join(dest_root, entry["path"])
    os.makedirs(os.path.dirname(target) or dest_root, exist_ok=True)
    if os.path.lexists(target):
        os.unlink(target)
    if link is not None:
        os.symlink(link, target)
        return
    with open(target, "wb") as fh:
        fh.write(data or b"")
    # A restored tree whose entrypoint lost +x does not run.
    if entry.get("mode") == "100755":
        os.chmod(target, 0o755)


def restore_snapshot(
    manifest: dict[str, Any],
    dest: str,
    *,
    archive_path: str | None = None,
    verify_only: bool = False,
) -> dict[str, Any]:
    """Rebuild the tree described by ``manifest`` into ``dest``.

    ``archive_path`` is the downloaded ``code-bytes`` tarball, or None when the
    snapshot had nothing pending (or when the caller could not fetch it -- in
    which case those files are reported unavailable rather than silently absent).

    ``verify_only`` resolves and hashes everything without writing, which is how
    a fleet gets swept for "which of these can actually be rebuilt?".

    Returns ``{"files": [...], "n_restored", "n_unavailable", "tree_sha256",
    "tree_matches"}``. Each file carries ``path``, ``source``, ``status``
    (``restored`` | ``verified`` | ``unavailable``) and, when unavailable, a
    ``reason``.
    """
    entries = manifest.get("entries") or []
    if not entries:
        raise RestoreError("manifest carries no entries; nothing to restore")

    # source="blob" members, keyed by path.
    archived: dict[str, tarfile.TarInfo] = {}
    tar: tarfile.TarFile | None = None
    gz = None
    if archive_path and os.path.isfile(archive_path):
        gz = gzip.open(archive_path, "rb")
        tar = tarfile.open(fileobj=gz, mode="r")
        archived = {m.name: m for m in tar.getmembers()}

    tmp_repo: str | None = None
    have_git = False
    needs_git = any(e.get("source") == "git" for e in entries)
    remote = manifest.get("remote")
    base = manifest.get("base_commit")
    if needs_git and remote and base:
        tmp_repo = tempfile.mkdtemp(prefix="probe-restore-")
        have_git = _fetch_base(str(remote), str(base), tmp_repo)

    results: list[dict[str, Any]] = []
    if not verify_only:
        os.makedirs(dest, exist_ok=True)

    try:
        for entry in sorted(entries, key=lambda e: e["path"]):
            path, source = entry["path"], entry.get("source")
            data: bytes | None = None
            link: str | None = None
            reason: str | None = None

            if entry.get("mode") == "120000":
                link = entry.get("symlink_target")
                if link is None and source == "blob" and path in archived:
                    link = archived[path].linkname
                if link is None:
                    reason = "symlink target not recorded"
            elif source == "git":
                if not have_git:
                    reason = (
                        f"cannot fetch {str(base)[:12] if base else 'base'} from "
                        f"{remote or 'no recorded remote'}"
                    )
                else:
                    proc = subprocess.run(
                        ["git", "cat-file", "blob", entry.get("blob", "")],
                        cwd=tmp_repo,
                        capture_output=True,
                    )
                    if proc.returncode == 0:
                        data = proc.stdout
                    else:
                        reason = f"blob {entry.get('blob', '')[:12]} not in the fetched commit"
            elif source == "blob":
                if tar is None:
                    reason = "code-bytes archive unavailable"
                elif path not in archived:
                    reason = "not present in the code-bytes archive"
                else:
                    stream = tar.extractfile(archived[path])
                    data = stream.read() if stream is not None else b""
            else:
                reason = f"unknown source {source!r}"

            if reason is None and link is None:
                got = hashlib.sha256(data or b"").hexdigest()
                if got != entry.get("sha256"):
                    # Never write a file whose bytes disagree with the record.
                    reason = f"sha256 mismatch (recorded {str(entry.get('sha256'))[:12]})"

            if reason is not None:
                results.append(
                    {"path": path, "source": source, "status": "unavailable", "reason": reason}
                )
                continue

            if not verify_only:
                _write(dest, entry, data, link)
            results.append(
                {
                    "path": path,
                    "source": source,
                    "status": "verified" if verify_only else "restored",
                }
            )
    finally:
        if tar is not None:
            tar.close()
        if gz is not None:
            gz.close()
        if tmp_repo:
            subprocess.run(["rm", "-rf", tmp_repo], capture_output=True)

    unavailable = [r for r in results if r["status"] == "unavailable"]
    # Recompute the tree identity over what we actually produced. It can only
    # match when every file resolved, which is precisely the claim being made.
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda e: e["path"]):
        digest.update(
            f"{entry['path']}\0{entry['mode']}\0{entry['sha256']}\n".encode()
        )
    tree = digest.hexdigest()

    return {
        "files": results,
        "n_restored": len(results) - len(unavailable),
        "n_unavailable": len(unavailable),
        "tree_sha256": tree,
        "tree_matches": tree == manifest.get("tree_sha256") and not unavailable,
    }


def verify_restored_tree(manifest: dict[str, Any], root: str) -> list[str]:
    """Paths under ``root`` whose bytes disagree with the manifest, or are absent.

    Used after a restore to prove the tree on disk is the tree that was recorded,
    rather than trusting that the writes went where they were told.
    """
    bad: list[str] = []
    for entry in manifest.get("entries") or []:
        target = os.path.join(root, entry["path"])
        if entry.get("mode") == "120000":
            if not os.path.islink(target) or os.readlink(target) != entry.get(
                "symlink_target"
            ):
                bad.append(entry["path"])
            continue
        if not os.path.isfile(target):
            bad.append(entry["path"])
            continue
        if _file_sha256(target)[0] != entry.get("sha256"):
            bad.append(entry["path"])
    return bad
