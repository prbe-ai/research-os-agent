"""probe.hw.resources.nvidia: the in-proc NVML floor.

Two things only this source can do: per-process GPU attribution (exporters
are machine-scoped), and sampling with the RIGHT device identity. Coords
always carry the PHYSICAL NVML index — CUDA ordinals are remapped by
CUDA_VISIBLE_DEVICES (which may hold integers, GPU-<uuid>, or MIG forms) and
CUDA_DEVICE_ORDER, so naive index attribution points at the wrong silicon on
shared machines (eng review, architecture finding 2; adversarial finding #15
for the UUID/MIG forms).

The fake NVML below is dependency-injected; a live-GPU test is @gpu-marked
elsewhere and skips on machines without NVML.
"""

from __future__ import annotations

from dataclasses import dataclass


from probe.hw.resources.nvidia import NvidiaResource


class FakeNvmlError(Exception):
    pass


@dataclass
class _Proc:
    pid: int
    usedGpuMemory: int


class FakeNvml:
    """The slice of pynvml the resource touches, over a device table."""

    NVMLError = FakeNvmlError
    NVML_TEMPERATURE_GPU = 0

    def __init__(self, devices, fail_init=False):
        self.devices = devices
        self.fail_init = fail_init
        self.shutdown_called = False

    def nvmlInit(self):
        if self.fail_init:
            raise FakeNvmlError("no NVML on this machine")

    def nvmlShutdown(self):
        self.shutdown_called = True

    def nvmlDeviceGetCount(self):
        return len(self.devices)

    def nvmlDeviceGetHandleByIndex(self, i):
        return i  # handles are just indices in the fake

    def nvmlDeviceGetUUID(self, h):
        return self.devices[h]["uuid"]

    def nvmlDeviceGetName(self, h):
        return self.devices[h]["name"]

    def nvmlDeviceGetUtilizationRates(self, h):
        class _U:
            gpu = self.devices[h]["util"]

        return _U()

    def nvmlDeviceGetMemoryInfo(self, h):
        class _M:
            used = self.devices[h]["mem_used"]
            total = self.devices[h]["mem_total"]

        return _M()

    def nvmlDeviceGetPowerUsage(self, h):  # milliwatts
        return self.devices[h]["power_mw"]

    def nvmlDeviceGetTemperature(self, h, _sensor):
        return self.devices[h]["temp"]

    def nvmlDeviceGetComputeRunningProcesses(self, h):
        return [_Proc(**p) for p in self.devices[h].get("procs", [])]

    def nvmlSystemGetDriverVersion(self):
        return "560.35.03"

    def nvmlSystemGetCudaDriverVersion_v2(self):
        return 12060


def _two_gpus():
    return [
        {
            "uuid": "GPU-aaa",
            "name": "H100 80GB",
            "util": 95,
            "mem_used": 60_000,
            "mem_total": 80_000,
            "power_mw": 350_000,
            "temp": 55,
            "procs": [{"pid": 4242, "usedGpuMemory": 59_000}],
        },
        {
            "uuid": "GPU-bbb",
            "name": "H100 80GB",
            "util": 5,
            "mem_used": 1_000,
            "mem_total": 80_000,
            "power_mw": 90_000,
            "temp": 31,
            "procs": [],
        },
    ]


def _by_key_coords(samples):
    return {(s.key, tuple(sorted(s.coords.items()))): s for s in samples}


def test_create_returns_none_when_nvml_init_fails():
    assert NvidiaResource.create(nvml=FakeNvml([], fail_init=True), pid=1) is None


