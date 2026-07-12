"""Versioned research-asset registry client (fold #5).

Maps onto the real research-os surface: a named, tenant-unique registry (`assets`)
plus zero-copy version pins (`asset_versions`). A version copies an artifact's
content_hash + r2:// uri + size, so promotion is a naming operation, not a copy.

The aspirational resolve/materialize/fork/propose/promote-candidate surface was
dropped: the backend rejected promotion tiers and ships only register + version
(see research-os `docs/DESIGN_DIVERGENCES.md`). ``materialize`` (copy a pinned
asset into a workspace) is deferred until the backend exposes an asset-version
download path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

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
        Returns an honest ``state: match|no_match``."""
        assets = self.list().items
        match = next(
            (a for a in assets if a.get("name") == name and (kind is None or a.get("kind") == kind)),
            None,
        )
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
            selected = vers[-1]  # latest
        return {
            "state": "match",
            "name": name,
            "kind": match.get("kind"),
            "asset": match,
            "versions": vers,
            "selected": selected,
        }
