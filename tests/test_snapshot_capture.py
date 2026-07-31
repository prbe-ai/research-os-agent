"""Per-file capture: reference what a remote already has, upload what it does not.

The bug these pin down: a snapshot used to record a commit SHA with nothing
verifying that commit existed anywhere but the machine that made it. When the
machine went away the code went with it, and every completeness check still
reported the run as captured.
"""

from __future__ import annotations

import subprocess

import pytest

from probe.sdk.snapshot import SnapshotError, capture_env, capture_manifest, pushed_base


def _git(cwd, *args):
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """A repo with a bare 'remote', one pushed commit, two tracked files."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))

    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "remote", "add", "origin", str(remote))
    (work / "a.py").write_text("a = 1\n")
    (work / "b.py").write_text("b = 2\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    _git(work, "push", "-q", "origin", "HEAD:refs/heads/main")
    return work


def _by_path(manifest):
    return {e["path"]: e for e in manifest["entries"]}


# --- the core promise ------------------------------------------------------

def test_clean_pushed_tree_uploads_nothing(repo):
    m = capture_manifest(str(repo))
    assert m["n_pending_upload"] == 0
    assert m["n_git_referenced"] == 2
    assert all(e["source"] == "git" for e in m["entries"])


def test_edited_file_uploads_alone(repo):
    """The whole point of per-file: one dirty file must not drag its siblings."""
    (repo / "a.py").write_text("a = 999\n")
    m = capture_manifest(str(repo))
    entries = _by_path(m)
    assert entries["a.py"]["source"] == "blob"
    assert entries["b.py"]["source"] == "git"
    assert m["n_pending_upload"] == 1
    assert m["n_git_referenced"] == 1


def test_untracked_file_uploads(repo):
    (repo / "c.py").write_text("c = 3\n")
    entries = _by_path(capture_manifest(str(repo)))
    assert entries["c.py"]["source"] == "blob"
    assert entries["a.py"]["source"] == "git"


def test_unpushed_commit_uploads_everything(repo):
    """The 19-run bug: committed locally is not the same as retrievable."""
    (repo / "a.py").write_text("a = 4\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "local only")
    m = capture_manifest(str(repo))
    assert _by_path(m)["a.py"]["source"] == "blob"


def test_no_remote_uploads_everything(tmp_path):
    work = tmp_path / "solo"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "a.py").write_text("a = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    m = capture_manifest(str(work))
    assert m["n_git_referenced"] == 0
    assert m["n_pending_upload"] == 1
    assert pushed_base(str(work)) == (None, None)


def test_not_a_git_repo_is_refused(tmp_path):
    """Walking an arbitrary directory has no .gitignore to honour, so the first
    thing it would sweep up is the .env the git path excludes."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "a.py").write_text("a = 1\n")
    (plain / ".env") .write_text("PROBE_TOKEN=secret\n")
    with pytest.raises(SnapshotError):
        capture_manifest(str(plain))


def test_deleted_tracked_file_absent_from_manifest(repo):
    (repo / "b.py").unlink()
    assert "b.py" not in _by_path(capture_manifest(str(repo)))


# --- identity --------------------------------------------------------------

def test_tree_hash_is_stable(repo):
    assert capture_manifest(str(repo))["tree_sha256"] == capture_manifest(str(repo))["tree_sha256"]


def test_tree_hash_changes_when_content_changes(repo):
    before = capture_manifest(str(repo))["tree_sha256"]
    (repo / "a.py").write_text("a = 2\n")
    assert capture_manifest(str(repo))["tree_sha256"] != before


def test_source_does_not_affect_identity(repo, tmp_path):
    """Same content must hash the same whether referenced or uploaded.

    Otherwise a run with one dirty file compares as different code from an
    identical clean run, and run-to-run comparison silently breaks.
    """
    referenced = capture_manifest(str(repo))["tree_sha256"]

    plain = tmp_path / "copy"
    plain.mkdir()
    _git(plain, "init", "-q")
    _git(plain, "config", "user.email", "t@t")
    _git(plain, "config", "user.name", "t")
    (plain / "a.py").write_text("a = 1\n")
    (plain / "b.py").write_text("b = 2\n")
    _git(plain, "add", "-A")
    _git(plain, "commit", "-qm", "init")
    uploaded = capture_manifest(str(plain))

    assert uploaded["n_git_referenced"] == 0
    assert uploaded["tree_sha256"] == referenced


def test_referenced_entry_carries_retrieval_metadata(repo):
    """Per-entry: the blob id. Commit and remote live once at the top level —
    they are identical for every referenced file, so repeating them per entry
    would bloat the execution-record payload on a large repo."""
    m = capture_manifest(str(repo))
    e = _by_path(m)["a.py"]
    assert e["blob"] and e["sha256"] and e["mode"] == "100644"
    assert m["base_commit"] and m["remote"]


def test_exec_bit_recorded(repo):
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    assert _by_path(capture_manifest(str(repo)))["run.sh"]["mode"] == "100755"


