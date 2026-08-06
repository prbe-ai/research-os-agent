"""probe.hw.monitor: the collector — one daemon thread per elected node
leader, fail-open everywhere.

Testability is by injection: sources, emit, clock, env, and lock dir are all
parameters; tick() is a plain method the thread calls in a loop, so every
behavior below runs without sleeping. Contracts under test, each traceable
to a locked review decision:
- per-node election (LOCAL_RANK heuristics, file-lock fallback) — 8 DDP
  ranks must not run 8 collectors;
- identity captured at start() and stamped on every point — contextvars are
  thread-local and never reach a daemon thread;
- higher-tier family claims suppress lower tiers; a tripped breaker
  re-delegates the family back down (first-write-wins makes handoff safe);
- circuit breaker: N consecutive failures disables a source, ONE warning;
- series governor: 2500/node runaway guard, family-priority admission;
- emit failures buffer bounded, drop-oldest — hardware never spools.
"""

from __future__ import annotations

import threading

from probe.hw.grid import HW_STEP_SECONDS
from probe.hw.monitor import HwMonitor, elect_leader
from probe.hw.types import HwSample


class FakeClock:
    def __init__(self, t=1_782_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeSource:
    def __init__(self, samples=(), families=frozenset(), fail=False):
        self.samples = list(samples)
        self.families = frozenset(families)
        self.fail = fail
        self.sample_calls = 0

    def sample(self, ts):
        self.sample_calls += 1
        if self.fail:
            raise RuntimeError("sensor exploded")
        return list(self.samples)

    def probe(self):
        return {}


def _collect_emits():
    emitted = []

    def emit(points):
        emitted.extend(points)

    return emitted, emit


# -- election ---------------------------------------------------------------


def test_local_rank_nonzero_is_never_leader(tmp_path):
    assert not elect_leader("run1", env={"LOCAL_RANK": "3"}, lock_dir=str(tmp_path))


def test_local_rank_zero_is_leader(tmp_path):
    assert elect_leader("run1", env={"LOCAL_RANK": "0"}, lock_dir=str(tmp_path))


def test_no_rank_env_falls_back_to_file_lock_single_winner(tmp_path):
    first = elect_leader("run1", env={}, lock_dir=str(tmp_path))
    second = elect_leader("run1", env={}, lock_dir=str(tmp_path))
    assert first is True and second is False
    # A different run elects its own leader.
    assert elect_leader("run2", env={}, lock_dir=str(tmp_path)) is True


# -- sampling / identity ----------------------------------------------------


def _monitor(sources, emit, clock, **kw):
    return HwMonitor(
        sources=sources,
        emit=emit,
        clock=clock,
        identity={"host": "node1"},
        interval=15.0,
        **kw,
    )


def test_identity_coords_stamped_on_every_point():
    clock = FakeClock()
    emitted, emit = _collect_emits()
    src = FakeSource([HwSample("hw/cpu/utilization", 50.0, {}, "mean")], {"system"})
    mon = _monitor([src], emit, clock)

    mon.tick()
    clock.t += HW_STEP_SECONDS
    mon.tick()

    assert emitted, "completed window should have flushed"
    assert all(p.coords["host"] == "node1" for p in emitted)


def test_device_coords_survive_identity_merge():
    clock = FakeClock()
    emitted, emit = _collect_emits()
    src = FakeSource([HwSample("hw/gpu/utilization", 9.0, {"gpu": 3}, "mean")], {"gpu"})
    mon = _monitor([src], emit, clock)

    mon.tick()
    clock.t += HW_STEP_SECONDS
    mon.tick()

    (point,) = [p for p in emitted if p.key == "hw/gpu/utilization"]
    assert point.coords == {"gpu": 3, "host": "node1"}


# -- tiering / breaker ------------------------------------------------------


def test_higher_tier_claim_suppresses_lower_tier_source():
    clock = FakeClock()
    emitted, emit = _collect_emits()
    scraper = FakeSource([HwSample("hw/gpu/utilization", 1.0, {"gpu": 0}, "mean")], {"gpu"})
    nvml = FakeSource([HwSample("hw/gpu/utilization", 2.0, {"gpu": 0}, "mean")], {"gpu"})
    mon = _monitor([scraper, nvml], emit, clock)  # tier order = list order

    mon.tick()
    assert scraper.sample_calls == 1
    assert nvml.sample_calls == 0


def test_breaker_trips_and_redelegates_family_down_tier():
    clock = FakeClock()
    emitted, emit = _collect_emits()
    scraper = FakeSource(fail=True, families={"gpu"})
    nvml = FakeSource([HwSample("hw/gpu/utilization", 2.0, {"gpu": 0}, "mean")], {"gpu"})
    mon = _monitor([scraper, nvml], emit, clock, breaker_threshold=3)

    for _ in range(3):
        mon.tick()
        clock.t += 1
    assert nvml.sample_calls == 0  # still suppressed while scraper is alive

    mon.tick()  # breaker now open: family re-delegated
    assert nvml.sample_calls == 1
    assert scraper.sample_calls == 3  # disabled source is not called again


def test_source_failure_never_propagates():
    clock = FakeClock()
    _, emit = _collect_emits()
    mon = _monitor([FakeSource(fail=True, families={"gpu"})], emit, clock)
    mon.tick()  # must not raise


# -- governor ---------------------------------------------------------------


def test_governor_refuses_new_series_past_cap_with_family_priority():
    """At the cap, remaining slots go to higher-priority families first:
    hw/proc/* is the first family degraded, core gpu/system metrics last."""
    clock = FakeClock()
    emitted, emit = _collect_emits()
    samples = [
        HwSample("hw/proc/rss_bytes", 1.0, {"i": i}, "max") for i in range(3)
    ] + [HwSample("hw/gpu/utilization", 1.0, {"gpu": i}, "mean") for i in range(3)]
    src = FakeSource(samples, {"gpu", "system"})
    mon = _monitor([src], emit, clock, governor_max_series=4)

    mon.tick()
    clock.t += HW_STEP_SECONDS
    mon.tick()

    keys = sorted({(p.key, tuple(sorted(p.coords.items()))) for p in emitted})
    gpu_series = [k for k in keys if k[0] == "hw/gpu/utilization"]
    proc_series = [k for k in keys if k[0] == "hw/proc/rss_bytes"]
    assert len(gpu_series) == 3  # high priority: all admitted
    assert len(proc_series) == 1  # low priority: only the leftover slot


# -- backpressure -----------------------------------------------------------


def test_emit_failure_buffers_and_retries_then_drops_oldest():
    clock = FakeClock()
    delivered = []
    failing = {"on": True}

    def emit(points):
        if failing["on"]:
            raise ConnectionError("outage")
        delivered.extend(points)

    src = FakeSource([HwSample("hw/cpu/utilization", 5.0, {}, "mean")], {"system"})
    mon = _monitor([src], emit, clock, buffer_max_points=2)

    for _ in range(4):  # four completed windows during the outage; buffer holds 2
        clock.t += HW_STEP_SECONDS
        mon.tick()

    failing["on"] = False
    clock.t += HW_STEP_SECONDS
    mon.tick()

    steps = sorted({p.step for p in delivered})
    # Bounded buffer: the oldest windows were dropped, the newest survived.
    assert len(steps) <= 3
    assert steps[-1] == (int(clock.t) // HW_STEP_SECONDS) - 1


# -- lifecycle --------------------------------------------------------------


def test_start_and_finish_run_and_join_the_daemon_thread():
    emitted, emit = _collect_emits()
    src = FakeSource([HwSample("hw/cpu/utilization", 5.0, {}, "mean")], {"system"})
    mon = HwMonitor(
        sources=[src], emit=emit, identity={"host": "n1"}, interval=0.01
    )

    mon.start()
    assert mon._thread is not None and mon._thread.daemon
    deadline = threading.Event()
    deadline.wait(0.1)  # let a few ticks happen
    mon.finish()
    assert src.sample_calls >= 1
    assert not mon._thread.is_alive()
