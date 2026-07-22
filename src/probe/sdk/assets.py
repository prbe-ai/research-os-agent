"""Versioned research-asset registry client (fold #5).

Maps onto the real Probe Research surface: a named, tenant-unique registry (`assets`)
plus zero-copy version pins (`asset_versions`). A version copies an artifact's
content_hash + r2:// uri + size, so promotion is a naming operation, not a copy.

The aspirational resolve/materialize/fork/propose/promote-candidate surface was
dropped: the backend rejected promotion tiers and ships only register + version
(see Probe Research `docs/DESIGN_DIVERGENCES.md`). ``materialize`` (copy a pinned
asset into a workspace) is deferred until the backend exposes an asset-version
download path.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import errors
from ..models import AssetCreate, AssetVersionCreate

if TYPE_CHECKING:
    from .client import Client
    from .transport import Page


class AssetClient:
    """Register named assets and pin zero-copy versions from artifacts."""

    def __init__(self, client: "Client"):
        self.client = client

    # -- registry ----------------------------------------------------------
    def register(
        self,
        name: str,
        *,
        kind: str = "dataset",
        description: str | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        strict: bool | None = None,
    ) -> dict | None:
        """Create a named asset. 409 if the name already exists in the tenant."""
        model = AssetCreate(
            name=name,
            kind=kind,
            description=description,
            tags=tags or [],
            metadata=metadata or {},
        )
        return self.client.write(
            "POST", "/v1/assets", model.model_dump(mode="json", exclude_none=True), strict=strict
        )

    def get(self, asset_id: str) -> dict:
        return self.client.transport.get(f"/v1/assets/{asset_id}")

    def list(self, **params: Any) -> "Page":
        return self.client.transport.get_page("/v1/assets", params=params or None)

    # -- versions (zero-copy pin) ------------------------------------------
    def add_version(
        self,
        asset_id: str,
        *,
        from_artifact_id: str | None = None,
        content_hash: str | None = None,
        uri: str | None = None,
        size_bytes: int | None = None,
        content_type: str | None = None,
        label: str | None = None,
        meta: dict[str, Any] | None = None,
        strict: bool | None = None,
    ) -> dict | None:
        """Pin a new immutable version. Provide either ``from_artifact_id`` (zero-copy:
        the version copies that artifact's content_hash/uri/size) or an explicit
        ``content_hash`` (+ optional uri/size)."""
        if not from_artifact_id and not content_hash:
            raise ValueError("provide from_artifact_id or content_hash")
        model = AssetVersionCreate(
            from_artifact_id=from_artifact_id,
            content_hash=content_hash,
            uri=uri,
            size_bytes=size_bytes,
            content_type=content_type,
            label=label,
            meta=meta or {},
        )
        return self.client.write(
            "POST",
            f"/v1/assets/{asset_id}/versions",
            model.model_dump(mode="json", exclude_none=True),
            strict=strict,
        )

    def versions(self, asset_id: str) -> list[dict]:
        return self.client.transport.get(f"/v1/assets/{asset_id}/versions")

    # GET /v1/assets caps `limit` at 200 (default 50). Page carries next_cursor but
    # does NOT auto-follow, so a scan has to walk it.
    _PAGE = 200
    _SCAN_PAGE_CAP = 200  # 40k assets; a sanity backstop, not a real bound

    def _find(self, name: str, kind: str | None) -> dict | None:
        """First asset matching name (+kind), scanning EVERY page of the registry."""
        cursor: str | None = None
        for _ in range(self._SCAN_PAGE_CAP):
            params: dict[str, Any] = {"limit": self._PAGE}
            if cursor:
                params["cursor"] = cursor
            page = self.list(**params)
            for asset in page.items:
                if asset.get("name") == name and (kind is None or asset.get("kind") == kind):
                    return asset
            cursor = page.next_cursor
            if not cursor:
                return None
        return None

    # -- read / resolve (used by the read-only MCP) ------------------------
    def resolve(
        self,
        name: str,
        *,
        kind: str | None = None,
        requirement: str | None = None,
        at: str | None = None,
    ) -> dict:
        """Resolve a named asset to its versions (read). The backend has no resolve
        endpoint, so this lists the registry and matches by name (+ kind) client-side.
        Returns an honest ``state: match|no_match``.

        The scan walks EVERY page. It used to be `self.list().items` — one page at the
        backend's default limit of 50 — so asset 51+ resolved to `no_match`. That is
        not a near-miss: `no_match` is exactly what licenses a caller to register a new
        identity (see the manage-research-asset skill), so a big registry silently
        manufactured the duplicate assets the registry exists to prevent."""
        match = self._find(name, kind)
        if match is None:
            return {"state": "no_match", "name": name, "kind": kind, "searched": ["assets"]}
        vers = self.versions(match["id"])
        selected = None
        if requirement:  # exact label/version match if requested
            selected = next(
                (v for v in vers if str(v.get("version")) == requirement or v.get("label") == requirement),
                None,
            )
        elif vers:
            # Pick by the version NUMBER, never by list position: the backend
            # returns versions ORDER BY version DESC, so vers[-1] -- which this
            # used to do, commented "latest" -- was the OLDEST. Anything with two
            # or more versions silently resolved (and materialized) to v1.
            selected = max(vers, key=lambda v: v.get("version") or 0)
        return {
            "state": "match",
            "name": name,
            "kind": match.get("kind"),
            "asset": match,
            "versions": vers,
            "selected": selected,
        }

    def materialize(
        self,
        name: str,
        dest: str,
        *,
        kind: str | None = None,
        requirement: str | None = None,
    ) -> dict:
        """Copy a pinned asset version's bytes into ``dest`` (fold #16 download).

        Resolves the asset by name, picks the selected version (latest, or the one
        matching ``requirement``), streams the artifact it was pinned from via a
        presigned GET, and verifies the bytes against the version's pinned
        ``content_hash`` before returning. Requires a version created from an artifact
        (``source_artifact_id``); a version pinned by ``content_hash`` alone has no
        downloadable object. Returns ``{dest, artifact_id, version, sha256}``; raises
        (and deletes ``dest``) on a hash mismatch."""
        resolved = self.resolve(name, kind=kind, requirement=requirement)
        if resolved["state"] != "match":
            raise errors.NotFoundError(f"asset {name!r} not found")
        version = resolved.get("selected")
        if not version:
            raise errors.NotFoundError(f"asset {name!r} has no versions")
        source_artifact_id = version.get("source_artifact_id")
        if not source_artifact_id:
            raise ValueError(
                f"asset version {version.get('version')} was pinned by content_hash only; "
                "materialize needs a version created from an artifact (from_artifact_id)"
            )
        # Stream to disk (an eval set or checkpoint can be large) and hash as it lands,
        # then verify against the version's pinned content_hash. A silently corrupt or
        # tampered download is exactly what a materialize-as-verification step must
        # catch; writing the bytes without checking them made that impossible.
        result = self.client.download_artifact_to(source_artifact_id, dest)
        expected = version.get("content_hash")
        if expected and result["sha256"] != expected:
            Path(dest).unlink(missing_ok=True)
            raise errors.TransportError(
                f"asset {name!r} v{version.get('version')} content_hash mismatch: "
                f"expected {expected}, got {result['sha256']} (deleted {dest})"
            )
        return {
            "dest": str(dest),
            "artifact_id": source_artifact_id,
            "version": version.get("version"),
            "sha256": result["sha256"],
        }
