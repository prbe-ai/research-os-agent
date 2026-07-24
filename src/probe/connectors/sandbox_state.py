"""Host-side helpers for the ``probe.sandbox-state/1`` bundle contract.

The in-sandbox half is ``probe-sandbox-snapshot`` (a static Go binary shipped
as package data under ``_bin/``); this module is everything the consuming
bridge needs on the host: locate the right binary, parse the integrity
trailer the binary prints to stdout, sort manifests (the binary emits walk
order to keep container memory flat), and author the final bundle with
``meta.json`` written atomically last so its presence marks completeness.

Design doc: ``docs/2026-07-23-sandbox-state-capture.md``.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from typing import Any

SCHEMA = "probe.sandbox-state/1"
TRAILER_SCHEMA = "probe.sandbox-snapshot-trailer/1"
TRAILER_PREFIX = "PSBX1 "
BUNDLE_DIRNAME = "probe-sandbox-state"
TOOL_NAME = "probe-sandbox-snapshot"
TOOL_VERSION = "0.1.0"

BEGIN_MANIFEST = "begin-manifest.jsonl.gz"
END_MANIFEST = "end-manifest.jsonl.gz"
END_DELTA = "end-delta.tar.gz"

_MACHINE_TO_ARCH = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def machine_to_arch(machine: str) -> str | None:
    """Map ``uname -m`` output to a shipped binary architecture."""
    return _MACHINE_TO_ARCH.get(machine.strip().lower())


def snapshot_binary_path(arch: str) -> Path:
    """Path to the static snapshot binary for *arch* (``amd64``/``arm64``).

    ``PROBE_SANDBOX_SNAPSHOT_BIN`` overrides the packaged binary (local
    development, unreleased builds).
    """
    override = os.getenv("PROBE_SANDBOX_SNAPSHOT_BIN")
    if override:
        path = Path(override)
        if not path.is_file():
            raise FileNotFoundError(f"PROBE_SANDBOX_SNAPSHOT_BIN={override} does not exist")
        return path
    if arch not in {"amd64", "arm64"}:
        raise ValueError(f"unsupported sandbox architecture {arch!r}")
    packaged = resources.files("probe.connectors") / "_bin" / f"sandbox-snapshot-linux-{arch}"
    with resources.as_file(packaged) as concrete:
        if not concrete.is_file():
            raise FileNotFoundError(
                f"snapshot binary missing from package data: {concrete}; "
                "reinstall probe-research >= 0.9 or set PROBE_SANDBOX_SNAPSHOT_BIN"
            )
        return Path(concrete)


def parse_trailer(stdout: str) -> dict[str, Any]:
    """Extract the last ``PSBX1`` trailer from a snapshot exec's stdout.

    The trailer is the integrity side-channel: it never round-trips through
    the (agent-writable) container filesystem, so hostile output on other
    lines is ignored rather than trusted.
    """
    line = None
    for candidate in stdout.splitlines():
        if candidate.startswith(TRAILER_PREFIX):
            line = candidate[len(TRAILER_PREFIX):]
    if line is None:
        raise ValueError("snapshot produced no PSBX1 trailer")
    trailer = json.loads(line)
    if not isinstance(trailer, dict) or trailer.get("schema") != TRAILER_SCHEMA:
        raise ValueError(f"unexpected trailer schema: {trailer!r}")
    return trailer


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sort_manifest(source: Path, destination: Path) -> int:
    """Rewrite a JSONL.gz manifest sorted bytewise by path; returns entries.

    Runs host-side so the in-container scan never holds a sort buffer. Lines
    are kept verbatim (no re-encoding) — only their order changes, so the
    binary's own field encoding survives untouched.
    """
    entries: list[tuple[bytes, bytes]] = []
    with gzip.open(source, "rb") as handle:
        for raw in handle:
            line = raw.rstrip(b"\n")
            if not line:
                continue
            record = json.loads(line)
            entries.append((str(record["p"]).encode("utf-8", "surrogatepass"), line))
    entries.sort(key=lambda item: item[0])
    tmp = destination.with_name(destination.name + ".tmp")
    with gzip.open(tmp, "wb") as handle:
        for _, line in entries:
            handle.write(line + b"\n")
    os.replace(tmp, destination)
    return len(entries)


def build_meta(
    *,
    begin_trailer: Mapping[str, Any] | None,
    end_trailer: Mapping[str, Any] | None,
    status: Mapping[str, Any],
    begin_at: str | None,
    end_at: str | None,
    arch: str | None,
    integrity: Mapping[str, bool],
    errors: list[str],
) -> dict[str, Any]:
    """Assemble host-authored ``meta.json`` content from the phase trailers."""
    end_stats = dict((end_trailer or {}).get("stats") or {})
    begin_stats = dict((begin_trailer or {}).get("stats") or {})
    summary = {
        "begin_entries": begin_stats.get("entries"),
        "begin_files": begin_stats.get("files_scanned"),
        "added": end_stats.get("added"),
        "modified": end_stats.get("modified"),
        "deleted": end_stats.get("deleted"),
    }
    limits = {
        "truncated": bool(
            (begin_trailer or {}).get("truncated") or (end_trailer or {}).get("truncated")
        ),
        "dropped": list((end_trailer or {}).get("dropped") or []),
        "dropped_count": (end_trailer or {}).get("dropped_count", 0),
        "delta_budget_bytes": end_stats.get("delta_budget_bytes"),
    }
    scan = {
        "hash_mode": (end_trailer or begin_trailer or {}).get("hash_mode", "fast"),
        "skipped_mounts": sorted(
            set((begin_trailer or {}).get("skipped_mounts") or [])
            | set((end_trailer or {}).get("skipped_mounts") or [])
        ),
        "one_filesystem": True,
    }
    collected_errors = list(errors)
    for trailer in (begin_trailer, end_trailer):
        collected_errors.extend((trailer or {}).get("errors") or [])
    return {
        "schema": SCHEMA,
        "tool": {"name": TOOL_NAME, "version": TOOL_VERSION, "arch": arch},
        "begin_at": begin_at,
        "end_at": end_at,
        "status": dict(status),
        "scan": scan,
        "summary": summary,
        "limits": limits,
        "integrity": dict(integrity),
        "errors": collected_errors,
    }


def write_bundle(
    bundle_dir: Path,
    files: Mapping[str, Path],
    meta: Mapping[str, Any],
) -> None:
    """Materialize the bundle; ``meta.json`` is written atomically LAST.

    A bundle without a valid ``meta.json`` is by definition incomplete, so
    renderers can distinguish "capture failed midway" from "capture done".
    Manifests are sorted on the way in; other files are copied verbatim.
    """
    bundle_dir.mkdir(parents=True, exist_ok=True)
    for name, source in files.items():
        if not source.is_file():
            continue
        destination = bundle_dir / name
        if name in (BEGIN_MANIFEST, END_MANIFEST):
            sort_manifest(source, destination)
        else:
            shutil.copyfile(source, destination)
    tmp = bundle_dir / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, bundle_dir / "meta.json")
