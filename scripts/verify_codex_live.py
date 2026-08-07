#!/usr/bin/env python3
"""Poll Research OS until a unique Codex capture canary is searchable."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def _search(base_url: str, token: str, marker: str) -> dict:
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/search",
        data=json.dumps({"query": marker, "include_semantic": True, "top_k": 20}).encode(),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "probe-codex-live-verifier/1",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read())


def _codex_hit(body: dict, marker: str) -> dict | None:
    needle = marker.casefold()
    semantic = body.get("semantic") or {}
    for hit in semantic.get("results") or []:
        if hit.get("source_system") != "codex":
            continue
        # Search only indexed source content. Search services often include a
        # generated `why_relevant` explanation which can repeat the query
        # verbatim even when the marker never reached the captured transcript.
        # Matching the whole hit therefore makes a failed canary look live.
        chunks = hit.get("chunks") or []
        content = [hit.get("content")]
        content.extend(chunk.get("content") for chunk in chunks if isinstance(chunk, dict))
        if any(needle in str(value or "").casefold() for value in content):
            return hit
    return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a real Codex session reached the searchable Research OS index."
    )
    parser.add_argument("marker", help="Unique phrase entered in a fresh Codex session")
    parser.add_argument(
        "--api-base-url",
        default=os.environ.get("PROBE_BASE_URL", "https://api.research.prbe.ai"),
    )
    parser.add_argument("--token", default=os.environ.get("PROBE_TOKEN"))
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--interval", type=int, default=10)
    args = parser.parse_args()
    if not args.token:
        parser.error("set PROBE_TOKEN or pass --token (a read-scoped user token)")

    deadline = time.monotonic() + args.timeout
    last_error = "no matching Codex document yet"
    while time.monotonic() < deadline:
        try:
            body = _search(args.api_base_url, args.token, args.marker)
            hit = _codex_hit(body, args.marker)
            if hit is not None:
                print(
                    json.dumps(
                        {
                            "live": True,
                            "source_system": hit["source_system"],
                            "doc_id": hit.get("doc_id"),
                            "title": hit.get("title"),
                        },
                        indent=2,
                    )
                )
                return 0
            if body.get("state") == "partial":
                last_error = f"search is partial: {(body.get('semantic') or {}).get('error')}"
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            last_error = f"search failed: {exc}"
        time.sleep(max(1, args.interval))

    print(f"Codex canary did not become live: {last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
