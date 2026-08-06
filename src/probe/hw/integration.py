"""Wiring between the SDK and the collector: default-on at ``run()``,
election, identity snapshot, the wire emit, and inventory publication.

Everything here is fail-open: a broken collector must never touch the run.
"""

from __future__ import annotations

import logging
import os
import socket
import threading

from probe.hw.grid import HW_STEP_SECONDS
from probe.hw.mappings import MappingPack
from probe.hw.monitor import HwMonitor, elect_leader
from probe.hw.resources.nvidia import NvidiaResource
from probe.hw.resources.openmetrics import DEFAULT_CANDIDATES, OpenMetricsResource
from probe.hw.resources.system import SystemResource

logger = logging.getLogger(__name__)

# A resume-receipt last_step at or above this is in hardware's epoch range
# (floor(unix/60) ≈ 29.7M in 2026; training steps live orders of magnitude
# lower): the server predates the #364 hardware exclusion. Warn and skip
# arming — refusing every training step would gate the run (warn-never-gate).
SUSPECT_RESUME_FLOOR = 10_000_000

_FAMILY_BY_ENDPOINT = {"dcgm": "gpu", "node": "system", "cadvisor": "container"}


def hw_enabled(hw_flag: bool | None, env=None) -> bool:
    if hw_flag is False:
        return False
    env = os.environ if env is None else env
    return str(env.get("PROBE_HW", "1")).strip().lower() not in ("0", "false", "off")


def _default_fetch(url: str) -> str:
    import httpx  # lazy; already a package dep

    resp = httpx.get(url, timeout=2.0)
    resp.raise_for_status()
    return resp.text


class LazyScraper:
    """Exporter scraper whose endpoint discovery happens on the collector
    thread's FIRST TICK — never inside run(). ``families`` starts empty (no
    tier claims until something actually answered) and the discovery tick
    emits nothing, so the floor covers the first window alone rather than
    mixing two estimators into one window."""

    def __init__(self, fetch=_default_fetch, label_filters: dict | None = None):
        self.families: frozenset = frozenset()
        self._fetch = fetch
        self._filters = label_filters if label_filters is not None else _pod_filters()
        self._inner: OpenMetricsResource | None = None
        self._discovered = False

    def sample(self, ts: float):
        if not self._discovered:
            self._discovered = True
            found = OpenMetricsResource.discover(fetch=self._fetch)
            if found:
                self._inner = OpenMetricsResource(
                    endpoints=found,
                    fetch=self._fetch,
                    pack=MappingPack.default(),
                    label_filters=self._filters,
                )
                self.families = frozenset(
                    _FAMILY_BY_ENDPOINT[name]
                    for name in found
                    if name in _FAMILY_BY_ENDPOINT
                )
            return []
        if self._inner is None:
            return []
        return self._inner.sample(ts)

    def probe(self) -> dict:
        return self._inner.probe() if self._inner else {}


def _pod_filters() -> dict:
    """Attribution for cluster-scoped exporters: filter to our own pod when
    the downward API says who we are."""
    pod = os.environ.get("PROBE_HW_POD_NAME") or os.environ.get("POD_NAME")
    return {"pod": pod} if pod else {}


def build_sources() -> list:
    sources: list = [LazyScraper()]  # tier 2 first: live claims suppress the floor
    system = SystemResource.create()
    if system is not None:
        system.families = frozenset({"system"})
        sources.append(system)
    nvml = NvidiaResource.create()
    if nvml is not None:
        nvml.families = frozenset({"gpu"})
        sources.append(nvml)
    return sources


def _emit_for(client, run_id: str):
    from probe.models import MetricPointIn  # stable import seam over _generated

    def emit(points) -> None:
        body = {
            "points": [
                MetricPointIn(
                    key=p.key,
                    kind="hardware",
                    value=p.value,
                    step_index=p.step,
                    wall_clock=_iso(p.step),
                    dimensions=p.coords or None,
                    agg=p.agg,
                ).model_dump(mode="json", exclude_none=True)
                for p in points
            ]
        }
        # durable=False: raises on failure (the monitor's bounded buffer is
        # the only retry) and never touches the journal.
        client.write("POST", f"/v1/runs/{run_id}/metrics", body, durable=False)

    return emit


def _iso(step: int) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(step * HW_STEP_SECONDS, tz=timezone.utc).isoformat()


def maybe_start(client, run_handle, hw_flag: bool | None):
    """Start the collector for this run if enabled and this process is the
    node leader. Returns the monitor or None. Never raises."""
    try:
        if not hw_enabled(hw_flag):
            return None
        if not elect_leader(run_handle.id):
            return None
        sources = build_sources()
        if not sources:
            return None
        identity = {"host": socket.gethostname()}
        monitor = HwMonitor(
            sources=sources,
            emit=_emit_for(client, run_handle.id),
            identity=identity,
            interval=float(os.environ.get("PROBE_HW_INTERVAL", "15")),
        )
        monitor.start()
        logger.info(
            "probe: hardware metrics on for %s (PROBE_HW=0 or run(hw=False) disables)",
            run_handle.id,
        )
        _spawn_inventory(client, run_handle, sources)
        return monitor
    except Exception:  # noqa: BLE001 — a broken collector must never touch the run
        logger.debug("hw: collector failed to start", exc_info=True)
        return None


def _spawn_inventory(client, run_handle, sources) -> None:
    """probe() can block (NVML init, HTTP) — gather off the run() path."""

    def gather() -> None:
        try:
            inventory: dict = {}
            for src in sources:
                try:
                    inventory.update(src.probe() or {})
                except Exception:  # noqa: BLE001
                    continue
            publish_inventory(
                client,
                run_id=run_handle.id,
                env_ref=getattr(run_handle, "env_ref", None),
                inventory=inventory,
            )
        except Exception:  # noqa: BLE001
            logger.debug("hw: inventory publication failed", exc_info=True)

    threading.Thread(target=gather, name="probe-hw-inventory", daemon=True).start()


def publish_inventory(client, *, run_id: str, env_ref, inventory: dict) -> None:
    """Mint a minimal execution record carrying the hardware inventory — but
    only when the run has no env_ref yet: a real snapshot's record is never
    clobbered (snapshot-side hardware merge is the documented follow-up in
    the snapshot plan)."""
    if not inventory:
        return
    if env_ref:
        logger.debug("hw: run %s already has env_ref; inventory mint skipped", run_id)
        return
    record = client.write(
        "POST", "/v1/execution-records", {"hardware": inventory}, durable=False
    )
    content_hash = (record or {}).get("content_hash")
    if content_hash:
        client.write(
            "PATCH", f"/v1/runs/{run_id}", {"env_ref": content_hash}, durable=False
        )
