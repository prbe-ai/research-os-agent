"""Client-side mirror of the server's canonical tag form (CONTRACT.md "tags").

The server normalizes every tag write to lowercase-kebab: strip, casefold,
inner whitespace -> '-', dedupe preserving first occurrence. The client
re-implements ONLY that canonicalization — never the caps (the server owns
those 422s) — so it can compare its own inputs against server rows: the
``tags=`` list-filter guard and the CLI's read-modify-write ``tag`` verb both
need "Baseline" to equal "baseline"."""

from __future__ import annotations

from typing import Iterable


def canonical_tags(tags: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        tag = "-".join(str(raw).casefold().split())
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out
