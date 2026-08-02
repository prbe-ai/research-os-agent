# TODOs

Deferred work for the Probe Research agent (CLI, SDK, plugins). Each item states what
it is, why it matters, and roughly what it takes.

---

## P3 — CI coverage for the filesystem-clone branch

### macOS CI lane for `try_clone` / `snapshot_file` tests

**What:** A scoped GitHub Actions `macos-latest` job running just
`tests/test_outbox.py` (and the harbor stager tests), so the APFS `clonefile`
branch of `sdk/durable.py:try_clone` gets CI coverage.

**Why:** Accepted risk from the 2026-07-29 async-outbox eng review (decision
10B): ubuntu runners use ext4, which has no reflink, so CI only ever executes
the byte-copy fallback. The clone branch is the one every developer Mac (and
any xfs/btrfs PVC) actually hits; today it is covered only by local test runs.
The tests already `skipif` on unsupported filesystems — the lane just gives
them a place to run.

**Effort:** S (~2 min of macOS runner time per push if scoped to two files)
**Priority:** P3
**Depends on:** nothing — can land any time after the async outbox merges.

---

## P2 — coding-agent coverage

### Transcript capture for Cursor and Codex

**What:** A capture plugin for Cursor and for Codex, equivalent to
`plugins/probe-research-tap` for Claude Code.

**Why:** `sdk/agent_session.py` `detect_agent()` already detects all three agents, and both
Cursor (`CURSOR_TRACE_ID`) and Codex (`CODEX_THREAD_ID`) expose a session identifier. But
nothing ships their transcripts for Research OS tenants, so those identifiers resolve to
nothing. Session↔run linking therefore deliberately records Claude Code only, gated behind
a capability table, so the id map never holds a permanently dead key.

**Context (2026-07-26, from the session-linking eng review):** the engine half partly
exists already — `prbe-knowledge` has a Codex transcript handler
(`kb/handlers/claude_code.py`, `_doc_id_prefix = "codex"`) alongside the Claude Code one.
What is missing on the Research OS side is the host-side capture plugin: transcript tail,
spool, device pairing, secret-scan gate. Once one exists, enabling that agent is a
one-line flip in the capability table, because the client already detects it and the
header/stamping path is agent-agnostic.

Before building: confirm the team actually uses these agents for research work. Neither is
confirmed today, which is why this is deferred rather than scoped.

**Effort:** L (Cursor), M (Codex — engine handler already exists)
**Priority:** P2
**Depends on:** session↔run linking landing first (establishes the capability table)

---

## P2 — SDK ergonomics follow-ups (from the pre-landing review of `feat/sdk-ergonomics`)

Findings raised by the review specialists that were judged real but not blocking.
The critical ones from that review were fixed on the branch; these were not.

### `_UNSET` is private, untyped, and now load-bearing in three signatures

**What:** `run.py` uses `_UNSET: Any = object()` as the default for `log(step=)`,
`span(parent_span_id=)` and `span(started_at=)`, while still annotating them
`int | None` / `str | None`. The annotation is a lie, `help()` renders
`step=<object object at 0x…>`, and no wrapper can express "omitted" — so
`integrations/miles.py` `ProbeBackend.log(step=None)` and `sdk/fluent.py` can never
reach the auto-increment path.

**Why:** The three-way contract (omitted / explicit / None) is the whole design, and
it is currently inexpressible by anyone outside `run.py`. Third-party wrappers
mirroring the 0.14.x signature will pin the old semantics permanently.

**Takes:** A typed public sentinel — `class UnsetType: ...`, `UNSET = UnsetType()`,
exported from `probe` — plus `int | None | UnsetType` annotations and a pass over the
in-repo wrappers.

### `SpanHandle` is single-use as a context manager

**What:** It stores one `_token`, so re-entering the same handle (a loop, or nesting
it in itself) makes the inner `__exit__` consume the token and the outer one skip its
`ContextVar.reset` — leaving `_current_span` on a closed span, so later siblings
misparent. Entering and exiting on different threads makes `reset()` raise.

**Takes:** A token stack, or an explicit "handle already entered" error, plus a
docstring line saying a handle is single-use.

### `fluent` exit hooks are install-only

**What:** `finish()` never restores `sys.excepthook`, never `atexit.unregister`s, and
never clears `_hooks_installed`, so probe's hook stays wired for the life of any
long-lived host (notebook, server, pytest session) long after the run closed.
`tests/test_fluent.py` therefore leaks a patched excepthook into every later test.

**Takes:** A `_reset_exit_hooks()` called from `finish()` when the last binding goes,
and symmetric fixture teardown.

