"""Client-side mirror of the server's canonical tag form (CONTRACT.md "tags").

The server normalizes every tag write to lowercase-kebab: NFC, control chars
(Cc) become separators, invisible format chars (Cf — zero-width, BOM) vanish,
casefold, inner whitespace -> '-', dedupe preserving first occurrence. The
client re-implements ONLY that canonicalization — never the caps (the server
owns those 422s) — so it can compare its own inputs against server rows: the
``tags=`` list-filter guard, the tags-write verification, and the CLI's
read-modify-write ``tag`` verb all need "Baseline" to equal "baseline".

MIRROR of ``_canonical`` in research-os ``app/core/tags.py`` — change the two
together or the guard and no-op detection drift (tests/test_tags.py pins the
shared vectors).
"""

from __future__ import annotations

import unicodedata
from typing import Iterable


def canonical_tags(tags: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in tags:
        cleaned: list[str] = []
        for ch in unicodedata.normalize("NFC", str(raw)):
            cat = unicodedata.category(ch)
            if cat == "Cc":
                cleaned.append(" ")
            elif cat != "Cf":
                cleaned.append(ch)
        tag = "-".join("".join(cleaned).casefold().split())
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out
