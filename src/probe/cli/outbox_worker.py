"""Runnable shim: the drainer moved to ``probe.sdk.outbox_worker`` so SDK
writers can kick it too (parity F1, docs/2026-08-04-outbox-miles-parity.md).
Workers spawned by older releases exec ``-m probe.cli.outbox_worker``, so this
module path must stay runnable forever.
"""

from __future__ import annotations

import sys

from ..sdk.outbox_worker import maybe_spawn, run

__all__ = ["maybe_spawn", "run"]

if __name__ == "__main__":
    sys.exit(run(sys.argv[1] if len(sys.argv) > 1 else None))