def test_samples_carry_physical_index_coords_and_agg_classes():
    res = NvidiaResource.create(nvml=FakeNvml(_two_gpus()), pid=1)
    samples = _by_key_coords(res.sample(ts=0.0))

    util0 = samples[("hw/gpu/utilization", (("gpu", 0),))]
    assert util0.value == 95.0 and util0.agg == "mean" and "min" in util0.companions
    assert samples[("hw/gpu/memory_used_bytes", (("gpu", 1),))].agg == "max"
    assert samples[("hw/gpu/powerWatts", (("gpu", 0),))].value == 350.0  # mW → W
    assert samples[("hw/gpu/temp", (("gpu", 1),))].value == 31.0


def test_process_scoped_memory_attributes_only_our_pid():
    res = NvidiaResource.create(nvml=FakeNvml(_two_gpus()), pid=4242)
    samples = _by_key_coords(res.sample(ts=0.0))

    assert samples[("hw/proc/gpu_memory_bytes", (("gpu", 0),))].value == 59_000.0
    assert ("hw/proc/gpu_memory_bytes", (("gpu", 1),)) not in samples


def test_cuda_visible_devices_integer_form_restricts_sampling():
    res = NvidiaResource.create(
        nvml=FakeNvml(_two_gpus()), pid=1, visible_devices="1"
    )
    coords = {s.coords.get("gpu") for s in res.sample(ts=0.0)}
    assert coords == {1}


def test_cuda_visible_devices_uuid_form_maps_to_physical_index():
    res = NvidiaResource.create(
        nvml=FakeNvml(_two_gpus()), pid=1, visible_devices="GPU-bbb"
    )
    coords = {s.coords.get("gpu") for s in res.sample(ts=0.0)}
    assert coords == {1}


def test_mig_form_with_parent_uuid_resolves_to_parent_device():
    """Older MIG spelling embeds the parent: MIG-GPU-<uuid>/<gi>/<ci>."""
    res = NvidiaResource.create(
        nvml=FakeNvml(_two_gpus()), pid=1, visible_devices="MIG-GPU-bbb/1/0"
    )
    coords = {s.coords.get("gpu") for s in res.sample(ts=0.0)}
    assert coords == {1}


def test_unresolvable_mig_form_falls_back_to_all_devices():
    """Modern MIG UUIDs don't embed the parent; mis-attributing to the wrong
    GPU would be worse than machine-scope, so the fallback is ALL devices
    (documented limitation; DCGM is the MIG-correct path)."""
    res = NvidiaResource.create(
        nvml=FakeNvml(_two_gpus()), pid=1, visible_devices="MIG-3f9a"
    )
    coords = {s.coords.get("gpu") for s in res.sample(ts=0.0)}
    assert coords == {0, 1}


def test_probe_reports_gpu_inventory():
    res = NvidiaResource.create(nvml=FakeNvml(_two_gpus()), pid=1)
    inv = res.probe()

    assert inv["gpu_count"] == 2
    assert inv["gpu_names"] == ["H100 80GB", "H100 80GB"]
    assert inv["driver_version"] == "560.35.03"
    assert inv["cuda_driver_version"] == "12.6"
    assert inv["gpu_memory_total_bytes"] == [80_000, 80_000]


def test_close_shuts_nvml_down():
    fake = FakeNvml(_two_gpus())
    res = NvidiaResource.create(nvml=fake, pid=1)
    res.close()
    assert fake.shutdown_called


def test_sampling_error_on_one_device_does_not_kill_the_rest():
    fake = FakeNvml(_two_gpus())

    def boom(_h):
        raise FakeNvmlError("sensor glitch")

    fake.nvmlDeviceGetTemperature = lambda h, _s: (_ for _ in ()).throw(
        FakeNvmlError("glitch")
    ) if h == 0 else 31

    res = NvidiaResource.create(nvml=fake, pid=1)
    samples = _by_key_coords(res.sample(ts=0.0))

    assert ("hw/gpu/temp", (("gpu", 0),)) not in samples  # glitched sensor skipped
    assert ("hw/gpu/utilization", (("gpu", 0),)) in samples  # device still sampled
    assert samples[("hw/gpu/temp", (("gpu", 1),))].value == 31.0
