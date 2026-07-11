"""Versioned research-asset SDK surface.

The client contract is implemented here even though research-os API v3 does not
yet ship the corresponding registry routes. Calls fail with
``CapabilityUnavailable('asset_registry')`` rather than silently encoding an
official asset as ordinary run metadata.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from . import errors

if TYPE_CHECKING:
    from .client import Client


def _fingerprint_path(path: Path) -> tuple[str, int, list[dict[str, Any]] | None]:
    if path.is_file():
        data = path.read_bytes()
        return hashlib.sha256(data).hexdigest(), len(data), None
    if not path.is_dir():
        raise FileNotFoundError(path)
    entries: list[dict[str, Any]] = []
    size = 0
    for item in sorted(p for p in path.rglob("*") if p.is_file()):
        if ".git" in item.relative_to(path).parts:
            continue
        data = item.read_bytes()
        size += len(data)
        entries.append(
            {
                "path": item.relative_to(path).as_posix(),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size_bytes": len(data),
            }
        )
    manifest = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(manifest).hexdigest(), size, entries


class AssetClient:
    """Read/modify reusable assets through the future registry API.

    Resolve is a read operation and is normally used through MCP. Materialize,
    fork, propose, and promote are SDK/CLI writes because they affect the local
    filesystem, upload bytes, or mutate registry/lineage state.
    """

    def __init__(self, client: "Client"):
        self.client = client

    def resolve(
        self,
        name: str,
        *,
        kind: str | None = None,
        requirement: str | None = None,
        at: str | None = None,
    ) -> dict:
        params = {"name": name}
        if kind:
            params["kind"] = kind
        if requirement:
            params["requirement"] = requirement
        if at:
            params["at"] = at
        return self._read("/v1/assets/resolve", params=params)

    def materialize(
        self,
        asset_ref: str,
        *,
        run_id: str,
        destination: str,
        mode: str = "readonly",
        strict: bool | None = None,
    ) -> dict | None:
        return self._write(
            "POST",
            f"/v1/assets/{quote(asset_ref, safe='')}/materializations",
            {"run_id": run_id, "destination": destination, "mode": mode},
            strict=strict,
        )

    def fork(
        self,
        asset_ref: str,
        *,
        run_id: str,
        destination: str,
        reason: str,
        strict: bool | None = None,
    ) -> dict | None:
        return self._write(
            "POST",
            f"/v1/assets/{quote(asset_ref, safe='')}/forks",
            {"run_id": run_id, "destination": destination, "reason": reason},
            strict=strict,
        )

    def propose(
        self,
        source: str,
        *,
        run_id: str,
        kind: str,
        canonical_name: str,
        base_ref: str | None = None,
        contract: dict[str, Any] | None = None,
        validation: dict[str, Any] | None = None,
        transform: dict[str, Any] | None = None,
        nearest_matches: list[dict[str, Any]] | None = None,
        new_identity_reason: str | None = None,
        strict: bool | None = None,
    ) -> dict | None:
        path = Path(source).expanduser().resolve()
        content_hash, size_bytes, manifest = _fingerprint_path(path)
        if base_ref is None and not new_identity_reason:
            raise ValueError("new_identity_reason is required when base_ref is absent")
        if kind == "dataset" and not transform:
            raise ValueError(
                "dataset proposals require transform metadata with pinned inputs, code, and parameters"
            )
        body = {
            "run_id": run_id,
            "kind": kind,
            "canonical_name": canonical_name,
            "base_ref": base_ref,
            "contract": contract or {},
            "validation": validation or {},
            "transform": transform,
            "nearest_matches": nearest_matches or [],
            "new_identity_reason": new_identity_reason,
            "content": {
                "local_path": str(path),
                "content_hash": content_hash,
                "size_bytes": size_bytes,
                "manifest": manifest,
                "portable": False,
            },
        }
        return self._write("POST", "/v1/assets/proposals", body, strict=strict)

    def promote(
        self,
        candidate_ref: str,
        *,
        approval: str,
        strict: bool = True,
    ) -> dict | None:
        if not approval.strip():
            raise ValueError("explicit approval text is required")
        return self._write(
            "POST",
            f"/v1/assets/{quote(candidate_ref, safe='')}/promote",
            {"approval": approval.strip()},
            strict=strict,
        )

    def _read(self, path: str, *, params: dict | None = None) -> dict:
        try:
            return self.client.transport.get(path, params=params)
        except errors.NotFoundError as exc:
            raise errors.CapabilityUnavailable(
                "asset_registry",
                "the deployed research-os API has no versioned asset registry yet",
            ) from exc

    def _write(
        self,
        method: str,
        path: str,
        body: dict,
        *,
        strict: bool | None,
    ) -> dict | None:
        try:
            return self.client.write(method, path, body, strict=True)
        except errors.NotFoundError as exc:
            raise errors.CapabilityUnavailable(
                "asset_registry",
                "the deployed research-os API has no versioned asset registry yet",
            ) from exc
