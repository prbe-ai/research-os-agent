"""Snapshot plumbing against a real throwaway git repo. No network."""

from __future__ import annotations

import subprocess

import pytest

from ros import snapshot


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    _git(d, "init", "-q")
    _git(d, "config", "user.email", "t@example.com")
    _git(d, "config", "user.name", "t")
    (d / "a.txt").write_text("hello\n")
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", "init")
    return d


def test_snapshot_captures_uncommitted_without_touching_worktree(repo):
    # dirty the tree: modify tracked + add untracked
    (repo / "a.txt").write_text("hello world\n")
    (repo / "untracked.txt").write_text("new\n")

    head_before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    status_before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout

    snap = snapshot.capture_git_snapshot("run-xyz", cwd=str(repo))

    # HEAD and working tree are untouched
    head_after = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    status_after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert head_before == head_after
    assert status_before == status_after
    assert snap["dirty"] is True
    assert snap["ref"] == "refs/ros/snapshots/run-xyz"

    # the shadow commit exists and contains the untracked file
    tree = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", snap["commit"]],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout
    assert "untracked.txt" in tree
    assert "a.txt" in tree
    # and its blob has the modified content
    show = subprocess.run(
        ["git", "show", f"{snap['commit']}:a.txt"], cwd=repo, capture_output=True, text=True
    ).stdout
    assert show == "hello world\n"


def test_snapshot_errors_outside_git(tmp_path):
    with pytest.raises(snapshot.SnapshotError):
        snapshot.capture_git_snapshot("r", cwd=str(tmp_path))
