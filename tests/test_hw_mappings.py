"""probe.hw.mappings: ONE translation table shared by every Prometheus-shaped
source (exporter scraper now, PromQL in 1b) — metric name → (key, coords,
counter/gauge, declared agg, companions). Implemented twice it would drift;
this module is the single authority (design doc, code-quality finding 1).

Counter semantics live here too: counters are cumulative, so a source keeps a
CounterTracker per series — first observation baselines (no output), later
observations emit a per-second rate, and a NEGATIVE delta means the exporter
restarted: drop that window and re-baseline, never emit the classic
homemade-rate spike (adversarial finding #11).
"""

from __future__ import annotations

from probe.hw.mappings import CounterTracker, MappingPack


def test_dcgm_utilization_maps_to_gpu_coord_and_mean_with_min_companion():
    pack = MappingPack.default()
    m = pack.resolve("DCGM_FI_DEV_GPU_UTIL", {"gpu": "3", "UUID": "GPU-abc"})
    assert m is not None
    assert m.key == "hw/gpu/utilization"
    assert m.coords == {"gpu": 3}
    assert m.kind == "gauge"
    assert m.agg == "mean"
    assert "min" in m.companions


def test_dcgm_memory_and_temp_map_to_max_agg():
    pack = MappingPack.default()
    mem = pack.resolve("DCGM_FI_DEV_FB_USED", {"gpu": "0"})
    temp = pack.resolve("DCGM_FI_DEV_GPU_TEMP", {"gpu": "0"})
    assert mem.key == "hw/gpu/memory_used_bytes" and mem.agg == "max"
    assert temp.key == "hw/gpu/temp" and temp.agg == "max"


def test_node_exporter_counters_are_classified_as_counters():
    pack = MappingPack.default()
    tx = pack.resolve("node_network_transmit_bytes_total", {"device": "eth0"})
    assert tx.kind == "counter"
    assert tx.key == "hw/net/sent_bytes_rate"
    assert tx.coords == {"nic": "eth0"}


def test_cadvisor_working_set_and_throttling_map_to_proc_family():
    pack = MappingPack.default()
    ws = pack.resolve("container_memory_working_set_bytes", {"pod": "train-0"})
    throttle = pack.resolve("container_cpu_cfs_throttled_periods_total", {"pod": "train-0"})
    assert ws.key == "hw/proc/memory_working_set_bytes"
    assert throttle.kind == "counter"


def test_every_declared_agg_is_in_the_server_vocabulary():
    """The server's MetricPointIn.agg is a closed enum; one illegal value
    422s the WHOLE batch, every emit fails into the drop-oldest buffer, and
    the rail goes silently dark — found live in prod (2026-08-06) because
    the sim only exercised mean/max. Pin the entire default pack, and the
    availability metrics specifically: 'min' is both legal AND the right
    semantic (the pressure signal is the LOWEST headroom in the window)."""
    from probe.hw.mappings import _DEFAULT
    from probe.hw.types import SERVER_AGGS

    pack = MappingPack.default()
    for name, entry in _DEFAULT.items():
        assert entry["agg"] in SERVER_AGGS, f"{name} declares illegal agg {entry['agg']}"
        for comp in entry.get("companions", ()):
            assert comp in SERVER_AGGS, f"{name} companion {comp} illegal"
    assert pack.resolve("node_memory_MemAvailable_bytes", {}).agg == "min"
    assert (
        pack.resolve("node_filesystem_avail_bytes", {"mountpoint": "/"}).agg == "min"
    )


def test_unmapped_metrics_resolve_to_none():
    pack = MappingPack.default()
    assert pack.resolve("go_goroutines", {}) is None


def test_user_entries_extend_the_default_pack():
    pack = MappingPack.default().extend(
        {
            "ipmi_fan_speed_rpm": {
                "key": "hw/system/fan_rpm",
                "coord_labels": {"fan": "fan"},
                "kind": "gauge",
                "agg": "mean",
            }
        }
    )
    m = pack.resolve("ipmi_fan_speed_rpm", {"fan": "2"})
    assert m.key == "hw/system/fan_rpm"
    assert m.coords == {"fan": "2"}


def test_counter_tracker_baselines_then_rates():
    c = CounterTracker()
    assert c.observe("s1", 1000.0, ts=100.0) is None  # first sight: baseline only
    assert c.observe("s1", 1600.0, ts=160.0) == 10.0  # 600 over 60s


def test_counter_reset_drops_the_window_and_rebaselines():
    """Exporter restart: counters restart at zero. A naive delta would emit a
    huge negative (or clamped-absurd) rate — the contract is silence, then
    normal rates from the new baseline."""
    c = CounterTracker()
    c.observe("s1", 5000.0, ts=100.0)
    assert c.observe("s1", 40.0, ts=160.0) is None  # reset detected: no spike
    assert c.observe("s1", 640.0, ts=220.0) == 10.0  # rates resume off new baseline
