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

## Completed

### `defaults.auto_hypothesis` is dead

**What:** `auto_hypothesis` / `AUTO_HYPOTHESIS_PREFIX` / `_agent_context` are referenced
only by their own test. Get-or-create came back WITHOUT the `[auto]` placeholder, and it
is not coming back — it was first-write-wins, so it became permanent unless a human
noticed.

**Takes:** Delete them and their test, one release after 0.15.0.

**Completed:** v0.25.0 (2026-07-30)
