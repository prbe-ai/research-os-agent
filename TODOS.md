# TODOs

Deferred work for the Probe Research agent (CLI, SDK, plugins). Each item states what
it is, why it matters, and roughly what it takes.

---

## P2 — coding-agent coverage

### Transcript capture for Cursor and Codex

**What:** A capture plugin for Cursor and for Codex, equivalent to
`plugins/probe-research-tap` for Claude Code.

**Why:** `sdk/defaults.py` `_agent_context()` already detects all three agents, and both
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

### `defaults.auto_hypothesis` is dead

**What:** `auto_hypothesis` / `AUTO_HYPOTHESIS_PREFIX` / `_agent_context` are referenced
only by their own test. Get-or-create came back WITHOUT the `[auto]` placeholder, and it
is not coming back — it was first-write-wins, so it became permanent unless a human
noticed.

**Takes:** Delete them and their test, one release after 0.15.0.
