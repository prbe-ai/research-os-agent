"""probe.hw.resources.openmetrics: the exporter scraper — the zero-code door
to DCGM-exporter, node_exporter, cAdvisor, and anything else that speaks
Prometheus exposition format.

Unit tests inject a fetch callable (no sockets); the real-HTTP path is the
docker-gated Prometheus E2E. Attribution: cluster-scoped exporters report
every pod on the node, so label filters restrict to OUR pod — metrics
without the filtered label (host-local exporters like DCGM) pass through.
Fetch failures return empty, never raise: fail-open is the monitor's
contract, the circuit breaker upstairs decides when to give up.
"""

from __future__ import annotations

from probe.hw.mappings import MappingPack
from probe.hw.resources.openmetrics import OpenMetricsResource

DCGM_PAGE = """\
# HELP DCGM_FI_DEV_GPU_UTIL GPU utilization.
# TYPE DCGM_FI_DEV_GPU_UTIL gauge
DCGM_FI_DEV_GPU_UTIL{gpu="0",UUID="GPU-aaa"} 95
DCGM_FI_DEV_GPU_UTIL{gpu="1",UUID="GPU-bbb"} 5
go_goroutines 42
"""

CADVISOR_PAGE = """\
container_memory_working_set_bytes{pod="train-0",container="main"} 100
container_memory_working_set_bytes{pod="other",container="main"} 999
"""

NODE_PAGE_T0 = 'node_network_transmit_bytes_total{device="eth0"} 1000\n'
NODE_PAGE_T1 = 'node_network_transmit_bytes_total{device="eth0"} 1600\n'


def _res(pages: dict[str, str], label_filters=None):
    return OpenMetricsResource(
        endpoints={name: f"http://x/{name}" for name in pages},
        fetch=lambda url: pages[url.rsplit("/", 1)[-1]],
        pack=MappingPack.default(),
        label_filters=label_filters or {},
    )


def test_parses_exposition_and_maps_through_the_pack():
    samples = _res({"dcgm": DCGM_PAGE}).sample(ts=0.0)
    by_gpu = {s.coords["gpu"]: s for s in samples if s.key == "hw/gpu/utilization"}

    assert by_gpu[0].value == 95.0 and by_gpu[1].value == 5.0
    assert by_gpu[0].agg == "mean" and "min" in by_gpu[0].companions
    # go_goroutines is unmapped → dropped.
    assert all(s.key.startswith("hw/") for s in samples)


def test_counters_baseline_then_emit_rates():
    pages = {"node": NODE_PAGE_T0}
    res = _res(pages)

    assert res.sample(ts=100.0) == []  # baseline pass: counter only, no output
    pages["node"] = NODE_PAGE_T1
    (sample,) = res.sample(ts=160.0)
    assert sample.key == "hw/net/sent_bytes_rate"
    assert sample.value == 10.0  # 600 bytes over 60s
    assert sample.coords == {"nic": "eth0"}


def test_label_filters_drop_other_pods_and_keep_unlabeled_metrics():
    res = _res(
        {"cadvisor": CADVISOR_PAGE, "dcgm": DCGM_PAGE},
        label_filters={"pod": "train-0"},
    )
    samples = res.sample(ts=0.0)

    ws = [s for s in samples if s.key == "hw/proc/memory_working_set_bytes"]
    assert [s.value for s in ws] == [100.0]  # the other pod's 999 is gone
    # DCGM metrics carry no pod label — the filter must not eat them.
    assert any(s.key == "hw/gpu/utilization" for s in samples)


def test_fetch_failure_yields_empty_never_raises():
    def broken(url):
        raise ConnectionError("exporter down")

    res = OpenMetricsResource(
        endpoints={"dcgm": "http://x/dcgm"},
        fetch=broken,
        pack=MappingPack.default(),
        label_filters={},
    )
    assert res.sample(ts=0.0) == []


def test_discovery_returns_only_responding_candidates():
    def fetch(url):
        if "9400" in url:
            return DCGM_PAGE
        raise ConnectionError("nothing there")

    found = OpenMetricsResource.discover(
        fetch=fetch,
        candidates={
            "dcgm": "http://localhost:9400/metrics",
            "node": "http://localhost:9100/metrics",
        },
    )
    assert found == {"dcgm": "http://localhost:9400/metrics"}
