"""probe.hw.grid: the epoch step grid and window aggregation.

The hardware rail's step_index is floor(unix_seconds / 60) — a protocol
CONSTANT, not config. Any process, restart, or backfill job computing a step
for the same instant must get the same answer with zero shared state; that
determinism is what lets first-write-wins dedup redelivery, restarts, and
backfill by construction (design doc 2026-08-05, resolved decision 2).

Samples accumulate in-thread and each completed 60s window emits ONE point
per series, reduced by the metric's declared agg. Stall-sensitive metrics
also emit min/max companion keys so a 20-second all-GPUs-idle stall cannot
hide inside a 60s mean.
"""

from __future__ import annotations

import math

from probe.hw.grid import HW_STEP_SECONDS, WindowAggregator, step_for


def test_step_is_epoch_floor_over_the_protocol_constant():
    assert HW_STEP_SECONDS == 60
    # 1_782_000_000 is divisible by 60 → a window boundary.
    assert step_for(1_782_000_000.0) == 29_700_000
    # Anywhere inside the window maps to the same step…
    assert step_for(1_782_000_059.999) == 29_700_000
    # …and the next boundary starts the next step.
    assert step_for(1_782_000_060.0) == 29_700_001


def test_two_writers_at_the_same_instant_agree_without_shared_state():
    """The property the run-relative scheme could not give: no started_at
    fetch, no interval config — the timestamp alone decides the step."""
    instants = [0.0, 59.9, 60.0, 1_782_000_123.4, 2_000_000_000.0]
    assert [step_for(t) for t in instants] == [step_for(t) for t in instants]
    assert all(isinstance(step_for(t), int) for t in instants)


def test_window_reduces_by_declared_agg():
    agg = WindowAggregator()
    t0 = 1_782_000_000.0
    agg.add("hw/gpu/utilization", {"gpu": 0}, 10.0, t0 + 1, agg="mean")
    agg.add("hw/gpu/utilization", {"gpu": 0}, 30.0, t0 + 30, agg="mean")
    agg.add("hw/gpu/memory_used_bytes", {"gpu": 0}, 5.0, t0 + 2, agg="max")
    agg.add("hw/gpu/memory_used_bytes", {"gpu": 0}, 9.0, t0 + 3, agg="max")
    agg.add("hw/disk//used_percent", {}, 41.0, t0 + 4, agg="last")
    agg.add("hw/disk//used_percent", {}, 42.0, t0 + 50, agg="last")

    # Flushing after the window closed emits exactly one point per series.
    points = agg.flush_completed(now=t0 + 60.0)
    by_key = {(p.key, tuple(sorted(p.coords.items()))): p for p in points}

    assert by_key[("hw/gpu/utilization", (("gpu", 0),))].value == 20.0
    assert by_key[("hw/gpu/memory_used_bytes", (("gpu", 0),))].value == 9.0
    assert by_key[("hw/disk//used_percent", ())].value == 42.0
    assert all(p.step == 29_700_000 for p in points)
    # Points carry their declared agg onto the wire (migration 0062): grouped
    # reads reduce correctly only if the declaration rides with the point.
    assert by_key[("hw/gpu/utilization", (("gpu", 0),))].agg == "mean"
    assert by_key[("hw/gpu/memory_used_bytes", (("gpu", 0),))].agg == "max"


def test_stall_sensitive_metrics_emit_min_companion():
    """A 20s stall inside a 60s mean reads as '~67% utilization, fine' — the
    _min companion is what makes it visible (adversarial finding #7)."""
    agg = WindowAggregator()
    t0 = 1_782_000_000.0
    for i, value in enumerate([100.0, 0.0, 0.0]):
        agg.add(
            "hw/gpu/utilization", {"gpu": 1}, value, t0 + i, agg="mean", companions=("min",)
        )

    points = agg.flush_completed(now=t0 + 60.0)
    values = {p.key: p.value for p in points}

    assert round(values["hw/gpu/utilization"], 2) == round(100.0 / 3, 2)
    assert values["hw/gpu/utilization_min"] == 0.0
    # The companion series declares ITS reduce, not the parent's.
    aggs = {p.key: p.agg for p in points}
    assert aggs["hw/gpu/utilization_min"] == "min"


def test_non_finite_samples_are_dropped_at_the_source():
    """Postgres treats NaN = NaN as TRUE and non-finite values poison the
    server-side catalog fold — nothing non-finite may leave the collector."""
    agg = WindowAggregator()
    t0 = 1_782_000_000.0
    agg.add("hw/gpu/powerWatts", {"gpu": 0}, math.nan, t0 + 1, agg="mean")
    agg.add("hw/gpu/powerWatts", {"gpu": 0}, math.inf, t0 + 2, agg="mean")
    agg.add("hw/gpu/powerWatts", {"gpu": 0}, -math.inf, t0 + 3, agg="mean")

    assert agg.flush_completed(now=t0 + 60.0) == []

    # A finite sample beside garbage survives alone.
    agg.add("hw/gpu/powerWatts", {"gpu": 0}, math.nan, t0 + 61, agg="mean")
    agg.add("hw/gpu/powerWatts", {"gpu": 0}, 88.0, t0 + 62, agg="mean")
    (point,) = agg.flush_completed(now=t0 + 120.0)
    assert point.value == 88.0


def test_flush_emits_only_completed_windows():
    """The current window is still accumulating — emitting it early would
    write a partial value that first-write-wins then freezes forever."""
    agg = WindowAggregator()
    t0 = 1_782_000_000.0
    agg.add("hw/cpu/utilization", {}, 50.0, t0 + 30, agg="mean")

    assert agg.flush_completed(now=t0 + 59.0) == []
    (point,) = agg.flush_completed(now=t0 + 60.0)
    assert point.step == 29_700_000
    # Flushing again does not re-emit.
    assert agg.flush_completed(now=t0 + 61.0) == []


def test_coords_partition_series_within_a_key():
    agg = WindowAggregator()
    t0 = 1_782_000_000.0
    agg.add("hw/gpu/utilization", {"gpu": 0}, 10.0, t0 + 1, agg="mean")
    agg.add("hw/gpu/utilization", {"gpu": 1}, 90.0, t0 + 1, agg="mean")

    points = agg.flush_completed(now=t0 + 60.0)
    values = {p.coords["gpu"]: p.value for p in points}
    assert values == {0: 10.0, 1: 90.0}