def test_gitignored_files_are_not_captured(repo):
    (repo / ".gitignore").write_text("secret.env\n")
    (repo / "secret.env").write_text("TOKEN=hunter2\n")
    assert "secret.env" not in _by_path(capture_manifest(str(repo)))


# --- environment -----------------------------------------------------------

def test_capture_env_records_the_actual_packages():
    info = capture_env()
    assert info["packages"], "must store the list, not just a digest"
    assert info["package_count"] == len(info["packages"])
    assert any("==" in p for p in info["packages"])
    assert info["python"]


def test_capture_env_raises_rather_than_recording_nothing(monkeypatch):
    """Silently returning {'python': ...} is how real runs lost their deps."""
    monkeypatch.setattr(
        "probe.sdk.snapshot._installed_distributions",
        lambda: (_ for _ in ()).throw(RuntimeError("no metadata")),
    )
    with pytest.raises(SnapshotError):
        capture_env()


def test_capture_env_non_strict_degrades_quietly(monkeypatch):
    monkeypatch.setattr(
        "probe.sdk.snapshot._installed_distributions",
        lambda: (_ for _ in ()).throw(RuntimeError("no metadata")),
    )
    assert capture_env(strict=False)["python"]


# --- hardening found in review --------------------------------------------

def test_unpushed_commit_uploads_only_what_changed(repo):
    """Falls back to the pushed merge-base; siblings stay referenced."""
    pushed = _git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("a = 4\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "local only")
    m = capture_manifest(str(repo))
    assert m["base_commit"] == pushed
    assert _by_path(m)["a.py"]["source"] == "blob"
    assert _by_path(m)["b.py"]["source"] == "git"
    assert (m["n_pending_upload"], m["n_git_referenced"]) == (1, 1)


def test_remote_credentials_are_never_recorded(repo):
    """A CI remote carries a live token; it must not reach run metadata."""
    _git(repo, "remote", "set-url", "origin",
         "https://x-access-token:ghs_SUPERSECRET@github.com/acme/repo.git")
    _, remote = pushed_base(str(repo))
    if remote:
        assert "ghs_SUPERSECRET" not in remote
        assert "<redacted>" in remote


def test_scrubber_handles_both_url_shapes():
    from probe.sdk.snapshot import _scrub_remote
    assert "TOK" not in _scrub_remote("https://oauth2:TOK@gitlab.com/a/b.git")
    assert "git@" not in _scrub_remote("git@github.com:acme/repo.git")
    assert _scrub_remote("https://github.com/acme/repo.git") == "https://github.com/acme/repo.git"


def test_symlink_participates_in_identity(repo):
    link = repo / "entry.py"
    link.symlink_to("a.py")
    _git(repo, "add", "-A")
    m = capture_manifest(str(repo))
    assert "entry.py" in _by_path(m), "a symlink must not vanish from the capture"
    before = m["tree_sha256"]
    link.unlink()
    link.symlink_to("b.py")
    assert capture_manifest(str(repo))["tree_sha256"] != before


def test_unreachable_remote_falls_back_without_hanging(tmp_path):
    import time
    work = tmp_path / "stalled"
    work.mkdir()
    _git(work, "init", "-q")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    _git(work, "remote", "add", "origin", "https://10.255.255.1/nope.git")
    (work / "a.py").write_text("a = 1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "init")
    started = time.monotonic()
    m = capture_manifest(str(work))
    assert m["n_pending_upload"] == 1 and m["n_git_referenced"] == 0
    assert time.monotonic() - started < 40


def test_zero_distributions_is_refused_under_strict(monkeypatch):
    monkeypatch.setattr("probe.sdk.snapshot._installed_distributions", list)
    with pytest.raises(SnapshotError):
        capture_env()


def test_stale_remote_branch_does_not_drag_the_base_backwards(repo):
    """A stale branch sorting earlier in ls-remote must not win the base.

    Taking the first merge-base found made an unrelated old branch decide the
    base, which marked hundreds of unchanged files for upload. Measured on the
    real repo before the fix: 57 pending uploads for a 5-file change.
    """
    old = _git(repo, "rev-parse", "HEAD")
    # 'aaa-stale' sorts before 'main' in ls-remote refname order.
    _git(repo, "push", "-q", "origin", f"{old}:refs/heads/aaa-stale")

    (repo / "b.py").write_text("b = 20\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "second")
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/main")
    newer = _git(repo, "rev-parse", "HEAD")

    # An unpushed commit on top, so pushed_base must fall back to a merge-base.
    (repo / "a.py").write_text("a = 99\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "local only")

    base, _ = pushed_base(str(repo))
    assert base == newer, "must pick the newest pushed ancestor, not the stale branch"

    m = capture_manifest(str(repo))
    assert _by_path(m)["b.py"]["source"] == "git", "b.py is pushed; must not re-upload"
    assert m["n_pending_upload"] == 1
