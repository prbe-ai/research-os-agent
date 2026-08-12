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
    TRANSCRIPTS = "transcripts"


class ToolCorpus(StrEnum):
    """``search_knowledge``'s `search_in` vocabulary (the agent-facing side).

    NOT the backend's `corpus` values. Three of these coincide with a
    BackendCorpus member and one (`documents`) does not -- see
    ``_SEARCH_IN_TO_BACKEND`` in service.py. The parameter was called `corpora`
    until it was renamed for exactly that reason: the plural read as `corpus` +
    s, and the identity mappings confirmed the misreading on whichever value a
    caller tried first."""

    # One value, not the former assets/procedures pair: both mapped to the same
    # backend corpus, so narrowing to one never excluded the other. The index has
    # a single bucket for these (IndexDocType.WORKSPACE_FILE), so the split was a
    # distinction the data model cannot make.
    FILES = "files"
    DOCUMENTS = "documents"
    TRANSCRIPTS = "transcripts"
    EXPERIMENTS = "experiments"


class EntityType(StrEnum):
    """Entity types across exact hits, semantic refs, and tool results."""

    PROJECT = "project"
    EXPERIMENT = "experiment"
    # Reached by NAME as a ref (`artifact:<name>`), because the reuse check asks
    # "does an official X already exist" and you have the name, not an id -- and
    # because no `GET /v1/artifacts/{id}` read route exists at all. This absorbed
    # the retired `asset` member: #143 folded every asset into an artifact keeping
    # its id, so one noun now covers both.
    ARTIFACT = "artifact"
    RUN = "run"
    GROUP = "group"  # a sweep/ensemble: an experiment-shaped noun, reached by ref
    FILE = "file"
    DOCUMENT = "document"  # a semantic hit whose ref is null
    # The team wiki (research-os 0098). A SINGLETON, and the only member here
    # whose ref carries no value: `ref="wiki"`, never `wiki:<id>`. There is one
    # document per tenant and the credential names the tenant, so an id would
    # imply a second one exists and would need a 404 for an id nobody can
    # construct -- the same reasoning that keeps `/v1/wiki` free of a path
    # parameter on the HTTP side.
    WIKI = "wiki"


class CollapseMode(StrEnum):
    """``search_knowledge(collapse=...)`` — the result-dedupe vocabulary.

    One member today, and deliberately its OWN type rather than a reuse of
    ``EntityType.EXPERIMENT``: those strings coincide, but "dedupe by experiment"
    and "this row IS an experiment" are different ideas, and typing the parameter
    as EntityType would advertise `project`/`run`/`artifact` as collapse modes
    that do not exist.

    `null` (skip the dedupe) is expressed by the parameter being optional, not by
    a member here -- an enum member spelled "none" would be a second way to say
    the same thing.
    """

    EXPERIMENT = "experiment"


class View(StrEnum):
    """``research_get(view=...)`` — the progressive-disclosure seam.

    Each view is a genuinely different, purpose-shaped payload, and which views
    exist depends on the entity kind (see ``service._VIEWS``). This is the thin
    harness: capability lives in this parameter, not in extra tools, so reading a
    trajectory does not cost a ``research_get_spans`` entrypoint.
    """

    CARD = "card"  # the cheap identity/status glance (the default)
    # On the wiki: every page's type/slug/title/updated_at, no bodies. The
    # wiki's own table of contents -- the front page is prose ABOUT the wiki,
    # this is the machine-readable list of what is in it.
    PAGES = "pages"
    TRAJECTORY = "trajectory"  # the spans themselves, not span_type COUNTS
    METRICS = "metrics"  # series summaries; filters.key drills to raw points
    ARTIFACTS = "artifacts"  # the artifact list
    REPRODUCE = "reproduce"  # hypothesis + env_ref resolved + config
    HANDOFF = "handoff"  # everything a new session needs to continue
    # On a run: BOTH relations — `run_ancestry` (the parent_run_id fork/retry
    # chain) and `edges` (artifact / asset-version provenance). On an experiment:
    # its edges. The run view used to be the ancestry walk alone, which reported
    # empty lineage for runs that had plainly produced artifacts.
    LINEAGE = "lineage"
    EVENTS = "events"  # the append-only lifecycle log
    NOTES = "notes"  # a project's free-text markdown notes
    SUMMARY = "summary"  # a project's dashboard-visible editable Markdown suffix
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


class BackendWikiState(StrEnum):
    """`wiki.state` in the GET /v1/browse response (research-os 0098).

    Its own type rather than a reuse of BackendSearchState above, whose members
    are `ok|partial`: the wiki excerpt is never PARTIAL -- it is a bounded glance
    by construction, so "some of it arrived" is its normal, complete state. What
    it can be instead is UNAVAILABLE, meaning the read failed and this response
    is in no position to say whether a document exists. Sharing one enum would
    advertise `partial` here and `unavailable` there, neither of which the
    respective producer ever emits.
    """

    OK = "ok"
    UNAVAILABLE = "unavailable"


