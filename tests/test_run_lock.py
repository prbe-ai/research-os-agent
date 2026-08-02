"""The run lock: does it know when an experiment is live, and does it fail closed?

ONE test here uses a real subprocess and a real SIGKILL. That is deliberate and it
is the only one that needs to: the entire reason this design chose flock over pid
files is that the KERNEL releases the lock when a process dies, however it dies. A
test that monkeypatches the liveness probe verifies our if-statement reads a
boolean, which was never in doubt, and proves nothing about the property the
design rests on. Everything else is a fast unit test over a temp directory.

The subprocess child is stdlib-only, does no network and no config loading, so it
starts and dies in milliseconds; it is killed in a finally with a bounded wait so
a wedged child cannot stall CI.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

from probe.cli import run_lock


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Point the lock at a temp state dir, and clear the escape hatch.

    Sets XDG_STATE_HOME because that is what version_policy.state_dir reads;
    without it these tests would scribble on the developer's real state.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv(run_lock.OVERRIDE_ENV, raising=False)
    return tmp_path


# ---------------------------------------------------------------------------
# The kernel guarantee. The reason flock was chosen at all.
# ---------------------------------------------------------------------------


def test_a_sigkilled_run_frees_its_lock_with_no_cleanup(isolate):
    """A crashed run must never wedge auto-update on that machine forever.

    Real child, real SIGKILL, real lock. Mocking any part of this would test the
    mock: nothing in our code releases the lock here, the kernel does.
    """
    child_src = (
        "import time; from probe.cli import run_lock; "
        "lock = run_lock.acquire('victim'); "
        "print('held' if lock else 'FAILED', flush=True); "
        "time.sleep(300)"
    )
    env = dict(os.environ, XDG_STATE_HOME=str(isolate))
    src = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")

    child = subprocess.Popen(
        [sys.executable, "-c", child_src],
        env=env,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held", "child never took the lock"
        assert run_lock.any_live() is True, "a live holder must read as live"

        child.kill()  # SIGKILL: no atexit, no finally, no cleanup of any kind
        child.wait(timeout=10)

        assert run_lock.any_live() is False, (
            "the kernel did not release the flock on SIGKILL — this is the "
            "guarantee the whole design rests on"
        )
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if child.stdout:
            child.stdout.close()


# ---------------------------------------------------------------------------
# flock tier.
# ---------------------------------------------------------------------------


def test_no_runs_at_all_reads_as_clear(isolate):
    assert run_lock.any_live() is False
    assert run_lock.live_runs() == []


def test_an_acquired_lock_reads_as_live_and_releases_cleanly(isolate):
    lock = run_lock.acquire("run-abc")
    assert lock is not None
    try:
        assert run_lock.any_live() is True
        assert run_lock.live_runs() == ["run-abc"]
    finally:
        lock.release()
    assert run_lock.any_live() is False


def test_release_is_idempotent(isolate):
    lock = run_lock.acquire("run-abc")
    assert lock is not None
    lock.release()
    lock.release()  # must not raise
    assert run_lock.any_live() is False


def test_the_context_manager_releases_on_exception(isolate):
    with pytest.raises(RuntimeError):
        with run_lock.RunLock("run-boom"):
            assert run_lock.any_live() is True
            raise RuntimeError("boom")
    assert run_lock.any_live() is False


def test_several_concurrent_runs_each_hold_their_own_entry(isolate):
    """A sweep is normal. One shared lock file would mean run B's exit released
    run A's protection, which is why entries are per-run."""
    locks = [run_lock.acquire(f"sweep-{index}") for index in range(4)]
    assert all(lock is not None for lock in locks)
    try:
        assert sorted(run_lock.live_runs()) == [f"sweep-{i}" for i in range(4)]
        locks[0].release()
        locks[2].release()
        assert sorted(run_lock.live_runs()) == ["sweep-1", "sweep-3"]
        assert run_lock.any_live() is True
    finally:
        for lock in locks:
            if lock is not None:
                lock.release()
    assert run_lock.any_live() is False


def test_an_abandoned_flock_file_is_swept(isolate):
    """A leftover file nobody holds must not accumulate, and must not read live."""
    stale = run_lock.runs_dir()
    stale.mkdir(parents=True, exist_ok=True)
    orphan = stale / f"ghost{run_lock.FLOCK_SUFFIX}"
    orphan.write_text('{"pid": 999999}')
    assert run_lock.any_live() is False
    assert not orphan.exists(), "an unheld flock file should be cleaned up"


# ---------------------------------------------------------------------------
# lease tier: the bare `run start` / `run end` bracket.
# ---------------------------------------------------------------------------


