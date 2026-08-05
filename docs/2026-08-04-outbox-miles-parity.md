# Outbox ↔ Miles Durable Queue: Feature-Parity Spec & Plan

**Goal:** bring the generic async outbox (`sdk/journal.py` + `cli/outbox_worker.py`,
landed 2026-07-30) to feature parity with the Miles durable metric queue
(`integrations/miles.py`, landed 2026-07-22), so the Miles integration can be
rebased onto the outbox and its private queue deleted.

**Why now:** we ship two independent durability stacks with zero shared code.
The Miles queue predates the outbox by 8 days and solved the cluster-grade
problems (background export, bounded finish, exporter lease, multi-producer
accounting); the outbox solved the queue-semantics problems (all write kinds,
dead letters, failure taxonomy, multi-tenant credential handling). Every fix
currently lands twice or diverges.

---

## 1. Current state (verified 2026-08-04)

### Delivery model — the part we keep getting wrong

The outbox **does** have background delivery, but only on the CLI path:

- **CLI async** (`probe --async log|span add|note add|artifact add|run end`, or
  `PROBE_ASYNC=1`): every enqueue calls `_kick_drainer()` →
  `outbox_worker.maybe_spawn()`, which forks ONE **detached worker process**
  (`start_new_session=True`, survives the parent). The worker holds a
  `.worker.lock` flock lease for its lifetime, drains until the journal is
  empty (capped backoff 2s→300s on transient failure), re-checks `status.json`
  under the lease before exiting (exit-race guard), then exits. Later enqueues
  see the live lease and skip — one fork per idle period, not per write.
  Auth-block (401/403) → worker exits, ops stay queued. `probe outbox pause`
  → worker exits at next loop turn. A dead worker is re-kicked by the next
  probe command of any kind. Effective live-curve latency while healthy: near
  zero (no sleep between successful passes).
- **SDK async** (`Client(async_writes=True)`): `write()` →
  `journal.append_http()`, never touches the network — and **never spawns
  anything**. Delivery happens only at `client.flush()` (foreground drain,
  per-context client resolution), `run.finish()` (hard barrier: refuses a
  terminal status while ops are undelivered or dead-lettered), or whenever a
  subsequent `probe` CLI command happens to kick the worker. A pure-SDK
  training loop gets **no live delivery** for the whole run.
- **SDK sync** (default): network-first; on transport failure, fail-open
  journal spool (unless `strict`/`fail_open=False`).

Miles: `_MetricExporter` is an **in-process daemon thread** per run —
`claim_next()` → replay via `run.log(strict=True)` / `set_status` → ack —
with `wake()` on enqueue and a poll interval (`PROBE_EXPORT_INTERVAL_SEC`,
default 2s, floor 0.05s). `_ExporterLease` (flock `exporter.lock`) guarantees
a live exporter and an offline repair drain never race. Finish is **bounded**:
`drain_and_close(PROBE_FINISH_TIMEOUT_SEC=20)` polls until the queue is
empty or the deadline passes, then stops the thread — unconfirmed records
REMAIN ON DISK and the job exits; `drain_miles_metric_queue` repairs offline
later, resumable by external id.

### Parity matrix