class EnvelopeState(StrEnum):
    """`completeness.state` in the MCP tool envelope."""

    COMPLETE = "complete"
    PARTIAL = "partial"
    # The query was answered in full and the answer is "nothing satisfies that
    # constraint" -- distinct from COMPLETE-with-empty-rows, and load-bearing on
    # the reuse check: "this artifact exists, no version meets your requirement"
    # must NOT read the same as "no such artifact". The first says pin a new
    # version of the SAME identity; the second licenses a new identity. The tool
    # description has promised this state since the asset registry shipped; it was
    # never actually emitted until artifact:<name> replaced that route.
    NO_MATCH = "no_match"


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
    # GET /v1/browse: enumerate structure without a query. Probed separately
    # from UNIFIED_SEARCH -- they shipped in different releases, so a backend
    # can have one and not the other.
    STRUCTURED_BROWSE = "structured_browse"
    UNIFIED_SEARCH = "unified_search"
    # Server-side project scoping. Probed by ECHO rather than assumed: a backend
    # that predates it accepts the unknown `project_id` body field, ignores it,
    # and answers tenant-wide with state="complete".
    PROJECT_SCOPED_SEARCH = "project_scoped_search"
    SEMANTIC_SEARCH = "semantic_search"
    KB_DOCUMENTS = "kb_documents"
    PORTABLE_SNAPSHOTS = "portable_snapshots"
    MANAGED_ARTIFACT_UPLOAD = "managed_artifact_upload"


class MissingMarker(StrEnum):
    """`completeness.missing` markers emitted by the read tools."""

    # research_browse — the backend predates GET /v1/browse. Emitted INSTEAD of
    # an empty tree: "nothing exists" and "this server cannot tell you what
    # exists" are opposite claims, and the first would stop an agent looking.
    STRUCTURED_BROWSE = "structured_browse"
    # research_search
    EXACT_SEARCH = "exact_search"
    SEMANTIC_SEARCH = "semantic_search"
    KB_VALUES = "kb_values"
    # The BACKEND trimmed the response onto its byte budget (dropped chunks or
    # whole results). Distinct from truncated_by_token_budget, which is this
    # tool's own row budget: one is the server shrinking the payload, the other
    # is us. Either way an absent document is not evidence of absence.
    TRUNCATED_BY_RESPONSE_BUDGET = "truncated_by_response_budget"
    # browse_research — the backend could not read the team wiki, so the
    # orientation excerpt is absent from a response that otherwise carries the
    # whole tree. Emitted ONLY for the backend's state="unavailable", never for
    # an absent `wiki` field: a tenant with no document, a scoped browse, and a
    # server that predates the wiki all legitimately carry nothing, and marking
    # those partial would put a permanent "degraded" flag on the most-used read
    # in the product. The distinction is the entire reason the wire shape spells
    # this as a state rather than a nullable field (app/browse/schemas.py).
    TEAM_WIKI = "team_wiki"
    # research_get
    TRUNCATED_BY_TOKEN_BUDGET = "truncated_by_token_budget"
    TOKEN_BUDGET_EXCEEDED = "token_budget_exceeded"
    EXECUTION_RECORD = "execution_record"
    EXPERIMENT = "experiment"  # a run whose experiment could not be read
    # A backend `limit` ceiling was reached, so rows past it are unreachable. At the
    # ceiling the lookahead row cannot be fetched (limit == want), so `more_beyond`
    # is False by construction and THIS is the only remaining signal that the agent
    # has not seen everything. Every _bounded consumer must emit its marker.
    SPANS_BEYOND_BACKEND_LIMIT = "spans_beyond_backend_limit"
    METRIC_POINTS_BEYOND_BACKEND_LIMIT = "metric_points_beyond_backend_limit"
    # GET /v1/runs/{ref}/bundle caps its artifact list at 200 server-side while
    # `artifact_total` reports the true count, and there is no offset to page it.
    # So handoff cannot show the rest: it says so, and view="artifacts" (which reads
    # the uncapped route) is where the full list lives.
    ARTIFACTS_BEYOND_BUNDLE_LIMIT = "artifacts_beyond_bundle_limit"
    # Same shape as the marker above, one layer out. GET /v1/wiki/versions pages by
    # `before_version` -- a KEYSET cursor -- while get_entity's cursor is an offset
    # into the rows a view returned. The two cannot be the same number, so the view
    # serves the newest page and says plainly that older revisions exist. They are
    # reachable, just not from here: `probe wiki versions --before-version <n>`.
    # Emitted only when the backend actually reported more (`next_before_version`),
    # never inferred from a full page.
    WIKI_VERSIONS_BEYOND_PAGE = "wiki_versions_beyond_page"
    # get_metrics_grouped / export_metric_points — the read was cut at the tool's
    # own row bound. Unlike the *_beyond_backend_limit markers the rest IS
    # reachable: `next_cursor` carries the resume position (pass it back as
    # `step_from` for grouped, `after_id` for export).
    ROWS_BEYOND_PAGE_BOUND = "rows_beyond_page_bound"


class ChannelError(StrEnum):
    """Client-side per-channel error markers (backend errors pass through as-is)."""

    MALFORMED_RESPONSE = "malformed_response"
    PROJECT_SCOPE_UNSUPPORTED = "project_scope_unsupported"
