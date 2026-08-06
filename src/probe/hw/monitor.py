"""The hardware collector: one daemon thread per elected node leader.

Fail-open is the contract at every layer — a source failure trips a breaker
(one warning, family re-delegated down-tier), an emit failure buffers
bounded with drop-oldest (hardware never spools, never competes with
training metrics), and nothing here can raise into user code.
"""

from __future__ import annotations

import logging
import os
import socket
import tempfile
import threading
import time

from probe.hw.grid import HwPoint, WindowAggregator

logger = logging.getLogger(__name__)

_RANK_VARS = ("LOCAL_RANK", "SLURM_LOCALID", "PMI_LOCAL_RANK", "OMPI_COMM_WORLD_LOCAL_RANK")

# Governor admission priority by key family: when slots run out, low-priority
# families are degraded first (per-process detail before core gpu/system).
_FAMILY_PRIORITY = {"hw/net": 1, "hw/disk": 1, "hw/proc": 2}


def _priority(key: str) -> int:
    prefix = "/".join(key.split("/", 2)[:2])
    return _FAMILY_PRIORITY.get(prefix, 0)


def elect_leader(run_id: str, env=None, lock_dir: str | None = None) -> bool:
    """One collector per node per run. Rank env vars are authoritative when
    present (torchrun/SLURM/MPI); otherwise first-past-the-file-lock wins.
    The lock is per (run, host): a crashed leader means no collector for the
    rest of that run — acceptable for a best-effort rail, and backfill can
    reconstruct the gap."""
    env = os.environ if env is None else env
    for var in _RANK_VARS:
        if var in env:
            return str(env[var]).strip() == "0"
    lock_dir = lock_dir or tempfile.gettempdir()
    path = os.path.join(
        lock_dir, f"probe-hw-{run_id}-{socket.gethostname()}.lock"
    )
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return True  # unwritable lock dir: better one collector than none
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return True


class HwMonitor:
    def __init__(
        self,
        sources,
        emit,
        *,
        identity: dict | None = None,
        clock=time.time,
        interval: float = 15.0,
        governor_max_series: int = 2500,
        breaker_threshold: int = 5,
        buffer_max_points: int = 10_000,
    ) -> None:
        self._sources = list(sources)
        self._emit = emit
        self._identity = dict(identity or {})  # captured ONCE; contextvars
        # are thread-local and never reach this daemon thread.
        self._clock = clock
        self._interval = interval
        self._governor_max = governor_max_series
        self._breaker_threshold = breaker_threshold
        self._buffer_max = buffer_max_points

        self._agg = WindowAggregator()
        self._series_seen: set = set()
        self._fail_counts: dict[int, int] = {}
        self._disabled: set[int] = set()
        self._warned: set[str] = set()
        self._buffer: list[HwPoint] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- tier claims --------------------------------------------------------
    def _enabled_sources(self):
        claimed: dict[str, int] = {}
        enabled = []
        for idx, src in enumerate(self._sources):
            if idx in self._disabled:
                continue
            families = getattr(src, "families", frozenset())
            if families and all(f in claimed for f in families):
                continue  # a live higher tier claimed everything this one has
            for family in families:
                claimed.setdefault(family, idx)
            enabled.append((idx, src))
        return enabled

    # -- one tick -----------------------------------------------------------
    def tick(self) -> None:
        now = self._clock()
        for idx, src in self._enabled_sources():
            try:
                samples = src.sample(now)
            except Exception as exc:  # noqa: BLE001 — fail-open by contract
                self._fail_counts[idx] = self._fail_counts.get(idx, 0) + 1
                if self._fail_counts[idx] >= self._breaker_threshold:
                    self._disabled.add(idx)
                    self._warn(
                        f"breaker:{idx}",
                        "hw: source %r disabled after %d consecutive failures "
                        "(last: %s); family re-delegates down-tier",
                        type(src).__name__,
                        self._fail_counts[idx],
                        exc,
                    )
                continue
            self._fail_counts[idx] = 0
            self._admit(samples, now)

        flushed = self._agg.flush_completed(now)
        pending = self._buffer + flushed
        if not pending:
            return
        try:
            self._emit(pending)
            self._buffer = []
        except Exception:  # noqa: BLE001 — drop-oldest, never spool, never raise
            self._buffer = pending[-self._buffer_max :]

    def _admit(self, samples, now: float) -> None:
        # Higher-priority families claim remaining governor slots first.
        for sample in sorted(samples, key=lambda s: _priority(s.key)):
            sid = (sample.key, tuple(sorted(sample.coords.items())))
            if sid not in self._series_seen:
                if len(self._series_seen) >= self._governor_max:
                    self._warn(
                        "governor",
                        "hw: series governor cap (%d) reached — new hardware "
                        "series are being dropped (runaway-mapping guard)",
                        self._governor_max,
                    )
                    continue
                self._series_seen.add(sid)
            coords = {**self._identity, **sample.coords}
            self._agg.add(
                sample.key,
                coords,
                sample.value,
                now,
                agg=sample.agg,
                companions=sample.companions,
            )

    def _warn(self, once_key: str, msg: str, *args) -> None:
        if once_key not in self._warned:
            self._warned.add(once_key)
            logger.warning(msg, *args)

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="probe-hw-monitor", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception:  # noqa: BLE001 — the loop must survive anything
                logger.debug("hw: tick failed", exc_info=True)

    def finish(self) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=5.0)
        try:
            self.tick()  # flush windows completed during shutdown
        except Exception:  # noqa: BLE001
            pass
