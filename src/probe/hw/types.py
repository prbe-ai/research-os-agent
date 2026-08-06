"""Shared sample shape emitted by every hardware source."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class HwSample:
    key: str
    value: float
    coords: dict
    agg: str
    companions: tuple = ()
