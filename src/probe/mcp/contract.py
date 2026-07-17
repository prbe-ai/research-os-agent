"""Shared /v1/search + MCP-envelope vocabulary (CONTRACT.md, workspaces+kb fold-in).

Enum-style constants so ``service.py`` and ``source.py`` cannot drift on the wire
strings. ``StrEnum`` members are plain ``str`` at runtime, so they serialize and
compare exactly like the literals they replace.
"""

from __future__ import annotations

from enum import StrEnum


class BackendCorpus(StrEnum):
    """`corpus` values accepted by POST /v1/search."""

    EXPERIMENTS = "experiments"
    FILES = "files"
    GITHUB = "github"


class ToolCorpus(StrEnum):
    """research_search's `corpora` vocabulary (the agent-facing side)."""

    ASSETS = "assets"
    PROCEDURES = "procedures"
    DOCUMENTS = "documents"
    TRANSCRIPTS = "transcripts"
    EXPERIMENTS = "experiments"


class EntityType(StrEnum):
    """Entity types across exact hits, semantic refs, and tool results."""

    PROJECT = "project"
    EXPERIMENT = "experiment"
    ARTIFACT = "artifact"
    RUN = "run"
    GROUP = "group"  # a sweep/ensemble: an experiment-shaped noun, reached by ref
    FILE = "file"
    DOCUMENT = "document"  # a semantic hit whose ref is null


class View(StrEnum):
    """``research_get(view=...)`` — the progressive-disclosure seam.

    Each view is a genuinely different, purpose-shaped payload, and which views
    exist depends on the entity kind (see ``service._VIEWS``). This is the thin
    harness: capability lives in this parameter, not in extra tools, so reading a
    trajectory does not cost a ``research_get_spans`` entrypoint.
    """

    CARD = "card"  # the cheap identity/status glance (the default)
    TRAJECTORY = "trajectory"  # the spans themselves, not span_type COUNTS
    METRICS = "metrics"  # series summaries; filters.key drills to raw points
    ARTIFACTS = "artifacts"  # the artifact list
    REPRODUCE = "reproduce"  # hypothesis + env_ref resolved + config
    HANDOFF = "handoff"  # everything a new session needs to continue
    LINEAGE = "lineage"  # run lineage, or experiment-level edges
    EVENTS = "events"  # the append-only lifecycle log
    GROUPS = "groups"  # sweeps/ensembles under an experiment
    VERSIONS = "versions"  # immutable published manifests


class Channel(StrEnum):
    """Which door produced a result (per-result provenance)."""

    EXACT = "exact"
    SEMANTIC = "semantic"
    KEYWORD = "keyword"  # client-side fallback on pre-/v1/search backends


class MatchMode(StrEnum):
    EXACT = "exact"
    SEMANTIC = "semantic"
    KEYWORD_FALLBACK = "keyword_fallback"


class BackendSearchState(StrEnum):
    """`state` in the POST /v1/search response."""

    OK = "ok"
    PARTIAL = "partial"


class EnvelopeState(StrEnum):
    """`completeness.state` in the MCP tool envelope."""

    COMPLETE = "complete"
    PARTIAL = "partial"


class Capability(StrEnum):
    """Keys of the capability map embedded in every tool envelope: what this
    backend can do, reported for information.

    A key earns its place by describing something the product HAS or could have.
    ``promotion_manifests`` is gone rather than False because promotion tiers were
    deliberately REJECTED (``sdk/client.py``: experiment versions replaced the
    removed run-level promote) — reporting a rejected concept as unavailable
    implies it is coming. ``portable_snapshots`` stays False because it is "not
    yet", not "no": ``sdk/snapshot.py`` captures git/env locally and no backend
    route reads one back.

    A False here must NOT, by itself, make a response partial. ``completeness.missing``
    says what a given response lacks; deriving it from every False flag is what
    pinned every research_context envelope to ``partial`` regardless of what was
    actually returned, which trains agents to ignore the signal entirely.
    """

    STRUCTURED_EXPERIMENTS = "structured_experiments"
    UNIFIED_SEARCH = "unified_search"
    SEMANTIC_SEARCH = "semantic_search"
    KB_DOCUMENTS = "kb_documents"
    VERSIONED_ASSETS = "versioned_assets"
    PORTABLE_SNAPSHOTS = "portable_snapshots"
    MANAGED_ARTIFACT_UPLOAD = "managed_artifact_upload"


class MissingMarker(StrEnum):
    """`completeness.missing` markers emitted by the read tools."""

    # research_search
    EXACT_SEARCH = "exact_search"
    SEMANTIC_SEARCH = "semantic_search"
    KB_CORPORA = "kb_corpora"
    # research_get
    TRUNCATED_BY_TOKEN_BUDGET = "truncated_by_token_budget"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    EXECUTION_RECORD = "execution_record"
    SPANS_BEYOND_BACKEND_LIMIT = "spans_beyond_backend_limit"


class ChannelError(StrEnum):
    """Client-side per-channel error markers (backend errors pass through as-is)."""

    MALFORMED_RESPONSE = "malformed_response"
    PROJECT_SCOPE_UNSUPPORTED = "project_scope_unsupported"
