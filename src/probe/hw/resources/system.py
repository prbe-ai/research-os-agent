"""The psutil floor: CPU, memory, disk, network on any machine, plus a
cgroup-v2 quota reader so containerized runs report percent-of-quota, not
percent-of-host — even when the kubelet scrape is off.
"""

from __future__ import annotations

import socket
from pathlib import Path

from probe.hw.mappings import CounterTracker
from probe.hw.types import HwSample


class SystemResource:
    def __init__(self, psutil_mod, cgroup_root: str) -> None:
        self._psutil = psutil_mod
        self._cgroup = Path(cgroup_root)
        self._counters = CounterTracker()

    @classmethod
    def create(cls, cgroup_root: str = "/sys/fs/cgroup"):
        try:
            import psutil  # lazy: absence disables the source, never errors
        except ImportError:
            return None
        return cls(psutil, cgroup_root)

    def probe(self) -> dict:
        return {
            "cpu_count": self._psutil.cpu_count(logical=True) or 1,
            "memory_total_bytes": self._psutil.virtual_memory().total,
            "hostname": socket.gethostname(),
        }

    def sample(self, ts: float) -> list[HwSample]:
        samples: list[HwSample] = []
        # Non-blocking: percent since the previous call (first call baselines at 0.0).
        cpu = self._psutil.cpu_percent(interval=None)
        samples.append(HwSample("hw/cpu/utilization", float(cpu), {}, "mean", ("min",)))
        samples.append(
            HwSample(
                "hw/mem/used_percent",
                float(self._psutil.virtual_memory().percent),
                {},
                "max",
            )
        )
        try:
            usage = self._psutil.disk_usage("/")
            samples.append(
                HwSample("hw/disk/used_percent", float(usage.percent), {"mount": "/"}, "max")
            )
        except OSError:
            pass

        net = self._psutil.net_io_counters()
        for sid, key, raw in (
            ("net.sent", "hw/net/sent_bytes_rate", net.bytes_sent),
            ("net.recv", "hw/net/recv_bytes_rate", net.bytes_recv),
        ):
            rate = self._counters.observe(sid, float(raw), ts)
            if rate is not None:
                samples.append(HwSample(key, rate, {}, "mean"))

        samples.extend(self._cgroup_quota_samples())
        return samples

    def _cgroup_quota_samples(self) -> list[HwSample]:
        samples: list[HwSample] = []
        mem_max = self._read(self._cgroup / "memory.max")
        mem_cur = self._read(self._cgroup / "memory.current")
        if mem_max and mem_max != "max" and mem_cur:
            limit = float(mem_max)
            if limit > 0:
                samples.append(
                    HwSample(
                        "hw/mem/quota_used_percent",
                        float(mem_cur) / limit * 100.0,
                        {},
                        "max",
                    )
                )
        cpu_max = self._read(self._cgroup / "cpu.max")
        if cpu_max:
            parts = cpu_max.split()
            if len(parts) == 2 and parts[0] != "max":
                quota, period = float(parts[0]), float(parts[1])
                if period > 0:
                    # A constant reduces exactly under mean — and 'last' is
                    # not in the server's agg enum (SERVER_AGGS).
                    samples.append(
                        HwSample("hw/cpu/quota_cores", quota / period, {}, "mean")
                    )
        return samples

    @staticmethod
    def _read(path: Path) -> str | None:
        try:
            return path.read_text().strip()
        except OSError:
            return None
