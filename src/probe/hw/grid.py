"""Epoch step grid and window aggregation for the hardware rail.

``step_index = floor(unix_seconds / HW_STEP_SECONDS)`` is a protocol
CONSTANT: every writer — collector, restarted collector, backfill weeks
later — derives the same step for the same instant with zero shared state,
which is what lets the store's first-write-wins identity dedup redelivery
and backfill by construction. Changing HW_STEP_SECONDS is a breaking
protocol change, not a config knob; higher-resolution capture rides the
labeled per-sample rail instead.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

HW_STEP_SECONDS = 60


def step_for(ts: float) -> int:
    """Epoch grid: the hardware step for a unix timestamp."""
    return int(ts // HW_STEP_SECONDS)


@dataclass
class HwPoint:
    key: str
    coords: dict
    step: int
    value: float


@dataclass
class _Series:
    agg: str
    companions: tuple
    values: list = field(default_factory=list)  # in arrival (ts) order


_REDUCERS = {
    "mean": lambda vs: sum(vs) / len(vs),
    "max": max,
    "min": min,
    "last": lambda vs: vs[-1],
}


class WindowAggregator:
    """Accumulates samples and emits one reduced point per series per
    completed window. The current window is never emitted: a partial value
    would be frozen forever by first-write-wins."""

    def __init__(self) -> None:
        # step -> (key, coords-items) -> _Series
        self._windows: dict[int, dict[tuple, _Series]] = {}
        self._coords: dict[tuple, dict] = {}

    def add(
        self,
        key: str,
        coords: dict,
        value: float,
        ts: float,
        *,
        agg: str,
        companions: tuple = (),
    ) -> None:
        if not math.isfinite(value):
            return  # non-finite values poison the server-side catalog fold
        ckey = tuple(sorted(coords.items()))
        self._coords[ckey] = coords
        series = self._windows.setdefault(step_for(ts), {}).setdefault(
            (key, ckey), _Series(agg=agg, companions=companions)
        )
        series.values.append(value)

    def flush_completed(self, now: float) -> list[HwPoint]:
        """Emit every window strictly before the one containing ``now``."""
        current = step_for(now)
        points: list[HwPoint] = []
        for step in sorted(s for s in self._windows if s < current):
            for (key, ckey), series in self._windows.pop(step).items():
                coords = self._coords[ckey]
                points.append(
                    HwPoint(key, coords, step, _REDUCERS[series.agg](series.values))
                )
                for comp in series.companions:
                    points.append(
                        HwPoint(f"{key}_{comp}", coords, step, _REDUCERS[comp](series.values))
                    )
        return points