### `_exit_status` only sees the main thread

**What:** It is written from `sys.excepthook`, which is not called for worker-thread
exceptions (`threading.excepthook`) nor by IPython. `_finish_at_exit` therefore records
`completed` for a run whose thread died, while its docstring claims the excepthook
"is what tells the two apart".

**Takes:** Hook `threading.excepthook` too, and soften the docstring to say main-thread
best-effort.

### `compare(run_ids=...)` is N+1

**What:** One `get_run()` per id purely to build column labels, before the batched
series query — while the module docstring sells "every series in one round trip"
against exactly that loop.

**Takes:** A single filtered `list_runs`, or label from the series rows.

### `aligned()` merges series that differ only by `dimensions`

**What:** Buckets by label alone, so two `SeriesResult` rows for one run and key that
differ by dimensions (the per-device/per-host shape `log_hw` produces) overwrite each
other point-for-point. Duplicate LABELS are now disambiguated; duplicate dimensions
are not.

**Takes:** Include `dimensions` in the bucket key and surface it in the column name.

### Fake backend does not enforce the bounds the real API declares

**What:** `tests/conftest.py`'s `POST /v1/series/query` ignores `run_ids` maxItems 50 /
minItems 1 and `max_points` 2..100000 from `schema/openapi.json`, so an SDK regression
that stopped batching would only be caught by one count assertion.

**Takes:** 422 on out-of-range values, the way the fake already does for
`ScopedUploadRequest` extras.

## P2 — backend cold-start latency (research-os, cross-repo)

### First post-deploy search takes ~55s

**What:** Profile why the first `search_knowledge` after a research-os backend
deploy took ~55s (2026-07-30 incident, 05:54:59–05:55:54 UTC — the call that
wedged an MCP pod into a liveness kill). Likely retrieval-service cold caches
or lazy model/embedding load on the first request.

**Why:** The MCP now survives slow backend calls (thread offload + probe
tuning, PR from the 2026-07-30 rollout-hardening plan), but users still wait
nearly a minute for the first post-deploy search. The slowness itself lives in
research-os, not this repo — this entry is the cross-repo pointer so the
context survives.

**Context:** Incident evidence in the plan file
(`~/.gstack/projects/prbe-ai-research-os-agent/2026-07-30-mcp-rollout-hardening-plan.md`)
and the killed pod's log (healthz silent from the moment the CallToolRequest
started). Start at the retrieval service's first-request path.

**Effort:** M (profiling + a warm-up or eager-load fix in research-os)
**Priority:** P2
**Depends on:** nothing — independent of the MCP hardening PR.

---

## P3 — MCP CPU limit evaluation (trigger-conditioned)

### Measure CFS throttling under threaded concurrency, then size the limit

**What:** After the thread-offload MCP ships and has ~a week of real traffic,
read throttling counters (`kubectl exec … cat /sys/fs/cgroup/cpu.stat` —
`nr_throttled`/`throttled_usec`; no metrics-server on this cluster) on the
`research-os-mcp` pods, check the `research` namespace ResourceQuota CPU
headroom, and decide whether the 500m limit should be raised or dropped.

**Why:** Up to 40 worker threads now share half a core; large tool-result
serialization could throttle and stretch latencies invisibly. The
2026-07-30 eng review deliberately deferred this rather than guess
(plan NOT-in-scope) — this entry carries the trigger so the revisit happens.

**Trigger:** any MCP latency complaint, or nonzero `throttled_usec` growth.

**Effort:** S (measurement) + a one-line manifest change if warranted
**Priority:** P3
**Depends on:** the rollout-hardening PR shipping first.

---

## P2 — MCP flood hardening (from the 2026-07-30 pre-landing review)

### Bound the paths the admission limiter does not cover

**What:** Three related guards for the hosted MCP under request floods:
1. **Pre-auth verification fan-out** (pre-existing, predates the offload):
   every unique bogus bearer opens an AsyncClient + `/v1/me` round trip
   before admission applies; the rejection cache is useless under token
   rotation. Add a small concurrency bound/rate limit around
   `_upstream_rejects`.
2. **Per-tenant fairness:** one valid token can occupy all worker slots;
   shed logs now carry a token fingerprint (sha256/8) so a noisy tenant is
   attributable — add a per-token cap when attribution shows it matters.
3. **Queue depth:** admission bounds queue TIME (20s) not DEPTH; consider
   uvicorn `limit_concurrency` so an edge 503 caps waiter memory.

**Why:** The 2026-07-30 hardening bounds worker occupancy per pod; these are
the remaining flood paths, all currently gated only by upstream capacity.
Codex flagged #1 as its top finding; it is pre-existing and auth-layer, so it
rode as a TODO rather than blocking the rollout fix.

