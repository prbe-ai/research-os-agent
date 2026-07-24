"""The durable-watcher loop, lifted out of ``probe trial watch``.

Poll a drain function until it makes progress, report results, and honor
``--once`` and the non-zero exit on failure. The loop is producer-agnostic: any
``drain() -> {"counts": {...}, "failed": [...]}`` works, so a future
``probe artifact watch`` (or any other staged-bytes producer) reuses this instead
of re-implementing the poll / report / exit dance.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import typer


def watch(
    drain: Callable[[], dict[str, Any]],
    *,
    interval: float,
    once: bool,
    report: Callable[[dict[str, Any]], None],
) -> None:
    """Poll ``drain()`` -> report -> sleep, until ``once``.

    ``drain`` returns a result carrying ``counts.completed`` / ``counts.failed`` and
    a ``failed`` list. Results are reported only when something happened (or on a
    one-shot run, as a deployment smoke check). A one-shot run with failures exits
    non-zero, matching the daemon's contract.
    """
    while True:
        result = drain()
        counts = result["counts"]
        if once or counts["completed"] or counts["failed"]:
            report(result)
        if once:
            if result["failed"]:
                raise typer.Exit(2)
            return
        time.sleep(interval)
