#!/usr/bin/env python
"""Snapshot research-os's OpenAPI schema into ``schema/openapi.json``.

The generated client models (``scripts/gen_models.py``) are built from this file,
so refresh it whenever the backend contract changes.

Point it at a checkout of research-os that is importable (its deps installed):

    RESEARCH_OS=/path/to/research-os python scripts/dump_openapi.py

It imports ``app.main:app`` and dumps ``app.openapi()``. No database is needed;
config has local-dev defaults. If research-os is not importable here, run this
inside that repo's environment instead and copy the JSON to ``schema/openapi.json``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "schema" / "openapi.json"


def main() -> int:
    research_os = os.environ.get("RESEARCH_OS")
    if research_os:
        sys.path.insert(0, research_os)
    try:
        from app.main import app  # type: ignore
    except Exception as exc:  # noqa: BLE001
        print(
            "could not import research-os `app.main`. Set RESEARCH_OS=/path/to/research-os "
            f"and install its deps, or run this in that repo's venv.\n  {exc}",
            file=sys.stderr,
        )
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    spec = app.openapi()
    OUT.write_text(json.dumps(spec, indent=2, sort_keys=True))
    paths = len(spec.get("paths", {}))
    schemas = len(spec.get("components", {}).get("schemas", {}))
    print(f"wrote {OUT.relative_to(ROOT)} ({paths} paths, {schemas} schemas)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