def test_a_live_lease_reads_as_live(isolate):
    run_lock.touch_lease("bracket", seconds=600)
    assert run_lock.any_live() is True
    assert run_lock.live_runs() == ["bracket"]


def test_an_expired_lease_reads_as_clear_and_is_swept(isolate):
    run_lock.touch_lease("bracket", seconds=-1)
    assert run_lock.any_live() is False
    lease = run_lock.runs_dir() / f"bracket{run_lock.LEASE_SUFFIX}"
    assert not lease.exists()


def test_clear_lease_frees_the_box_immediately(isolate):
    run_lock.touch_lease("bracket", seconds=600)
    run_lock.clear_lease("bracket")
    assert run_lock.any_live() is False


def test_clear_lease_on_an_absent_run_is_a_noop(isolate):
    run_lock.clear_lease("never-existed")  # must not raise


def test_renewal_only_writes_once_the_lease_is_half_spent(isolate):
    """`probe log` in a training loop hits this on every call. A write per
    invocation would violate the O(1) contract the root callback already has."""
    run_lock.touch_lease("bracket", seconds=1000)
    lease = run_lock.runs_dir() / f"bracket{run_lock.LEASE_SUFFIX}"
    before = json.loads(lease.read_text())["expires_at"]

    run_lock.renew_lease_if_stale("bracket", seconds=1000)
    assert json.loads(lease.read_text())["expires_at"] == before, "must not rewrite"

    # Now age it past halfway. Compare against the AGED value, not `before`:
    # renewal recomputes from "now", so in the same wall-clock second the result
    # legitimately equals `before` and a strict `>` against it is a flaky clock
    # assertion rather than a behavioural one.
    aged = int(time.time()) + 10
    lease.write_text(json.dumps({"run": "bracket", "expires_at": aged}))
    run_lock.renew_lease_if_stale("bracket", seconds=1000)
    assert json.loads(lease.read_text())["expires_at"] > aged, "must extend"


def test_renewal_does_not_create_a_lease_for_a_flock_tier_run(isolate):
    """An SDK run holds an flock and clears it on finish. If renewal invented a
    lease for it, that lease would outlive the run and block updates until it
    expired."""
    lock = run_lock.acquire("sdk-run")
    try:
        run_lock.renew_lease_if_stale("sdk-run")
        lease = run_lock.runs_dir() / f"sdk-run{run_lock.LEASE_SUFFIX}"
        assert not lease.exists()
    finally:
        lock.release()
    assert run_lock.any_live() is False


# ---------------------------------------------------------------------------
# Fail-closed. Every uncertainty resolves to "a run may be live".
# ---------------------------------------------------------------------------


def test_a_malformed_lease_reads_as_live(isolate):
    directory = run_lock.runs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"garbage{run_lock.LEASE_SUFFIX}").write_text("{not json at all")
    assert run_lock.any_live() is True, "an unreadable lease must fail CLOSED"


def test_a_lease_with_a_nonnumeric_expiry_reads_as_live(isolate):
    directory = run_lock.runs_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"weird{run_lock.LEASE_SUFFIX}").write_text('{"expires_at": "soon"}')
    assert run_lock.any_live() is True


def test_an_unreadable_runs_directory_reads_as_live(isolate, monkeypatch):
    run_lock.touch_lease("bracket", seconds=600)

    def _boom(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(run_lock.Path, "iterdir", _boom)
    assert run_lock.any_live() is True


def test_a_platform_without_fcntl_reads_as_live(isolate, monkeypatch):
    """Windows. The outbox swallows a missing fcntl because delivery is
    best-effort; doing that here would invert the whole point of the lock on the
    one platform where we cannot see flock-tier runs at all."""
    run_lock.runs_dir().mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(run_lock, "_fcntl", lambda: None)
    assert run_lock.any_live() is True


def test_acquire_returns_none_rather_than_raising_without_fcntl(isolate, monkeypatch):
    """A run must still START on a platform we cannot lock on."""
    monkeypatch.setattr(run_lock, "_fcntl", lambda: None)
    assert run_lock.acquire("run-abc") is None


def test_the_override_forces_clear(isolate, monkeypatch):
    """The escape hatch for a user whose box is idle but whose state says
    otherwise, and for tests that need a deterministic answer."""
    run_lock.touch_lease("bracket", seconds=600)
    monkeypatch.setenv(run_lock.OVERRIDE_ENV, "1")
    assert run_lock.any_live() is False


def test_run_refs_with_slashes_do_not_escape_the_runs_directory(isolate):
    """Run refs can be slugs. A ref like `../../etc/passwd` must not write
    outside the state directory."""
    lock = run_lock.acquire("../../evil")
    assert lock is not None
    try:
        assert lock.path.parent == run_lock.runs_dir()
        assert run_lock.any_live() is True
    finally:
        lock.release()
