"""Exporter scraper: Prometheus exposition format → hardware samples.

The zero-code extension door: DCGM-exporter, node_exporter, cAdvisor, or any
custom exporter, translated through the ONE shared MappingPack. Fetching is
injected (the monitor supplies an httpx-backed callable; tests supply pages)
and every failure path returns empty rather than raising — the circuit
breaker in the monitor owns give-up policy.
"""

from __future__ import annotations

from probe.hw.mappings import CounterTracker, MappingPack
from probe.hw.types import HwSample

# Well-known host-local candidates probed by non-blocking discovery on the
# collector's first tick. Kubelet/cAdvisor is NOT here: it needs credentials
# and is opt-in via PROBE_HW_KUBELET (design decision 5).
DEFAULT_CANDIDATES = {
    "dcgm": "http://localhost:9400/metrics",
    "node": "http://localhost:9100/metrics",
}


def parse_exposition(text: str):
    """Minimal exposition parser: yields (name, labels, value)."""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            metric, value_part = line.rsplit(None, 1)
            # A trailing timestamp would make value_part non-float; exposition
            # timestamps are rare from exporters — retry one field left.
            try:
                value = float(value_part)
            except ValueError:
                metric, value_part = metric.rsplit(None, 1)
                value = float(value_part)
            if "{" in metric:
                name, raw = metric.split("{", 1)
                raw = raw.rstrip("}")
                labels = {}
                for pair in _split_labels(raw):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        labels[k.strip()] = v.strip().strip('"')
                yield name.strip(), labels, value
            else:
                yield metric.strip(), {}, value
        except ValueError:
            continue  # malformed line: skip, never raise


def _split_labels(raw: str):
    """Split label pairs on commas outside quotes."""
    out, buf, quoted = [], [], False
    for ch in raw:
        if ch == '"':
            quoted = not quoted
            buf.append(ch)
        elif ch == "," and not quoted:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


class OpenMetricsResource:
    def __init__(
        self,
        endpoints: dict[str, str],
        fetch,
        pack: MappingPack,
        label_filters: dict[str, str],
    ) -> None:
        self._endpoints = endpoints
        self._fetch = fetch
        self._pack = pack
        self._filters = label_filters
        self._counters = CounterTracker()

    @staticmethod
    def discover(fetch, candidates: dict[str, str] | None = None) -> dict[str, str]:
        """Probe well-known endpoints; keep the ones that answer. Runs on the
        collector thread's first tick — never at run() init."""
        found = {}
        for name, url in (candidates or DEFAULT_CANDIDATES).items():
            try:
                fetch(url)
                found[name] = url
            except Exception:
                continue
        return found

    def probe(self) -> dict:
        return {"openmetrics_endpoints": sorted(self._endpoints.values())}

    def sample(self, ts: float) -> list[HwSample]:
        samples: list[HwSample] = []
        for endpoint_name, url in self._endpoints.items():
            try:
                text = self._fetch(url)
            except Exception:
                continue  # breaker upstairs decides when this endpoint is dead
            for name, labels, value in parse_exposition(text):
                if any(
                    key in labels and labels[key] != want
                    for key, want in self._filters.items()
                ):
                    continue  # someone else's pod/container
                mapped = self._pack.resolve(name, labels)
                if mapped is None:
                    continue
                if mapped.kind == "counter":
                    sid = f"{endpoint_name}:{name}:{sorted(labels.items())}"
                    rate = self._counters.observe(sid, value, ts)
                    if rate is None:
                        continue
                    value = rate
                samples.append(
                    HwSample(mapped.key, value, mapped.coords, mapped.agg, mapped.companions)
                )
        return samples
