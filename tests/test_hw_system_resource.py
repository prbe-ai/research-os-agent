"""probe.hw.resources.system: the psutil floor + cgroup-v2 quota reader.

The floor is what a bare machine gets when no exporter exists. Container
quota-awareness comes from a ~50-line cgroup-v2 reader (memory.max /
memory.current / cpu.max) so "memory %" means percent-of-quota inside a
container even when the kubelet scrape is off (design doc, tier 3).
Network/disk byte counters ride the shared CounterTracker: first sample
baselines, later samples emit rates, never cumulative values.
"""

from __future__ import annotations

import math

from probe.hw.resources.system import SystemResource


def _by_key(samples):
    return {s.key: s for s in samples}


def test_sample_emits_core_system_keys_with_finite_values():
    res = SystemResource.create()
    assert res is not None  # psutil is a hard dep of the package
    samples = _by_key(res.sample(ts=1_782_000_000.0))

    for key in ("hw/cpu/utilization", "hw/mem/used_percent"):
        assert key in samples, f"missing {key}"
        assert math.isfinite(samples[key].value)
    # At least one disk usage reading (the root mount on any real machine).
    assert any(k.startswith("hw/disk/") for k in samples)


def test_network_counters_baseline_then_rate():
    res = SystemResource.create()
    first = _by_key(res.sample(ts=1_782_000_000.0))
    assert not any(k.startswith("hw/net/") for k in first)  # baseline pass

    second = _by_key(res.sample(ts=1_782_000_015.0))
    assert "hw/net/sent_bytes_rate" in second
    assert second["hw/net/sent_bytes_rate"].value >= 0.0


def test_cgroup_v2_quota_relative_memory_and_cpu(tmp_path):
    (tmp_path / "memory.max").write_text("1073741824\n")
    (tmp_path / "memory.current").write_text("536870912\n")
    (tmp_path / "cpu.max").write_text("200000 100000\n")

    res = SystemResource.create(cgroup_root=str(tmp_path))
    samples = _by_key(res.sample(ts=1_782_000_000.0))

    assert samples["hw/mem/quota_used_percent"].value == 50.0
    assert samples["hw/cpu/quota_cores"].value == 2.0


def test_unlimited_or_absent_cgroup_emits_no_quota_keys(tmp_path):
    (tmp_path / "memory.max").write_text("max\n")  # v2 spelling of "no limit"

    res = SystemResource.create(cgroup_root=str(tmp_path))
    samples = _by_key(res.sample(ts=1_782_000_000.0))

    assert not any("quota" in k for k in samples)


def test_probe_reports_host_inventory():
    res = SystemResource.create()
    inventory = res.probe()

    assert inventory["cpu_count"] >= 1
    assert inventory["memory_total_bytes"] > 0
    assert isinstance(inventory["hostname"], str) and inventory["hostname"]
