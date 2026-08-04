"""Inputs git deliberately ignores: named explicitly, stored or referenced.

`.gitignore` is right about build output and wrong about a downloaded dataset, a
base checkpoint, or a config kept out of the repo on purpose. Those are INPUTS,
and the manifest had no way to name them — so they were recorded nowhere, not
even as a hash.

Two outcomes, decided by size, because copying a 40GB checkpoint into every run
is duplication rather than reproducibility:

    small  -> source="blob"       travels in the code-bytes archive
    large  -> source="reference"  path + host + sha256 recorded, bytes left alone
"""

from __future__ import annotations

import subprocess

import pytest

from probe.sdk.restore import restore_snapshot
from probe.sdk.snapshot import (
    SnapshotError,
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
    """A repo whose .gitignore hides real inputs: a dataset and a checkpoint."""
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "t@e.com")
    _git(work, "config", "user.name", "t")
    (work / ".gitignore").write_text("data/\ncheckpoints/\nlocal.yaml\n")
    (work / "train.py").write_text("print('train')\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")

    (work / "data").mkdir()
    (work / "data" / "train.jsonl").write_text('{"q": "select 1"}\n')
    (work / "local.yaml").write_text("lr: 3e-4\n")
    (work / "checkpoints").mkdir()
    (work / "checkpoints" / "base.pt").write_bytes(b"W" * 5000)
    return work


def _by_path(m):
    return {e["path"]: e for e in m["entries"]}


# --- the gap this closes -----------------------------------------------------

def test_gitignored_inputs_are_absent_unless_named(repo):
    """The status quo: git hides them, so the record has nothing at all."""
    m = capture_manifest(str(repo))
    assert "data/train.jsonl" not in _by_path(m)
    assert "local.yaml" not in _by_path(m)


def test_named_inputs_are_captured(repo):
    m = capture_manifest(str(repo), include=["data/**", "local.yaml"])
    got = _by_path(m)
    assert "data/train.jsonl" in got and "local.yaml" in got
    assert got["local.yaml"]["source"] == "blob"
    assert got["local.yaml"]["included"] is True
    assert got["data/train.jsonl"]["sha256"]


def test_named_inputs_actually_travel_in_the_archive(repo, tmp_path):
    m = capture_manifest(str(repo), include=["local.yaml"])
    assert "local.yaml" in {e["path"] for e in pending_entries(m)}
    dest = tmp_path / "code.tar.gz"
    build_pending_archive(str(repo), m, str(dest))
    import gzip
    import tarfile

    with gzip.open(dest, "rb") as gz, tarfile.open(fileobj=gz, mode="r") as tar:
        assert tar.extractfile("local.yaml").read() == b"lr: 3e-4\n"


def test_a_directory_include_captures_its_files(repo):
    m = capture_manifest(str(repo), include=["data"])
    assert "data/train.jsonl" in _by_path(m)


def test_an_include_that_matches_nothing_is_an_error(repo):
    """Silently capturing nothing is how a 'reproducible' run loses its dataset."""
    with pytest.raises(SnapshotError, match="matched no files"):
        capture_manifest(str(repo), include=["data/does-not-exist/*"])


def test_an_include_cannot_escape_the_root(repo, tmp_path):
    """A path outside the snapshot root has no meaning in the manifest, and a
    relative entry pointing at one would silently restore to the wrong place."""
    (tmp_path / "outside.txt").write_text("not mine\n")
    with pytest.raises(SnapshotError, match="escapes"):
        capture_manifest(str(repo), include=["../outside.txt"])


def test_a_tracked_file_is_not_duplicated_by_an_include(repo):
    """Naming a file git already offers must not add a second entry for it --
    two rows for one path would double-count and corrupt tree_sha256."""
    m = capture_manifest(str(repo), include=["train.py"])
    assert [e["path"] for e in m["entries"]].count("train.py") == 1
    # this fixture has no remote, so git can supply nothing; the point is that
    # the include did not append a duplicate, whatever the classification.
    assert _by_path(m)["train.py"].get("included") is None


# --- large files are referenced, not copied ----------------------------------

def test_a_large_input_is_referenced_rather_than_uploaded(repo):
    m = capture_manifest(
        str(repo), include=["checkpoints/base.pt"], reference_over_bytes=1000
    )
    entry = _by_path(m)["checkpoints/base.pt"]
    assert entry["source"] == "reference"
    assert entry["uri"].startswith("file://") and entry["uri"].endswith("base.pt")
    assert entry["host"] and entry["sha256"]
    assert m["n_referenced_offsite"] == 1


def test_a_referenced_file_never_enters_the_archive(repo, tmp_path):
    """Copying tens of GB per run is duplication, not reproducibility."""
    m = capture_manifest(
        str(repo), include=["checkpoints/base.pt"], reference_over_bytes=1000
    )
    assert "checkpoints/base.pt" not in {e["path"] for e in pending_entries(m)}
    dest = tmp_path / "code.tar.gz"
    summary = build_pending_archive(str(repo), m, str(dest))
    import gzip
    import tarfile

    with gzip.open(dest, "rb") as gz, tarfile.open(fileobj=gz, mode="r") as tar:
        assert "checkpoints/base.pt" not in tar.getnames()
    assert summary["uncompressed_bytes"] < 5000


def test_restore_reports_a_reference_as_off_platform_not_missing(repo, tmp_path):
    """Not a failure -- the bytes exist somewhere specific -- but the tree on disk
    is still incomplete, so tree_matches must not claim success."""
    m = capture_manifest(
        str(repo), include=["checkpoints/base.pt"], reference_over_bytes=1000
    )
    archive = tmp_path / "code.tar.gz"
    build_pending_archive(str(repo), m, str(archive))

    result = restore_snapshot(m, str(tmp_path / "out"), archive_path=str(archive))

    assert result["n_unavailable"] == 0, "a reference is not a failure"
    assert result["n_referenced"] == 1
    assert result["referenced"][0]["path"] == "checkpoints/base.pt"
    assert result["referenced"][0]["uri"].startswith("file://")
    assert result["tree_matches"] is False, "the checkpoint is not on disk"
    assert not (tmp_path / "out" / "checkpoints" / "base.pt").exists()


# --- end to end --------------------------------------------------------------

def test_snapshot_records_both_outcomes(client, app, repo):
    client.fail_open = False
    run = open_run(client, experiment="e", name="r")
    snap = run.snapshot(
        cwd=str(repo),
        include=["data/**", "local.yaml", "checkpoints/base.pt"],
        reference_over_bytes=1000,
        include_env=False,
        include_gpu=False,
    )

    assert snap["code_bytes"]["uploaded"] is True
    meta = next(
        a["meta"]
        for rows in app.artifacts.values()
        for a in rows
        if a.get("kind") == "code_snapshot"
    )
    assert meta["n_referenced_offsite"] == 1
    assert meta["n_pending_upload"] == 0
    paths = {
        p
        for rows in app.artifacts.values()
        for a in rows
        if a.get("kind") == "code_bytes"
        for p in a["meta"]["paths"]
    }
    assert "data/train.jsonl" in paths and "local.yaml" in paths
    assert "checkpoints/base.pt" not in paths
