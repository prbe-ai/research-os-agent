"""Stable public alias for the generated wire models.

Import request/response types from here, never from ``probe._generated.models``
directly. The generated module is a build artifact (see ``scripts/gen_models.py``);
this seam means a change to how it is generated is a one-line update, not a
sweep across the SDK.

When the backend contract moves: refresh ``schema/openapi.json``
(``scripts/dump_openapi.py``) and run ``make gen-models``. If a field the SDK
references was renamed or removed, the import or attribute use below fails fast,
that is the drift signal working as intended.

The expression-view models (``MetricViewCreate``/``Spec``/``Patch``/``Out``/``Data``,
``MetricViewPreviewRequest``) and ``DerivedProvenance`` back the read-time view
surface and the derived-metric write; ``probe.expr`` builds specs against them.

The ``/ingest/v1/runs`` body (``IngestRunRequest`` and its nested ``IngestRun`` /
``IngestArtifact``) is now declared in the backend schema, so the passive push is
generated and validated like every other write path.

``WikiWrite`` is the team wiki's version-checked write body (research-os 0098).
Only the WRITE model is re-exported: the read shapes (``WikiPageOut``,
``WikiVersionsOut``, ``TeamWikiExcerpt``) are generated too, but the client hands
reads back as plain dicts like every other read, and importing a response model
nothing validates against would imply a parsing step that does not exist.
"""

from __future__ import annotations

from ._generated.models import (
    AnchorLevel,
    ArtifactCreate,
    ArtifactPinImpact,
    ArtifactVersionCreate,
    ArtifactVersionOut,
    DerivedProvenance,
    DownloadResponse,
    EdgeCreate,
    EventOut,
    ExecutionRecordCreate,
    ExecutionRecordOut,
    ExperimentCreate,
    ExperimentVersionMint,
    ExperimentVersionOut,
    IngestArtifact,
    IngestRun,
    IngestRunRequest,
    LatestScalarsRequest,
    LineageEdgeOut,
    LineageEntityType,
    LineageRelation,
    MetricBatch,
    MetricPointIn,
    MetricViewCreate,
    MetricViewData,
    MetricViewOut,
    MetricViewPatch,
    MetricViewPreviewRequest,
    MetricViewSpec,
    ParentRelation,
    ProjectCreate,
    RunCreate,
    RunDetailOut,
    RunGroupCreate,
    RunGroupOut,
    RunGroupPatch,
    RunOut,
    RunPatch,
    RunStatus,
    Scope,
    SpanBatch,
    SpanCreate,
    StepCreate,
    TokenCreate,
    TokenCreated,
    TokenOut,
    UploadGcRequest,
    UploadGcResult,
    ScopedUploadRequest,
    UploadRequest,
    UploadResponse,
    WikiWrite,
)

__all__ = [
    # The vertically-movable artifact anchors, backing `probe artifact move --to`.
    # Taken from the contract rather than spelled out in the CLI so a level the
    # backend adds arrives with `make regen` instead of by hand.
    "AnchorLevel",
    "ArtifactCreate",
    "ArtifactPinImpact",
    "ArtifactVersionCreate",
    "ArtifactVersionOut",
    "DerivedProvenance",
    "DownloadResponse",
    "EdgeCreate",
    "EventOut",
    "ExecutionRecordCreate",
    "ExecutionRecordOut",
    "ExperimentCreate",
    "ExperimentVersionMint",
    "ExperimentVersionOut",
    "IngestArtifact",
    "IngestRun",
    "IngestRunRequest",
    "LatestScalarsRequest",
    "LineageEdgeOut",
    "LineageEntityType",
    "LineageRelation",
    "MetricBatch",
    "MetricPointIn",
    "MetricViewCreate",
    "MetricViewData",
    "MetricViewOut",
    "MetricViewPatch",
    "MetricViewPreviewRequest",
    "MetricViewSpec",
    "ParentRelation",
    "ProjectCreate",
    "RunCreate",
    "RunDetailOut",
    "RunGroupCreate",
    "RunGroupOut",
    "RunGroupPatch",
    "RunOut",
    "RunPatch",
    "RunStatus",
    "Scope",
    "SpanBatch",
    "SpanCreate",
    "StepCreate",
    "TokenCreate",
    "TokenCreated",
    "TokenOut",
    "UploadGcRequest",
    "UploadGcResult",
    "ScopedUploadRequest",
    "UploadRequest",
    "UploadResponse",
    "WikiWrite",
]
