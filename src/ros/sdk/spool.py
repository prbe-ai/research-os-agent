"""The SDK local spool for fail-open, non-blocking writes.

The design invariant (ingestion doc, fail-open): a data write must never block or
crash the researcher's training loop. When a write fails and the caller opted into
fail-open, we append the raw request to an on-disk JSONL queue and return. ``flush``
replays the queue in order later (e.g. at ``run end`` or from ``exp flush``).

This is deliberately dumb: append-only, replay-in-order, stop-on-first-failure so
ordering is preserved. It is not a high-throughput buffer; it is a safety net.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path


def default_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "ros" / "spool"


@dataclass
class SpoolRecord:
    method: str
    path: str
    json_body: dict | None


class Spool:
    def __init__(self, directory: Path | None = None):
        self.dir = directory or default_dir()
        self.file = self.dir / "pending.jsonl"

    def append(self, method: str, path: str, json_body: dict | None) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"method": method, "path": path, "json": json_body})
        with self.file.open("a") as fh:
            fh.write(line + "\n")

    def pending(self) -> list[SpoolRecord]:
        if not self.file.exists():
            return []
        out: list[SpoolRecord] = []
        for line in self.file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out.append(SpoolRecord(rec["method"], rec["path"], rec.get("json")))
        return out

    def flush(self, transport) -> int:
        """Replay pending records in order. Returns the count successfully sent.
        Stops at the first failure and rewrites the queue with the remainder so a
        transient outage does not reorder or drop writes."""
        records = self.pending()
        if not records:
            return 0
        sent = 0
        remaining: list[SpoolRecord] = []
        failed = False
        for rec in records:
            if failed:
                remaining.append(rec)
                continue
            try:
                transport.request(rec.method, rec.path, json_body=rec.json_body)
                sent += 1
            except Exception:  # noqa: BLE001 - keep the rest queued on any failure
                failed = True
                remaining.append(rec)
        if remaining:
            self.file.write_text(
                "\n".join(
                    json.dumps({"method": r.method, "path": r.path, "json": r.json_body})
                    for r in remaining
                )
                + "\n"
            )
        else:
            self.file.unlink(missing_ok=True)
        return sent
