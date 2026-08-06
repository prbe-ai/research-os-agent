"""In-proc NVML floor: per-device GPU sampling + per-process attribution.

Coords always carry the PHYSICAL NVML index. CUDA_VISIBLE_DEVICES (integer,
GPU-<uuid>, or MIG forms) restricts which devices this run is attributed to;
CUDA ordinals are remapped by that variable, so anything ordinal-based would
point at the wrong silicon on shared machines. Unresolvable MIG entries fall
back to ALL devices — machine-scope beats mis-attribution; DCGM-exporter is
the MIG-correct path.
"""

from __future__ import annotations

import os

from probe.hw.types import HwSample

_UNSET = object()


class NvidiaResource:
    def __init__(self, nvml, pid: int, visible: set[int] | None) -> None:
        self._nvml = nvml
        self._pid = pid
        self._visible = visible  # None means "all devices"
        self._err = getattr(nvml, "NVMLError", Exception)

    @classmethod
    def create(cls, nvml=None, pid: int | None = None, visible_devices=_UNSET):
        if nvml is None:
            try:
                import pynvml as nvml  # lazy: absence disables the source
            except ImportError:
                return None
        try:
            nvml.nvmlInit()
        except Exception:
            return None  # availability probe: no NVML, no source, no error
        if pid is None:
            pid = os.getpid()
        if visible_devices is _UNSET:
            visible_devices = os.environ.get("CUDA_VISIBLE_DEVICES")
        res = cls(nvml, pid, None)
        res._visible = res._resolve_visible(visible_devices)
        return res

    # -- visibility ---------------------------------------------------------
    def _uuid_index(self) -> dict[str, int]:
        n = self._nvml.nvmlDeviceGetCount()
        out = {}
        for i in range(n):
            try:
                out[self._nvml.nvmlDeviceGetUUID(self._nvml.nvmlDeviceGetHandleByIndex(i))] = i
            except self._err:
                continue
        return out

    def _resolve_visible(self, spec) -> set[int] | None:
        if spec is None:
            return None
        entries = [e.strip() for e in str(spec).split(",") if e.strip()]
        if not entries:
            return set()  # CUDA_VISIBLE_DEVICES="" means NO devices
        by_uuid = self._uuid_index()
        count = self._nvml.nvmlDeviceGetCount()
        result: set[int] = set()
        for entry in entries:
            if entry.lstrip("-").isdigit():
                idx = int(entry)
                if 0 <= idx < count:
                    result.add(idx)
            elif entry.startswith("MIG-GPU-"):
                # Older MIG spelling embeds the parent: MIG-GPU-<uuid>/<gi>/<ci>
                parent = entry[4:].split("/")[0]
                if parent in by_uuid:
                    result.add(by_uuid[parent])
                else:
                    return None
            elif entry.startswith("MIG-"):
                return None  # modern MIG uuid: parent unknowable here
            elif entry in by_uuid:
                result.add(by_uuid[entry])
            else:
                return None  # unrecognized entry: machine-scope over mis-attribution
        return result

    # -- resource contract --------------------------------------------------
    def probe(self) -> dict:
        n = self._nvml.nvmlDeviceGetCount()
        names, totals = [], []
        for i in range(n):
            h = self._nvml.nvmlDeviceGetHandleByIndex(i)
            try:
                names.append(str(self._nvml.nvmlDeviceGetName(h)))
                totals.append(int(self._nvml.nvmlDeviceGetMemoryInfo(h).total))
            except self._err:
                names.append("unknown")
                totals.append(0)
        cuda = self._nvml.nvmlSystemGetCudaDriverVersion_v2()
        return {
            "gpu_count": n,
            "gpu_names": names,
            "gpu_memory_total_bytes": totals,
            "driver_version": str(self._nvml.nvmlSystemGetDriverVersion()),
            "cuda_driver_version": f"{cuda // 1000}.{(cuda % 1000) // 10}",
        }

    def sample(self, ts: float) -> list[HwSample]:
        samples: list[HwSample] = []
        count = self._nvml.nvmlDeviceGetCount()
        indices = range(count) if self._visible is None else sorted(self._visible)
        for i in indices:
            try:
                h = self._nvml.nvmlDeviceGetHandleByIndex(i)
            except self._err:
                continue
            coords = {"gpu": i}
            # Each metric individually guarded: one glitched sensor must not
            # take out the device's other readings.
            try:
                util = float(self._nvml.nvmlDeviceGetUtilizationRates(h).gpu)
                samples.append(
                    HwSample("hw/gpu/utilization", util, coords, "mean", ("min",))
                )
            except self._err:
                pass
            try:
                samples.append(
                    HwSample(
                        "hw/gpu/memory_used_bytes",
                        float(self._nvml.nvmlDeviceGetMemoryInfo(h).used),
                        coords,
                        "max",
                    )
                )
            except self._err:
                pass
            try:
                samples.append(
                    HwSample(
                        "hw/gpu/powerWatts",
                        self._nvml.nvmlDeviceGetPowerUsage(h) / 1000.0,
                        coords,
                        "mean",
                    )
                )
            except self._err:
                pass
            try:
                samples.append(
                    HwSample(
                        "hw/gpu/temp",
                        float(
                            self._nvml.nvmlDeviceGetTemperature(
                                h, getattr(self._nvml, "NVML_TEMPERATURE_GPU", 0)
                            )
                        ),
                        coords,
                        "max",
                    )
                )
            except self._err:
                pass
            try:
                ours = sum(
                    int(p.usedGpuMemory or 0)
                    for p in self._nvml.nvmlDeviceGetComputeRunningProcesses(h)
                    if p.pid == self._pid
                )
                if ours:
                    samples.append(
                        HwSample("hw/proc/gpu_memory_bytes", float(ours), coords, "max")
                    )
            except self._err:
                pass
        return samples

    def close(self) -> None:
        try:
            self._nvml.nvmlShutdown()
        except self._err:
            pass
