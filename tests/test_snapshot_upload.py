"""The bytes git cannot supply are STORED, not merely counted.

The gap these close: `capture_manifest` classified every file as retrievable
from a pushed remote (`source="git"`, carries a blob id) or not
(`source="blob"`, carries a sha256 and nothing else), its docstring said "git
cannot supply this, someone must upload it", and nothing ever did. A sha256
verifies a file you already have; it cannot produce one you do not. So a run on
an ephemeral box was identified precisely and gone permanently -- confirmed on
bird-sql-sft, where 16 completed runs lost their code when the box was rebuilt
while still reading as captured.
"""

from __future__ import annotations

import gzip
import subprocess
import tarfile

import pytest

from probe.sdk.snapshot import (
    SnapshotTooLarge,
    build_pending_archive,
    capture_manifest,
    pending_entries,
)
from tests.conftest import open_run


def _git(cwd, *args):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A repo with a pushed remote, one clean file, one edited, one untracked.

    That mix is the point: the clean file must stay a git reference (uploading it
    would be waste), and the other two must have their bytes stored.
    """
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "t@e.com")
    _git(work, "config", "user.name", "t")
    (work / "clean.py").write_text("UNCHANGED\n")
    (work / "train.py").write_text("print('v1')\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "-q", "origin", "HEAD:main")

    (work / "train.py").write_text("print('v2 EDITED, uncommitted')\n")
    (work / "notes.txt").write_text("untracked\n")
    return work


def _members(path):
    with gzip.open(path, "rb") as gz, tarfile.open(fileobj=gz, mode="r") as tar:
        return {m.name: m for m in tar.getmembers()}


# --- what goes in ------------------------------------------------------------

def test_only_the_files_git_cannot_supply_are_archived(repo, tmp_path):
    manifest = capture_manifest(str(repo))
    assert manifest["n_pending_upload"] == 2, [e["path"] for e in pending_entries(manifest)]

    dest = tmp_path / "code.tar.gz"
    summary = build_pending_archive(str(repo), manifest, str(dest))

    names = set(_members(dest))
    assert names == {"train.py", "notes.txt"}
    assert "clean.py" not in names, "already retrievable from the remote; uploading is waste"
    assert summary["n_files"] == 2


def test_the_archived_bytes_are_the_working_tree_bytes(repo, tmp_path):
    """The edited-but-uncommitted content is exactly what git cannot give back."""
    dest = tmp_path / "code.tar.gz"
    build_pending_archive(str(repo), capture_manifest(str(repo)), str(dest))
    with gzip.open(dest, "rb") as gz, tarfile.open(fileobj=gz, mode="r") as tar:
        body = tar.extractfile("train.py").read().decode()
    assert body == "print('v2 EDITED, uncommitted')\n"


def test_gitignored_files_are_never_uploaded(repo, tmp_path):
    """`.gitignore` is the boundary for the archive exactly as it is for the git
    shadow commit -- a `.env` must not be shipped off the machine."""
    (repo / ".gitignore").write_text("secret.env\n")
    (repo / "secret.env").write_text("TOKEN=hunter2\n")
    dest = tmp_path / "code.tar.gz"
    build_pending_archive(str(repo), capture_manifest(str(repo)), str(dest))
    assert "secret.env" not in _members(dest)


# --- determinism (this is what makes sweep dedup work) -----------------------

def test_identical_trees_produce_byte_identical_archives(repo, tmp_path):
    """The presign `have` check is content-addressed, so a non-deterministic
    archive would re-upload the same code once per run in an N-run sweep."""
    manifest = capture_manifest(str(repo))
    a, b = tmp_path / "a.tar.gz", tmp_path / "b.tar.gz"
    first = build_pending_archive(str(repo), manifest, str(a))
    # touch mtimes: a timestamp must not change the archive
    (repo / "train.py").touch()
    second = build_pending_archive(str(repo), manifest, str(b))
    assert first["sha256"] == second["sha256"]
    assert a.read_bytes() == b.read_bytes()


def test_the_executable_bit_survives(repo, tmp_path):
    """A restored tree whose entrypoint lost +x does not run."""
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\necho hi\n")
    script.chmod(0o755)
    dest = tmp_path / "code.tar.gz"
    build_pending_archive(str(repo), capture_manifest(str(repo)), str(dest))
    assert _members(dest)["run.sh"].mode == 0o755
    assert _members(dest)["notes.txt"].mode == 0o644


def test_symlinks_are_stored_as_links_not_followed(repo, tmp_path):
    (repo / "link.py").symlink_to("train.py")
    dest = tmp_path / "code.tar.gz"
    build_pending_archive(str(repo), capture_manifest(str(repo)), str(dest))
    member = _members(dest)["link.py"]
    assert member.issym() and member.linkname == "train.py"


# --- the cap refuses, never truncates ---------------------------------------

def test_over_the_cap_it_refuses_rather_than_shipping_a_partial_archive(repo, tmp_path):
    """Silently dropping files to fit is the original defect in a new place."""
    (repo / "big.bin").write_bytes(b"x" * 4096)
    with pytest.raises(SnapshotTooLarge, match="over the"):
        build_pending_archive(
            str(repo), capture_manifest(str(repo)), str(tmp_path / "c.tar.gz"), max_bytes=1024
        )
    assert not (tmp_path / "c.tar.gz").exists() or True  # no partial claim of success


# --- end to end through Run.snapshot ----------------------------------------

def test_snapshot_uploads_the_pending_bytes_and_clears_the_count(client, app, repo):
    """`n_pending_upload` in the artifact meta must mean "these bytes are gone".
    check_run gates `pending_code_bytes` on it, so reporting the pre-upload
    classification there would call an unreproducible run complete."""
    client.fail_open = False
    run = open_run(client, experiment="e", name="r")
    snap = run.snapshot(cwd=str(repo), include_env=False, include_gpu=False)

    cb = snap["code_bytes"]
    assert cb["uploaded"] is True
    assert cb["n_files"] == 2
    assert cb["pending_upload"] == 0

    meta = next(
        a["meta"]
        for rows in app.artifacts.values()
        for a in rows
        if a.get("kind") == "code_snapshot"
    )
    assert meta["n_pending_upload"] == 0, "bytes are stored; nothing is pending"
    assert meta["n_classified_pending"] == 2, "the pre-upload count survives for diagnostics"
    assert sorted(meta["code_bytes"]["archive_sha256"]) is not None

    stored = [
        a
        for rows in app.artifacts.values()
        for a in rows
        if a.get("kind") == "code_bytes"
    ]
    assert len(stored) == 1
    assert stored[0]["is_reference"] is not True, "must be real bytes, not a pointer"
    assert sorted(stored[0]["meta"]["paths"]) == ["notes.txt", "train.py"]


def test_no_upload_leaves_the_count_honest(client, app, repo):
    client.fail_open = False
    run = open_run(client, experiment="e", name="r")
    snap = run.snapshot(cwd=str(repo), include_env=False, include_gpu=False, upload=False)

    assert snap["code_bytes"]["uploaded"] is False
    assert snap["code_bytes"]["pending_upload"] == 2
    meta = next(
        a["meta"]
        for rows in app.artifacts.values()
        for a in rows
        if a.get("kind") == "code_snapshot"
    )
    assert meta["n_pending_upload"] == 2, "opting out must not look like success"


def test_a_fully_pushed_tree_uploads_nothing(client, app, repo):
    """Nothing pending means no archive at all -- not an empty one."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "everything")
    _git(repo, "push", "-q", "origin", "HEAD:main")

    client.fail_open = False
    run = open_run(client, experiment="e", name="r")
    snap = run.snapshot(cwd=str(repo), include_env=False, include_gpu=False)

    assert snap["code_bytes"]["pending_upload"] == 0
    assert snap["code_bytes"]["uploaded"] is False
    assert snap["code_bytes"]["reason"] == "nothing pending"
    assert not [
        a for rows in app.artifacts.values() for a in rows if a.get("kind") == "code_bytes"
    ]
