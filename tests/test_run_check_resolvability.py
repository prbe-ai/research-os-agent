"""`run check` must not say "complete" on the strength of a row existing.

Seventeen bird-sql-sft runs read as captured for a week because the check counted
artifacts instead of proving retrieval. Their code_snapshot rows were all present
and all pointed at commits that lived nowhere.
"""

from __future__ import annotations

import subprocess

import pytest

from probe.sdk.snapshot import commit_on_remote


class _Client:
    """Just enough of Client to exercise check_run against a canned bundle."""

    def __init__(self, bundle):
        self._bundle = bundle

    def run_bundle(self, run_id):
        return self._bundle


def _check(bundle, **kw):
    from probe.sdk.client import Client

    c = _Client(bundle)
    return Client.check_run(c, "run-1", **kw)


def _bundle(*, env_ref="e" * 64, snapshot_meta=None, artifacts=None):
    arts = artifacts if artifacts is not None else [
        {"kind": "code_snapshot", "is_reference": True,
         "meta": snapshot_meta if snapshot_meta is not None else {
             "base_commit": "d" * 40,
             "remote": "https://github.com/acme/repo.git",
             "n_git_referenced": 10, "n_pending_upload": 0}},
    ]
    return {"run": {"env_ref": env_ref, "metadata": {}}, "artifacts": arts}


# --- the verdict vocabulary ------------------------------------------------

def test_default_is_unverified_not_complete():
    """The word that caused this. Nothing absent is not the same as rebuildable."""
    assert _check(_bundle())["state"] == "unverified"


def test_complete_is_only_earned_by_verifying(monkeypatch):
    monkeypatch.setattr("probe.sdk.snapshot.commit_on_remote", lambda *a, **k: True)
    assert _check(_bundle(), verify=True)["state"] == "complete"


def test_unresolvable_reference_is_incomplete(monkeypatch):
    """The 17-run case: the row is there, the commit is not."""
    monkeypatch.setattr("probe.sdk.snapshot.commit_on_remote", lambda *a, **k: False)
    out = _check(_bundle(), verify=True)
    assert out["state"] == "incomplete"
    assert "unresolvable_code_reference" in out["missing"]


# --- the free check --------------------------------------------------------

def test_pending_bytes_are_incomplete_without_any_network():
    """Costs a dict lookup: the summary already rode in on the artifact meta."""
    out = _check(_bundle(snapshot_meta={
        "base_commit": "d" * 40, "remote": "r",
        "n_git_referenced": 9, "n_pending_upload": 1}))
    assert out["state"] == "incomplete"
    assert "pending_code_bytes" in out["missing"]


def test_zero_pending_does_not_flag():
    assert "pending_code_bytes" not in _check(_bundle())["missing"]


# --- pre-existing checks still hold ---------------------------------------

def test_missing_env_ref_still_incomplete():
    out = _check(_bundle(env_ref=None))
    assert out["state"] == "incomplete" and "execution_record" in out["missing"]


def test_missing_code_snapshot_still_incomplete():
    out = _check(_bundle(artifacts=[]))
    assert "code_snapshot_artifact" in out["missing"]


def test_old_run_without_a_manifest_cannot_be_verified(monkeypatch):
    """Pre-0.26.3 captures recorded no base_commit; refuse to guess either way."""
    out = _check(_bundle(snapshot_meta={"dirty": False}), verify=True)
    assert out["verified_code_reference"] is False
    assert out["state"] == "unverified"


# --- the network probe -----------------------------------------------------

def test_commit_on_remote_is_false_for_an_unreachable_host():
    commit_on_remote.cache_clear()
    assert commit_on_remote("https://10.255.255.1/nope.git", "a" * 40, timeout=5.0) is False


def test_commit_on_remote_rejects_empty_inputs():
    assert commit_on_remote("", "a" * 40) is False
    assert commit_on_remote("https://example.com/r.git", "") is False


def test_commit_on_remote_is_memoized_so_audits_do_not_scale_with_runs(tmp_path):
    """Runs from one machine share a base commit; 200 runs must not be 200 fetches."""
    remote = tmp_path / "r.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    work = tmp_path / "w"
    work.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@t"],
                 ["config", "user.name", "t"], ["remote", "add", "origin", str(remote)]):
        subprocess.run(["git", *args], cwd=work, check=True)
    (work / "a.py").write_text("a = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=work, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:refs/heads/main"], cwd=work, check=True)
    sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work,
                         capture_output=True, text=True).stdout.strip()

    commit_on_remote.cache_clear()
    assert commit_on_remote(str(remote), sha) is True
    before = commit_on_remote.cache_info()
    for _ in range(50):
        commit_on_remote(str(remote), sha)
    after = commit_on_remote.cache_info()
    assert after.misses == before.misses, "repeat audits must be served from cache"
    assert after.hits >= 50
