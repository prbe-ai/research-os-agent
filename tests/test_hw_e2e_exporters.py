"""E2E against real exporters: the scraper vs an actual node_exporter.

Unit tests prove the parser against synthetic pages; THIS proves the real
thing — a live prom/node-exporter container's exposition output flows
through discovery → parse → mapping pack → hw samples with rates. Docker-
gated: auto-skips wherever Docker is absent (laptops without it, CI shards).

Also here: the @gpu live-NVML proof, which skips without a GPU and runs on
the Nebius smoke.
"""

from __future__ import annotations

import shutil
import subprocess
import time
import urllib.request

import pytest

from probe.hw.mappings import MappingPack
from probe.hw.resources.nvidia import NvidiaResource
from probe.hw.resources.openmetrics import OpenMetricsResource

_PORT = 19100  # high port to avoid a real node_exporter on :9100


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=True
        )
        return True
    except Exception:
        return False


docker_required = pytest.mark.skipif(
    not _docker_ready(), reason="docker unavailable"
)


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.read().decode()


@pytest.fixture(scope="module")
def node_exporter():
    container = subprocess.run(
        [
            "docker", "run", "--rm", "-d",
            "-p", f"{_PORT}:9100",
            "prom/node-exporter:latest",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    url = f"http://localhost:{_PORT}/metrics"
    try:
        deadline = time.time() + 30
        while True:
            try:
                _fetch(url)
                break
            except Exception:
                if time.time() > deadline:
                    raise RuntimeError("node_exporter never became ready")
                time.sleep(0.5)
        yield url
    finally:
        subprocess.run(["docker", "rm", "-f", container], capture_output=True)


@docker_required
def test_discovery_finds_a_real_exporter(node_exporter):
    found = OpenMetricsResource.discover(
        fetch=_fetch, candidates={"node": node_exporter}
    )
    assert found == {"node": node_exporter}


@docker_required
def test_real_exposition_flows_through_the_pack_to_hw_samples(node_exporter):
    res = OpenMetricsResource(
        endpoints={"node": node_exporter},
        fetch=_fetch,
        pack=MappingPack.default(),
        label_filters={},
    )
    first = res.sample(ts=time.time())
    keys = {s.key for s in first}
    # Gauges land on the first pass; a real Linux node_exporter always
    # reports available memory.
    assert "hw/mem/available_bytes" in keys
    mem = next(s for s in first if s.key == "hw/mem/available_bytes")
    assert mem.value > 0

    time.sleep(1.1)
    second = res.sample(ts=time.time())
    second_keys = {s.key for s in second}
    # Counters baseline on the first pass and emit rates on the second.
    assert "hw/net/recv_bytes_rate" in second_keys or "hw/net/sent_bytes_rate" in second_keys
    for s in second:
        if s.key.endswith("_rate"):
            assert s.value >= 0.0
            assert s.coords.get("nic")  # device label became the nic coord


def _nvml_present() -> bool:
    try:
        import pynvml

        pynvml.nvmlInit()
        pynvml.nvmlShutdown()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _nvml_present(), reason="no NVML/GPU on this machine")
def test_live_nvml_sampling_and_inventory():
    res = NvidiaResource.create()
    assert res is not None
    try:
        inventory = res.probe()
        assert inventory["gpu_count"] >= 1
        samples = res.sample(ts=time.time())
        keys = {s.key for s in samples}
        assert "hw/gpu/utilization" in keys
        assert all(s.coords.get("gpu") is not None for s in samples)
    finally:
        res.close()
