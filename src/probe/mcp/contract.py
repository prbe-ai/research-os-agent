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
    FILE = "file"
    DOCUMENT = "document"  # a semantic hit whose ref is null


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
    """Keys of the capability map embedded in every tool envelope."""

    STRUCTURED_EXPERIMENTS = "structured_experiments"
    UNIFIED_SEARCH = "unified_search"
    SEMANTIC_SEARCH = "semantic_search"
    KB_DOCUMENTS = "kb_documents"
    VERSIONED_ASSETS = "versioned_assets"
    PORTABLE_SNAPSHOTS = "portable_snapshots"
    MANAGED_ARTIFACT_UPLOAD = "managed_artifact_upload"
    PROMOTION_MANIFESTS = "promotion_manifests"


class MissingMarker(StrEnum):
    """`completeness.missing` markers emitted by research_search."""

    EXACT_SEARCH = "exact_search"
    SEMANTIC_SEARCH = "semantic_search"
    KB_CORPORA = "kb_corpora"


class ChannelError(StrEnum):
    """Client-side per-channel error markers (backend errors pass through as-is)."""

    MALFORMED_RESPONSE = "malformed_response"
    PROJECT_SCOPE_UNSUPPORTED = "project_scope_unsupported"
