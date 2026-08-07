"""The ledger lock, under real contention.

`run_units` drives 3-4 agents concurrently and every one of them appends to ONE
ledger. The flock in `Ledger._append` is the single mechanism that keeps their
lines from interleaving, and it had no test: replacing it with `if True:` left
the whole suite green, so a refactor could have removed it silently.

Threads AND processes, because they fail differently. Threads share the
interpreter and can be serialized by accident (the GIL around a small write);
separate processes cannot, and `flock` is a kernel-level claim whose whole point
is spanning them. `tests/test_outbox_process.py` makes the same argument for the
outbox journal -- this is the same shape for the ledger.

WHAT THESE TESTS DO AND DO NOT PROVE, stated plainly because the distinction
took a measurement to find. Deleting the flock from `_append` does NOT fail
them, at 25-byte records or at 40 KB ones, single-threaded or across four
processes. That is not a gap in the tests: on a local filesystem a single
`write()` to an `O_APPEND` descriptor is already atomic -- the kernel serializes
it under the inode lock -- so on APFS the OS is what keeps records whole, not us.

The lock is kept anyway, for two reasons that a mutation on this machine cannot
demonstrate:

* `O_APPEND` atomicity is a LOCAL-filesystem guarantee. NFS does not provide it,
  and `PROBE_BACKFILL_STATE_DIR` is overridable -- a team pointing it at a shared
  mount is exactly the setup this feature exists for.
* It is the seam any future read-modify-write would need. Removing it because
  today's writes happen to be single appends would leave nothing to re-add.

So these assert the property that actually matters and IS testable: under four
concurrent writers, every record survives, every line parses, and the fold sees
all of them. A test claiming to prove the lock necessary would be theatre.
"""

from __future__ import annotations

import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from multiprocessing import Process

import pytest

from probe.cli import backfill_ledger as bl

WRITERS = 4
PER_WRITER = 25

#: A realistic record: a failed unit's `error` carries the agent's last line of
#: output, which is not short. Also large enough to cross the interpreter's
#: write buffer, which is where a torn line would come from if one could.
BIG = "e" * 40_000


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    monkeypatch.setenv("PROBE_BACKFILL_STATE_DIR", str(d))
    return d


def _plan(ledger, n):
    ledger.record_plan(
        [bl.Unit(unit_id=f"u{i}", project="p", paths=(f"f{i}.py",)) for i in range(n)],
        ["p"],
    )


def test_concurrent_thread_appends_do_not_interleave(tmp_path, state_dir):
    """Every line must be complete JSON. A torn line is a lost record, and the
    reader skips it silently -- so interleaving costs data with no error."""
    led = bl.Ledger.for_folder(tmp_path)
    _plan(led, WRITERS * PER_WRITER)

    def writer(worker: int) -> None:
        for i in range(PER_WRITER):
            led.finish_unit(
                f"u{worker * PER_WRITER + i}", ok=False, enqueued=i, error=BIG
            )

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        list(pool.map(writer, range(WRITERS)))

    lines = [ln for ln in led.path.read_text().splitlines() if ln.strip()]
    for ln in lines:
        json.loads(ln)  # raises on a torn line, which is the point
    assert len(lines) == 1 + WRITERS * PER_WRITER  # the plan record plus each finish


def test_every_thread_append_survives_the_fold(tmp_path, state_dir):
    led = bl.Ledger.for_folder(tmp_path)
    total = WRITERS * PER_WRITER
    _plan(led, total)

    def writer(worker: int) -> None:
        for i in range(PER_WRITER):
            led.finish_unit(f"u{worker * PER_WRITER + i}", ok=True, error=None)

    with ThreadPoolExecutor(max_workers=WRITERS) as pool:
        list(pool.map(writer, range(WRITERS)))

    state = led.read()
    assert state.progress() == (total, total)
    assert state.outstanding() == []


def _die_holding(path: str) -> None:  # pragma: no cover - subprocess
    """Take the lock, then die without releasing it. Must be module level:
    multiprocessing pickles the target, and a closure cannot be pickled."""
    from probe.sdk.durable import file_lock

    with file_lock(path):
        os._exit(9)


def _child(ledger_path: str, worker: int, per: int) -> None:  # pragma: no cover - subprocess
    led = bl.Ledger(bl.Path(ledger_path))
    for i in range(per):
        led.finish_unit(f"u{worker * per + i}", ok=False, error="e" * 40_000)


@pytest.mark.skipif(sys.platform == "win32", reason="flock semantics differ")
def test_concurrent_process_appends_do_not_interleave(tmp_path, state_dir):
    """The case threads cannot prove. `flock` is a kernel claim; its reason to
    exist is spanning processes, which is what a resumed import plus a still-
    running one actually looks like."""
    led = bl.Ledger.for_folder(tmp_path)
    total = WRITERS * PER_WRITER
    _plan(led, total)

    procs = [
        Process(target=_child, args=(str(led.path), w, PER_WRITER)) for w in range(WRITERS)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=60)
        assert p.exitcode == 0, "a writer died"

    lines = [ln for ln in led.path.read_text().splitlines() if ln.strip()]
    for ln in lines:
        json.loads(ln)
    assert len(lines) == 1 + total
    assert len(led.read().units) == total


def test_the_state_directory_is_owner_only(tmp_path, state_dir):
    """The lock sidecar carries no data, so its own mode does not matter -- the
    DIRECTORY is what keeps the ledger and everything beside it private."""
    led = bl.Ledger.for_folder(tmp_path)
    led.open_import(tmp_path, files=1, bytes_=1)
    assert (led.path.parent.stat().st_mode & 0o077) == 0
    assert (led.path.stat().st_mode & 0o077) == 0


def test_a_writer_that_dies_holding_the_lock_does_not_wedge_the_next_one(
    tmp_path, state_dir
):
    """The kernel releases a flock when the fd closes, including on a crash. If
    it did not, one killed agent would stall every other unit forever."""
    led = bl.Ledger.for_folder(tmp_path)
    _plan(led, 2)

    p = Process(target=_die_holding, args=(str(led.lock_path),))
    p.start()
    p.join(timeout=30)
    assert p.exitcode == 9

    led.finish_unit("u0", ok=True)  # must not hang
    assert led.read().progress() == (1, 2)