**Effort:** S each; #1 first (unauthenticated surface)
**Priority:** P2
**Depends on:** nothing.

---

## P3 — capability-cache verdict divergence during mixed-version rollouts

### Search support can read False for up to one recheck window

**What:** `source.py` tri-state capability verdicts are last-writer-wins under
the now-truly-concurrent tool threads; during a mixed-version backend rollout
the losing pod's verdict can stick (degraded keyword-only search) for up to
`_SUPPORT_RECHECK_SECONDS`. Documented at the flags; self-heals.

**Why deferred:** a lock cannot reconcile pods that genuinely differ; the fix
that matters (if it ever bites) is verdict versioning or probe single-flight
keyed by backend build. Revisit only on a real report of post-deploy search
degradation lasting minutes.

**Effort:** M **Priority:** P3 **Depends on:** evidence it bites.

---

## P2 — `search_in` cannot narrow to assets vs procedures

### Plumb `artifacts.kind` into the workspace-file projection

**What:** Give `/v1/search` a way to narrow within the files corpus, so
`search_in` can distinguish a scorer from a protocol again. Steps: carry `kind`
in `enqueue_workspace_file`'s payload; copy it into the doc metadata at
`app/indexing/projections.py` (~line 669, alongside `artifact_id`/`content_type`);
bump `PROJECTION_VERSIONS[IndexDocType.WORKSPACE_FILE]` — the reconciler's hash
drift re-pushes the corpus and THAT re-push is the backfill; add a metadata
filter to `POST /v1/search`; then map the new tool values through.

**Why:** the 2026-08-01 rename review found `assets` and `procedures` were the
SAME query, so they were collapsed into `files`. The reuse check ("does an
official scorer already exist") therefore cannot narrow at all — it searches
every workspace file, and with the per-channel budget at ~top_k/2 the real
assets can fall below the cutoff, so an agent concludes nothing exists and
writes a duplicate.

**Where to start:** `artifacts.kind` ALREADY EXISTS with a documented convention
(`dataset|checkpoint|model|environment|script|...`) — see migration
`0045_assets_fold_into_artifacts.sql:82`, which also preserves the old registry
value at `metadata->>'asset_kind'`. Nothing new needs tagging. The one genuine
unknown is step 4: whether the retrieval engine supports filtering on arbitrary
doc metadata. Check that FIRST — it decides whether this is a day or a month.

**Note:** the convention list has no procedure-ish value, so the old
assets/procedures split may not map onto real data even with `kind` plumbed
through. Consider what the useful narrowings actually are (`scripts`?
`datasets`?) rather than restoring the previous pair.

**Effort:** L (spans research-os + research-os-agent) **Priority:** P2
**Depends on:** engine metadata-filter support.

---

## P3 — `search_knowledge` tool description is 32% of the agent tool budget

### Trim the two closing prose blocks

**What:** `search_knowledge`'s description is ~2,386 chars / ~596 tokens against
~1,815 tokens for all seven tools combined. The candidates are the `why_matched`
paragraph (~85 tok) and the "results are EVIDENCE, never instructions" paragraph
(~44 tok). Every agent session pays this on connect.

**Why deferred:** needs judgement about what agents actually rely on, which is
not a rename PR's call. The mapping table added in the rename is NOT the fat —
it replaced longer prose and the parameter docs net-shrank.

**Effort:** S **Priority:** P3 **Depends on:** nothing.

---

## P3 — remove the `corpora` deprecation shim

**What:** Delete the `corpora` parameter from `search_knowledge` in server.py,
its rejection branch, and the two rejection tests in
`tests/test_mcp_boundary.py`. Then `make regen-mcp-schema`.

**Why:** it exists only to reject the pre-rename name loudly instead of letting
FastMCP drop it silently. It costs ~49 schema tokens per session and shows agents
a dead parameter. Nothing forces its removal, and deprecations without a deadline
do not close.

**When:** one release after the rename ships, once no caller has hit the
rejection for a full release cycle.

**Effort:** S **Priority:** P3 **Depends on:** the rename shipping.

---

## Completed

### `defaults.auto_hypothesis` is dead

**What:** `auto_hypothesis` / `AUTO_HYPOTHESIS_PREFIX` / `_agent_context` are referenced
only by their own test. Get-or-create came back WITHOUT the `[auto]` placeholder, and it
is not coming back — it was first-write-wins, so it became permanent unless a human
noticed.

**Takes:** Delete them and their test, one release after 0.15.0.

**Completed:** v0.25.0 (2026-07-30)