| Capability | Outbox | Miles queue | Gap owner |
|---|---|---|---|
| Covers all write kinds (metrics, spans, notes, artifact blobs, run end) | ✅ http + upload ops, content-addressed `blobs/` | ❌ metrics + finish records only | — |
| Failure taxonomy (permanent→dead-letter, transient→halt+backoff, auth→credential halt, 409+existing_id→delivered) | ✅ | ❌ single retry path, no dead letters | — |
| Ops tooling (`status/drain/watch/retry/discard/pause/resume`) | ✅ | partial (`report()`, offline drain) | — |
| Multi-tenant safety (ops pin context name+base_url, tokens resolved fresh at drain) | ✅ | ❌ single client | — |
| Background delivery, CLI writers | ✅ detached worker | n/a | — |
| **Background delivery, SDK writers** | ❌ nothing until flush/finish | ✅ exporter thread, wake-on-enqueue | **F1** |
| **In-process exporter option** (no fork; custom transports; Ray actors) | ❌ | ✅ | **F2** |
| **Bounded finish** (deadline, retain on disk, job exits) | ❌ hard barrier only | ✅ 20s default | **F3** |
| **Multi-producer accounting** (producer registry, sequences, capture-gap report) | ❌ (flock allows concurrent appends but nothing accounts for them) | ✅ `producers/`, `producer_id = role:host:pid:uuid`, sequence tracking | **F4** |
| **Redaction at capture** | ❌ | ✅ `_is_sensitive_key`/`_scrub_string` | **F5** |
| **Run-scoped repair** (drain/retry one run; resumable by external id) | ❌ whole-journal only | ✅ | **F6** |
| Exporter-vs-repair mutual exclusion | ✅ already: `.drain.lock` per pass + `.worker.lock` worker lease | ✅ `exporter.lock` | none — document equivalence |
| fsync discipline (dir fsync on move/ack) | ✅ | ✅ | none |
| Queue schema versioning | ✅ `probe.outbox/1` | ✅ `_DRAINABLE_QUEUE_SCHEMAS` | none |
| Early "no deliverable credentials" check | CLI only (`_async_client`) | fail-open handling via `probe_fail_open` | **F7** |
| Step-key mapping, `planned_labeled_points`, run spec/identity | — | ✅ | **non-goal**: stays in the adapter |
| `expected_producer_set`, `distributed_close_barrier` | — | ❌ (Miles itself lists these as missing) | non-goal |

## 2. Non-goals

- Miles-contract adaptation stays in `integrations/miles.py`: step-key
  mapping, `planned_labeled_points`, run spec/identity resolution,
  `--use-probe`/`MILES_USE_PROBE` activation.
- Distributed close barrier / expected-producer-set: Miles doesn't have them
  either; parity means matching what exists.
- Changing sync-mode semantics (network-first + fail-open spool) — untouched.

## 3. Features

### F1 — SDK enqueue kicks the detached worker (default on)

Move `cli/outbox_worker.py` → `sdk/outbox_worker.py` (CLI keeps a re-export;
it only depends on journal + subprocess, no CLI imports). In
`Client.write()`'s async branch, after `append_http()`, call
`outbox_worker.maybe_spawn(journal.dir)` behind a ≤1/sec monotonic throttle
(`maybe_spawn` is already O(1): `status.json` read + one lock probe).
New kwarg `Client(async_writes=True, auto_drain=True)`; tests and
fake-transport users pass `auto_drain=False` (a detached worker cannot replay
through an in-process fake transport).

This alone closes the headline gap: pure-SDK async writers get the same
live-ish delivery CLI writers already have, from a worker that — unlike
Miles' thread — **survives the training process crashing**.

### F2 — in-process exporter thread (opt-in)

`sdk/exporter.py`: `OutboxExporter(client, interval)` — daemon thread that
takes the `.worker.lock` lease non-blocking (if a detached worker owns it,
the thread stays passive: delivery is already happening), then loops
`drain(journal, client_factory=<flush's factory>)` with a wake `Event` set by
`Client.write()` on every append, falling back to the interval poll.
Stops on auth-block (parity with worker exit 3), while paused, and on
`close()`. Activation: `Client(async_writes=True, drain_interval=2.0)` or
`PROBE_EXPORT_INTERVAL_SEC` (adopt Miles' env name verbatim so existing Miles
deployments port with zero config changes; same 0.05s floor).

When to prefer over F1: Ray/actor environments where forking is hostile,
custom transports, tests. F1 default + F2 opt-in mirrors Miles' behavior
while keeping the crash-surviving worker as the standard path.

### F3 — bounded finish

`Run.finish(status, summary=..., flush_timeout=None)`:

- `None` (default): today's hard barrier, unchanged.
- Number (or `PROBE_FINISH_TIMEOUT_SEC` when set): foreground-drain up to the
  deadline; if ops remain, **append the terminal-status write as an op**
  (the outbox analog of Miles' `type: "finish"` record), kick the worker, and
  return a report `{delivered, remaining, finish_queued: True}` instead of
  raising. The run reaches its terminal status when the tail drains — offline
  if need be.

CLI: `probe run end --flush-timeout N` (env honored). The op envelope carries
`session_id` + attempt info from the Run handle so the late drain attributes
to the original writing session (research-os#364 fencing: a queued finish
from epoch N must not clobber a resumed run's epoch N+1 — the server-side
quarantine handles this once #364 lands; until then the ordering guarantee is
FIFO-per-journal, same as Miles today).

#### F3a — pending-data accounting

The run never *claims* a terminal status ahead of its data: the queued finish
sits at the TAIL of the FIFO journal, so the server flips status only after
every op ahead of it lands. The exposure is the inverse — between job exit
and final drain the run looks like a stale "running" run with a dead
heartbeat, indistinguishable from a crash (Miles has this same hole today).
Accounting, in layers:

1. **Local, always available.** `finish()` returns
   `{delivered, remaining, finish_queued}`; `probe run end` prints a banner
   naming the spool dir, the per-run pending count, and the retrying worker;
   `probe outbox status` (and `--run <ref>`, F6) reports per-run
   pending/dead-lettered counts. Superset of Miles'
   `export-status.json`/`report()`.
2. **Exit beacon, best-effort.** At flush-timeout the client makes ONE
   bounded (~2s) synchronous attempt to PATCH the run:
   `writer_exited_at`, `pending_ops: N`, status → **`draining`** (new engine
   state; added to the #364 work list). Dashboard shows "draining — awaiting
   deferred upload" instead of a phantom "running". If the network is down —
   the very reason the flush timed out — the beacon fails silently; nothing
   can inform the server without a network, and the journal stays the source
   of truth.
3. **Permanent record on delivery.** The queued finish op carries `ended_at`
   (local exit time), `finish_deferred: true`, and `pending_at_exit`; on
   drain the run's metadata records delivered-at vs ended-at, so "final data
   arrived late via the outbox" is visible on the run forever, not just while
   ops are queued. Metric timestamps need no correction: op bodies journal
   the original wall clock captured at `log()` time.
4. **Liveness interplay.** `draining` counts as alive-ish for
   `on_conflict="auto"` — a merely-draining run must not be resumed or
   superseded. And if the reaper marked the run crashed before the drain
   completed, a deferred finish arriving with the LAST writing session's
   `session_id` corrects it to the real terminal status with a recovery note
   (#364's session/epoch validation is what makes that safe).

### F4 — multi-producer accounting

Journal grows a `producers/` registry (Miles' design, generalized):
`Journal.register_producer(producer_id, role)`; every op envelope gains
optional `producer_id` + `producer_sequence`; `Journal.status()` /
`probe outbox status` report per-producer last-sequence and detected gaps
(sequence skips = writes lost before enqueue). `producer_id` format follows
Miles: `role:host:pid:uuid`.

### F5 — redaction at capture

`Client(redact=callable | True)` — applied to op bodies in `append_http`
(and metric payloads specifically). `True` selects
`sdk/redaction.default_scrub`, ported from Miles' `_is_sensitive_key` /
`_scrub_string` (single source; Miles imports it back). Default `None`:
no behavior change.

### F6 — run-scoped repair

`probe outbox drain --run <ref>` and `probe outbox retry --run <ref>`:
filter ops by run reference (ops already carry the run path; envelope gains a
normalized `run_ref` field at append so filtering doesn't parse URLs).
Resumable by external id like `drain_miles_metric_queue`. Drained ops stamp
their ORIGINAL `session_id` (already in the envelope per F3) so late repairs
attribute to the execution that produced them, not the repair session.

### F7 — early credential check for SDK async

Mirror the CLI's `_async_client` guard: `Client(async_writes=True)` with no
token and no ingest token raises `ValidationError` at construction ("queueing
an op nothing can ever deliver fails hours later in the drainer") unless
`auto_drain=False` (test/fake-transport configurations are exactly the ones
with no real credentials).

## 4. Endgame: rebase Miles onto the outbox

Once F1–F7 land:

1. `MilesMetricBackend` writes through a `Client(async_writes=True,
   drain_interval=...)` journal instead of `DurableMetricQueue`; the adapter
   keeps step-key mapping, `planned_labeled_points`, run identity, and
   redaction *config* (the scrub impl now lives in `sdk/redaction`).
2. `drain_miles_metric_queue` keeps reading the legacy on-disk schema for one
   release, plus `probe outbox import-miles <dir>` to convert stranded
   legacy queues.
3. One release later: delete `DurableMetricQueue`, `_ExporterLease`,
   `_MetricExporter` (~700 lines).

Behavior deltas for Miles after the rebase — all strict upgrades:
dead-letter queue + `probe outbox` tooling, artifact/span coverage,
multi-tenant-safe drains, and a drainer that survives job death.

## 5. Plan (phases = PRs, in order)

Each task lands TDD-style with tests in the named files; phases 1–3 are
independent of 4–5 and can be reviewed in parallel.

- [ ] **P1 — F1 + F7** · move worker to `sdk/outbox_worker.py`; `auto_drain`
  kwarg + throttled `maybe_spawn` in `Client.write()`; construction-time
  credential check. Tests: `tests/test_outbox_autodrain.py` — enqueue spawns
  once per idle period; `auto_drain=False` never spawns; credential check
  raises/exempts correctly. (CLI keeps working via re-export;
  `tests/test_cli_outbox.py` unchanged = the regression gate.)
- [ ] **P2 — F2** · `sdk/exporter.py` + `drain_interval` /
  `PROBE_EXPORT_INTERVAL_SEC`. Tests: wake-on-append delivers without waiting
  a full interval; passive when worker lease held; auth-block stops the
  thread with ops intact; `close()` joins.
- [ ] **P3 — F3** · `flush_timeout` on `Run.finish` + `probe run end
  --flush-timeout` + `PROBE_FINISH_TIMEOUT_SEC`. Tests: default stays a hard
  barrier; bounded path retains ops + queues the finish op (with
  `finish_deferred`/`ended_at`/`pending_at_exit`) + returns the report; exit
  beacon PATCHes `draining` when the network is up and is skipped silently
  when it is not; a later drain lands the terminal status attributed to the
  original session_id. Engine-side `draining` state + deferred-finish
  correction ride the #364 work list, not this PR — until they land, the
  beacon PATCH is a tag/summary-field fallback.
- [ ] **P4 — F4** · producer registry + envelope fields + status/CLI report.
  Tests: interleaved producers; gap detection on sequence skip.
- [ ] **P5 — F5 + F6** · redaction hook (port Miles scrub, Miles imports it
  back) + `--run` filters on drain/retry + `run_ref` envelope field. Tests:
  sensitive keys scrubbed at rest in op files; run-scoped drain leaves other
  runs' ops queued.
- [ ] **P6 — Miles rebase** · adapter on outbox, legacy-queue import,
  deprecation notice on `DurableMetricQueue`. Gate: run the
  `run/nebius-sandbox-metrics` job with `MILES_USE_PROBE=1` against a real
  workspace before merging.
- [ ] **P7 (next release) — delete the legacy queue.**

## 6. Open decisions (recommendations inline, flag disagreement at review)

1. **F1 default-on?** Recommended yes — async mode that silently delivers
   nothing until `finish()` is a footgun we already hit reasoning about it.
   The `auto_drain=False` escape covers tests/fakes.
2. **F7 raise vs warn** — recommended raise (matches CLI behavior; failing at
   construction beats failing hours later in a detached worker's log).
3. **Env-name reuse** (`PROBE_EXPORT_INTERVAL_SEC`, `PROBE_FINISH_TIMEOUT_SEC`)
   — recommended: adopt Miles' names as the generic ones; zero-churn
   migration for existing Miles deployments.
