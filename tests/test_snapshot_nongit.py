"""A directory with no git repo is captured, not refused.

There is no reference half without git -- no pushed base, no blob ids -- so
every file is `source="blob"` and every file is uploaded. That case used to
raise, which was defensible only while no uploader existed: the directory with
NOTHING retrievable anywhere was the one turned away outright. `research-workflows/`
is exactly this shape.

What replaces the refusal is a filter, and the filter is the whole design: git
gave the classifier `.gitignore` for free, a bare directory has nothing, so the
defaults must be conservative in the one direction that matters.
"""

from __future__ import annotations

import os

import pytest

from probe.sdk.snapshot import (
    build_pending_archive,
    capture_directory_manifest,
    capture_manifest,
)
from probe.sdk.restore import restore_snapshot
from tests.conftest import open_run


@pytest.fixture
def project(tmp_path):
    """A realistic non-git project: source, config, a venv, caches, secrets."""
    root = tmp_path / "workflows"
    (root / "src").mkdir(parents=True)
    (root / "configs").mkdir()
    (root / "src" / "train.py").write_text("LR = 3e-4\n")
    (root / "configs" / "sweep.yaml").write_text("seed: 1337\n")
    (root / "run.sh").write_text("#!/bin/sh\npython src/train.py\n")
    (root / "run.sh").chmod(0o755)

    # what a lockfile rebuilds -- must never be uploaded
    (root / ".venv" / "lib").mkdir(parents=True)
    (root / ".venv" / "lib" / "huge.py").write_text("x" * 10_000)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("//\n")
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "train.cpython-313.pyc").write_bytes(b"\x00")

    # credentials -- must never leave the machine
    (root / ".env").write_text("PROBE_TOKEN=hunter2\n")
    (root / "id_rsa").write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n")
    (root / "certs").mkdir()
    (root / "certs" / "server.pem").write_text("-----BEGIN CERTIFICATE-----\n")
    return root


def _paths(m):
    return {e["path"] for e in m["entries"]}


# --- what gets captured ------------------------------------------------------

def test_the_authored_files_are_captured(project):
    m = capture_manifest(str(project))
    assert _paths(m) == {"src/train.py", "configs/sweep.yaml", "run.sh"}


def test_every_file_is_marked_for_upload(project):
    """No git means nothing is retrievable from anywhere."""
    m = capture_manifest(str(project))
    assert all(e["source"] == "blob" for e in m["entries"])
    assert m["n_pending_upload"] == 3
    assert m["n_git_referenced"] == 0
    assert m["base_commit"] is None and m["remote"] is None and m["vcs"] is None


def test_lockfile_rebuilt_directories_are_not_uploaded(project):
    """Without this the first snapshot of an ordinary project ships .venv."""
    m = capture_manifest(str(project))
    assert not any(p.startswith((".venv", "node_modules")) for p in _paths(m))
    assert not any(p.endswith(".pyc") for p in _paths(m))
    skipped = {s["path"] for s in m["skipped"]}
    assert ".venv" in skipped and "node_modules" in skipped


def test_credentials_never_leave_the_machine(project):
    """Auto-uploading a working directory must not be how a .env escapes."""
    m = capture_manifest(str(project))
    assert not ({".env", "id_rsa", "certs/server.pem"} & _paths(m))
    secrets = {s["path"] for s in m["skipped"] if s["reason"] == "secret"}
    assert secrets == {".env", "id_rsa", "certs/server.pem"}


def test_exclusions_are_reported_not_silent(project):
    """Once a filter exists, absence stops being informative on its own: a reader
    has to tell 'not an input' from 'excluded by policy'."""
    m = capture_manifest(str(project))
    assert m["skipped"], "an exclusion nobody can see is indistinguishable from a bug"
    assert all({"path", "reason"} == set(s) for s in m["skipped"])


def test_modes_and_symlinks_survive(project):
    (project / "link.py").symlink_to("src/train.py")
    m = capture_manifest(str(project))
    by_path = {e["path"]: e for e in m["entries"]}
    assert by_path["run.sh"]["mode"] == "100755"
    assert by_path["configs/sweep.yaml"]["mode"] == "100644"
    assert by_path["link.py"]["mode"] == "120000"
    assert by_path["link.py"]["symlink_target"] == "src/train.py"


def test_a_missing_directory_raises(tmp_path):
    from probe.sdk.snapshot import SnapshotError

    with pytest.raises(SnapshotError, match="not a directory"):
        capture_directory_manifest(str(tmp_path / "nope"))


# --- end to end: capture, upload, destroy, rebuild ---------------------------

def test_a_non_git_project_round_trips(project, tmp_path):
    """The whole point: a directory with no repo is fully reproducible."""
    m = capture_manifest(str(project))
    archive = tmp_path / "code.tar.gz"
    build_pending_archive(str(project), m, str(archive))

    originals = {
        e["path"]: (project / e["path"]).read_bytes() for e in m["entries"]
    }
    import shutil

    shutil.rmtree(project)  # *** the box is destroyed ***

    dest = tmp_path / "rebuilt"
    result = restore_snapshot(m, str(dest), archive_path=str(archive))

    assert result["n_unavailable"] == 0
    assert result["tree_matches"] is True
    for path, want in originals.items():
        assert (dest / path).read_bytes() == want, path
    assert os.access(dest / "run.sh", os.X_OK)


def test_snapshot_of_a_non_git_dir_records_no_commit(client, app, project):
    client.fail_open = False
    run = open_run(client, experiment="e", name="r")
    snap = run.snapshot(cwd=str(project), include_env=False, include_gpu=False)

    assert snap["git"] is None
    assert snap["code_bytes"]["uploaded"] is True
    assert snap["code_bytes"]["n_files"] == 3

    meta = next(
        a["meta"]
        for rows in app.artifacts.values()
        for a in rows
        if a.get("kind") == "code_snapshot"
    )
    assert meta["vcs"] is None
    assert meta["n_pending_upload"] == 0, "the bytes are stored"
    assert meta["skipped"], "the exclusions travel with the record"
