"""Stable public alias for the generated wire models.

Import request/response types from here, never from ``ros._generated.models``
directly. The generated module is a build artifact (see ``scripts/gen_models.py``);
this seam means a change to how it is generated is a one-line update, not a
sweep across the SDK.

When the backend contract moves: refresh ``schema/openapi.json``
(``scripts/dump_openapi.py``) and run ``make gen-models``. If a field the SDK
references was renamed or removed, the import or attribute use below fails fast,
that is the drift signal working as intended.

The ``/ingest/v1/runs`` body (``IngestRunRequest`` and its nested ``IngestRun`` /
``IngestArtifact``) is now declared in the backend schema, so the passive push is
generated and validated like every other write path.
"""

from __future__ import annotations

from ._generated.models import (
    ArtifactCreate,
    ExperimentCreate,
    IngestArtifact,
    IngestRun,
    IngestRunRequest,
    MetricBatch,
    MetricPointIn,
    ParentRelation,
    ProjectCreate,
    RunCreate,
    RunOut,
    RunPatch,
    RunStatus,
    SpanBatch,
    SpanCreate,
    StepCreate,
)

__all__ = [
    "ArtifactCreate",
    "ExperimentCreate",
    "IngestArtifact",
    "IngestRun",
    "IngestRunRequest",
    "MetricBatch",
    "MetricPointIn",
    "ParentRelation",
    "ProjectCreate",
    "RunCreate",
    "RunOut",
    "RunPatch",
    "RunStatus",
    "SpanBatch",
    "SpanCreate",
    "StepCreate",
]
