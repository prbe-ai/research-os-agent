# Automatic hardware metrics (`probe.hw`) — design

Collect hardware/system metrics (GPU, CPU, memory, disk, network) for every
run automatically — no training-loop coupling, no user code — and store them
through the existing metric rail using primitives the store already has:
coordinates instead of key-string encoding, first-write-wins identity for
idempotency, declared per-series aggregation, and `kind=hardware` for rail
separation. The design is Prometheus-first at the source layer: where a
Prometheus ecosystem exists (exporters or a full server), we ride it; a small
in-process floor (psutil + NVML) covers machines that have nothing. A
deterministic epoch-derived `step_index` makes redelivery, restart, *and
after-the-fact backfill from an existing Prometheus server* all idempotent —
backfill is a first-class mode, not a repair hack.

Status: REVIEWED (eng pass + adversarial outside-voice pass, 2026-08-05).
All decisions locked — see §Resolved decisions. Phase 1 requires **one
surgical server PR** (resume-receipt predicate + hw series budget, no
migration); everything else writes through surfaces that shipped in
research-os #177/#190 (coords, grouped reads, declared agg 0062).

## Evidence base

Investigated both incumbents at source (2026-08-05):

**W&B** (`wandb/wandb@e8a753d`): collection lives entirely outside Python — a
Go daemon (`core/internal/monitor/`) owns scheduling via a two-method
`Resource` interface (`Sample()` per tick, `Probe()` once for static
metadata), and all vendor GPU/TPU code sits in a separate Rust sidecar
(`wandb-xpu`: NVML, DCGM, AMD, Apple private APIs, libtpu) reached over gRPC
with a portfile handshake and parent-pid watchdog. Samples go to a dedicated
time-indexed events channel (`wandb-events.jsonl`, `system.gpu.0.powerWatts`
keys), separate from step-keyed history. Device and node identity are encoded
in key strings (`gpu.0.temp`, `/l:label` suffix) because the store has no
dimension concept. A generic OpenMetrics scraper (regex metric/label filters)
is their zero-code extension door. ~15k lines across Go+Rust; default-on.
One collector per machine via the shared service process.

**MLflow** (`mlflow/mlflow@2e08b5a`): the entire feature is 581 lines of pure
Python — one daemon thread, psutil + try-pynvml→fallback-pyrsmi monitors,
opt-in (default off). Samples are stored as *ordinary run metrics* named
`system/...` at a synthetic step counter. One idea worth taking: client-side
pre-aggregation — sample every 10s but log one aggregate per N samples,
decoupling observation resolution from storage volume. No hardware inventory
capture, no cgroup awareness, no per-process attribution; run-liveness
checked by polling `get_run()` per sample (an HTTP call per tick — do not
copy); a flaky sensor warns every 10s forever (do not copy either).

**The structural observation that shapes this design**: both incumbents'
architectures are largely compensations for missing store primitives (no
dimensions → key-string encoding; no idempotent identity → resume-step
queries; no declared agg → hardcoded mean or UI-side guessing). Probe has the
primitives, so the correct integration is thinner than either incumbent — an
MLflow-weight collector with W&B's architectural ideas on top of our data
model.

## Hard requirements

- **Zero training-loop coupling.** The collector is a background thread; it
  never needs the current training step, never hooks the loop, never raises
  into user code. Fail-open like the miles ProbeBackend (collector dies → run
  continues).
- **Idempotent under redelivery, restart, and multiple producers.** Any
  process, at any time, computing a sample for the same instant must produce
  the same point identity, with **zero shared state** (no server round-trip,
  no config that can drift between writers).
- **One collector per node.** Multi-process training (torchrun/DDP: 8+ ranks
  per node, each calling `run()`) must elect a single collector; N collectors
  scraping and writing the same node-scoped series is wasted load with a
  nondeterministic first-writer.
- **Run-scoped attribution.** Metrics must be attributable to *this run's*
  node/pod/devices, not the whole machine or cluster.
- **Invisible to the rest of the SDK's step machinery.** The hardware rail
  must never influence resume guards, auto-step floors, or any
  training-metric semantics (see §Wire model / resume).
- **No new storage rail.** `kind=hardware` + existing columns. A second
  storage path is a second ingestion/retention/export/dashboard surface; the
  tracker-comparison verdict (INCREMENTAL) applies here too.

## Architecture

`probe.hw` in the SDK: an `HwMonitor` owning one daemon thread and a list of
sources, mirroring W&B's interface shape:

```python
class HwResource(Protocol):
    def probe(self) -> dict: ...            # static inventory, called once
    def sample(self) -> list[HwSample]: ... # HwSample(key, value, coords)
```

- Started by `run()` (default ON — see §Resolved decisions), stopped by
  `finish()`; pause/resume for notebook contexts. A first-run log line
  announces collection (transparency without a prompt). Kill switches:
  `PROBE_HW=0` env or `run(hw=False)`.
- **Collector election (one per node per run):** a process starts the
  collector only if it is the node-local leader — `LOCAL_RANK==0` /
  `SLURM_LOCALID==0` style heuristics first, falling back to a per-(host,
  run_id) file lock in the state dir. Non-leaders start nothing. The
  election is best-effort: if two collectors ever race through, first-write-
  wins keeps the data correct; election exists to avoid the wasted load, not
  for correctness.
- **Identity snapshot at start.** Unit context is contextvar-scoped
  (thread-local, `run.py` `unit` docstring) and does NOT reach a daemon
  thread. `HwMonitor.start()` eagerly captures host, rank, pod name
  (downward API), and device set once, and stamps coords from that snapshot —
  it never reads ambient context from its own thread.
- Constructors are availability probes: each source returns `None`/raises at
  init when its substrate is absent, and registration is conditional —
  absent hardware costs nothing anywhere (W&B's pattern). **Anything that
  can block — endpoint discovery, NVML init, `probe()` inventory — runs on
  the collector thread's first tick, never inside `run()`.**
- **Failure taxonomy, not a vibe:** per-source consecutive-failure counter
  with a circuit breaker — N consecutive failures disables the source for
  the rest of the run with *one* warning (never warn-per-tick). When a
  breaker trips on a source that had claimed a family, the family
  **re-delegates down-tier next tick** (scraper dies → NVML floor takes GPU
  back); first-write-wins makes the handoff safe by construction.
- Sampling cadence ~10–15s; **logging cadence 60s** — samples accumulate in
  the thread and each window emits one point per series, reduced by that
  metric's declared agg. **Non-finite samples (NaN/Inf) are dropped at the
  source** — Postgres treats NaN=NaN as TRUE and non-finite values poison
  catalogs (learned the hard way server-side, 2026-07-27).
- **Series governor (client side):** the collector counts the distinct hw
  series it emits and self-caps at **2,500 per node** (config-raisable);
  over budget it degrades by family priority — per-process GPU stats first,
  then per-mount disk, then per-device detail — with one warning. Typical
  nodes emit ~220 series, so the governor is a runaway guard (e.g. a buggy
  mapping pack exploding coords), not a normal-use limit.

### Tiered sources (Prometheus-first)

Priority order at init; a higher tier claiming a metric family disables the
lower tier's version, so nothing double-logs. The claim table is static and
dumb — three families (gpu / system / container) × source, no dynamic
capability negotiation:

1. **PromQL source** (phase 1b) — an existing Prometheus-compatible *server*
   (see §Existing-Prometheus integration). Best data, one HTTP query per
   interval, server-side `rate()`.
2. **Exporter scraper** — direct exposition-format scrape of well-known
   endpoints, auto-discovered **non-blocking on the first collector tick**:
   DCGM-exporter (`:9400`), node_exporter (`:9100`) — both unauthenticated,
   host-local, default-on. **Kubelet cAdvisor is opt-in**
   (`PROBE_HW_KUBELET=1`): it presents the pod's service-account token, and
   a credentialed call from a pip-installed library must be an explicit
   choice, not a surprise for the customer's security team. When enabled,
   cAdvisor supplies the cgroup-correct container metrics psutil cannot see:
   `container_memory_working_set_bytes` (what the OOM-killer acts on) and
   `container_cpu_cfs_throttled_periods_total` (throttling). Explicit
   endpoint config always skips discovery. Regex metric + label filters
   (W&B's scraper design).
3. **In-process floor** — psutil (CPU, memory, disk, network) +
   `nvidia-ml-py` (per-device GPU util/memory/power/temp + per-process
   attribution via pid, which *only* in-proc NVML provides — exporters are
   machine-scoped). Plus a ~50-line cgroup-v2 reader (`memory.current/max`,
   `cpu.max`) so container quota-awareness survives with kubelet off.

**Attribution** is a named requirement: cluster-scoped sources report every
pod/GPU on the node. The collector filters to its snapshot identity
(downward-API pod name, hostname fallback). Per-GPU attribution resolves
`CUDA_VISIBLE_DEVICES` — **integer, `GPU-<uuid>`, and MIG-instance forms** —
to physical NVML indices via UUID lookup; coords always carry the physical
index. MIG limitation: standard NVML does not report per-instance
utilization — under MIG we fall back to parent-GPU metrics and document it
(DCGM/DCGM-exporter is the MIG-correct path, one more reason tier 2 matters).

**Counters become rates at the source, with reset semantics.** Prometheus
counters are cumulative; the scraper keeps last-scrape state and emits
deltas/rates. A negative delta means the exporter restarted (counter reset):
**emit nothing for that window and re-baseline** — never a spike. The
PromQL source pushes `rate()` into the query; psutil counters (network/disk
io) diff against the previous sample with the same reset rule. Counter-vs-
gauge classification lives in the shared mapping layer (below), in exactly
one place.

**One shared mapping layer** (`probe/hw/mappings.py`): metric-name →
(key, coords, counter/gauge, declared agg) for the standard exporter
families — `DCGM_FI_*` → `hw/gpu/*` (gpu label → coord), `node_*` →
`hw/system/*`, `container_*` → `hw/proc/*` — consumed by BOTH the exporter
scraper and the PromQL source so the pack cannot drift between tiers.
User-extensible declaratively (expr → key + label→coord map).

Deliberately absent: Apple Silicon (sudo `powermetrics` or private APIs; we
don't train on Macs), TPU/Trainium native code (no fleet; the scraper covers
them if an exporter exists). Decision rule: vendor-specific code only when a
real fleet needs it AND no exporter exists.

## Wire model

- **Keys**: `hw/<subsystem>/<metric>` — `hw/gpu/utilization`,
  `hw/gpu/memory_used_bytes`, `hw/cpu/utilization`, `hw/mem/used_percent`,
  `hw/disk/<mount>/used_percent`, `hw/net/sent_bytes_rate`,
  `hw/proc/rss_bytes`, `hw/proc/gpu_memory_bytes` (run-pid-attributed).
- **Coordinates, not key strings**: device identity rides in coords —
  `coords={"gpu": 3}` (physical NVML index), host/rank from the start-time
  identity snapshot. All bounded (D18-legal). The existing `grouped by=gpu`
  endpoint yields per-device lines and reduce-across-devices; the series
  catalog folds min/max/last per device with zero new code.
- **Range axis — epoch grid (LOCKED):**
  `step_index = floor(unix_seconds / 60)` — a **fixed protocol constant**,
  not config. Zero shared state: any process, restart, or backfill job at
  any time computes the same step for the same instant, so first-write-wins
  dedups by construction. Run-relative derivation was rejected because it
  requires every writer to agree on `started_at` (server round-trip) and on
  an interval (config that can drift between live collection and backfill —
  drift silently breaks dedup). Step numbers are opaque (~29.7M in 2026);
  nobody reads hw step numbers — charts use `wall_clock`, which is stored on
  every point as the join key to training steps.
- **Resume invisibility (LOCKED — the outside voice's P0 catch):** the
  reopen receipt's `last_step` arms a kind-agnostic resume guard
  (`run.py` `arm_resume_guard`, research-os#364) — one hw point at ~29.7M
  would make a resumed run refuse all training steps and mint auto-steps
  above 29.7M. Fix, both halves required:
  1. *Server (the one surgical change):* receipt `last_step` — and any
     other step-floor computation — **excludes `kind=hardware`** (single
     predicate, no migration).
  2. *Client (defense in depth):* the resume guard and `_auto_step_floor`
     become kind-scoped; the hardware rail neither consults nor updates
     them.
- **Sub-minute visibility inside 60s windows:** the window reduce uses each
  metric's declared agg (utilization→`mean`, memory/temp→`max`,
  rates→`mean`, capacity→`last`), and **stall-sensitive metrics emit
  min/max companion keys** (`hw/gpu/utilization_min`) so a 20-second
  all-GPUs-idle stall cannot hide inside a 60s mean. If finer resolution is
  ever needed (debug mode), high-rate samples ride the **labeled per-sample
  rail** (existing, separate identity semantics and budget) — the canonical
  60s grid is never broken, so higher resolution is an additive mode, not a
  protocol change.
- **`kind=hardware`** on every point (what `log_hw` already sends) — rail
  separation by column, not by store.

### Rail isolation for readers (verified server-side)

Every metric read endpoint — list, `/metrics/wide`, grouped, series-latest —
already takes an optional `kind` filter (research-os `metrics_router.py`,
`series_router.py`, verified @ main e4537d9; wide's docstring: "one cell
must never average across kind namespaces"). Conflation with training
metrics is therefore impossible at the data level (identity includes key,
catalog includes kind) and preventable at the presentation level by
convention: **step-axis chart rails and wide pivots exclude
`kind=hardware` unless explicitly requested; the hw dashboard section plots
against `wall_clock`, never step.** A kind-less query would render training
(0–50k) and hw (~29.7M) on one axis as garbage — the rule exists so no
reader ever does that.

### Why not step-keyed hardware samples (rejected)

Step-based sampling only observes the machine while training is making
progress, and hardware pathologies live precisely where it isn't: GPUs idle
during data loading, checkpoint I/O saturation, eval stalls, NCCL hangs where
the step count stops entirely. Sampling on step boundaries produces zero
samples in exactly those windows — a built-in selection bias toward
"everything looks fine." Additional strikes: step duration is unbounded in
both directions, it requires training-loop coupling (violates requirement 1),
and miles already runs two step cadences — hardware belongs to neither; time
is the only clock all processes share. Correlation with training steps is a
`wall_clock` join in the dashboard (W&B reached the same conclusion). An
optional phase-2 refinement: *event-triggered bonus samples* at span
boundaries we already record, stamped with a label — enriches the time rail
without inheriting step-sampling's failure modes.

## Existing-Prometheus integration and backfill

Two situations, one falls out free:

**They run exporters** → tier 2 covers it today: point the scraper at the
endpoints (or let non-blocking discovery find the well-known ports) plus
label filters. Nothing new.

**They run a Prometheus server** (or Thanos / Mimir / VictoriaMetrics /
Grafana Cloud / Amazon Managed Prometheus — same HTTP query API) → the
**PromQL source** (phase 1b): instant queries against `/api/v1/query` each
interval, bearer/basic auth headers, one round-trip returns every metric for
this node/pod already labeled and `rate()`-converted. Config is a server URL
+ auth + optional mapping overrides.

The customer experience for "we already have Prometheus for our hardware":
set `PROBE_HW_PROM_URL` (+ auth token) or pass the equivalent in `run()`,
and run as normal. Probe installs nothing on their machines and deploys no
exporters — it is a read-only client of the Prometheus they already operate,
pulling only this run's node/pod slice. **Honest caveat (from adversarial
review): real fleets rewrite labels via `relabel_configs`, so the default
mapping pack is a starting point, not a guarantee** — the mapping layer's
declarative overrides are the supported path, and 1b ships a
`probe hw doctor --prom URL` command that shows exactly which metrics mapped,
which didn't, and why. If their Prometheus lacks a family, lower tiers cover
what they can.

**Backfill** — the payoff of stateless identity: Prometheus retains weeks of
history, so `query_range` over `[run.started_at, run.ended_at]` (chunked,
1h; window bounded to `now` for still-running runs) reconstructs a run's
hardware record after the fact, landing on identities identical to live
collection. Semantics, stated precisely (adversarial finding #4 accepted):

- **Backfill is gap-fill.** First-write-wins means live points always beat
  backfilled points for the same bucket — including a partial window a dying
  collector wrote. Backfill fills *missing* buckets; it does not "repair"
  existing values, by design (the store deliberately forbids overwrite).
- Live-NVML values and Prometheus-derived values for the same key are
  systematically different estimators; the family-claim rule prevents them
  from ever contending for the same series during a run, and gap-fill
  semantics resolve the backfill seam.
- Clock basis: backfill derives steps from Prometheus timestamps, live from
  local wall clock; NTP is an explicit assumption — ≤ seconds of skew moves
  nothing (60s buckets), unsynced-by-minutes clocks will misalign buckets
  and are out of scope.

Surfaces: `probe hw backfill <run> [--prom URL] [--window ...]` (CLI + SDK).
**Zero-overhead mode** — no collector thread, backfill reconstructs the run
afterwards — is a *post-mortem tool*, not a `finish()` hook: crashed runs
(OOM-kill, preemption, NCCL hang) never reach `finish()`, so the mode's
contract is "run backfill any time after the fact; the run's heartbeat
window bounds the range." The identity snapshot (node/pod for label
filtering) and inventory probe still run at start even in this mode — they
are cheap and backfill needs them.

**Copy-in, not reference** (decided): we ingest into the run store rather
than reading the customer's Prometheus at render time. Prometheus retention
is ~weeks and runs are permanent; coords/grouped/export need the data local;
read-time federation is auth sprawl into every viewer context.

## Transport, async logging, and isolation

**Async compatibility is by construction.** The hw rail has no ordering
requirement: every point is self-identifying (epoch step, coords, key), so
out-of-order arrival, interleaving with training batches, and delayed
flushes all land correctly. Under the durable queue's at-least-once
semantics, a redelivered hw batch recomputes to identical identities and
first-write-wins drops the duplicates — the property the miles
preemption-replay tests already prove, inherited because steps are derived,
not counted. Volume: one ~220-point batch per node per minute.

**Backpressure priority (decided, mechanism specified):** hardware is
best-effort — bounded in-memory buffer, drop-oldest on overflow, and
drop-rather-than-spool during outages. Because `Client.write()` is the
single funnel for every SDK write (journaling, async mode, detached-lease
renewal ride on it), hw does NOT fork the transport: `write()` gains a
per-call **durability hint** (`durable=True` default; hw passes
`durable=False`) honored by the journal/spool layer. One funnel, per-kind
policy. Hardware never competes with training metrics for spool space,
bandwidth, or retries — and backfill can reconstruct dropped windows
wherever a Prometheus server exists.

**Isolation boundaries** — isolated where isolation protects the run, shared
where sharing is the point:

- *Execution*: own module (`probe.hw`), own daemon thread, per-node
  election, fail-open at init and per-sample, circuit breakers, one kill
  switch. Shared code surface: only the transport client.
- *Data*: `kind=hardware`, `hw/` prefix, coords-only — never touches the
  labeled-point budget; readers include/exclude by column; own dashboard
  section on `wall_clock`.
- *Deliberately shared*: the `metric_points` table, series catalog,
  ingestion endpoint, auth, export — one store, one dashboard, one export
  path is exactly why a separate rail was rejected.

## Static inventory → `execution_records`

`probe()` results — GPU model/count/VRAM, driver + CUDA version, CPU model,
RAM, hostname — are one fact per run, not a time series. They land in
`execution_records` beside `env_ref`/snapshot data: hardware is
reproducibility context, same family as image and argv (both flagged
uncaptured in the 2026-08-04 repro-manifest audit). Queryable filters
("all runs on H100 + driver 550.x") are the top practical use.

**Cross-plan dependency (adversarial finding #13):** snapshot/
`execution_records` is currently opt-in — many runs have no record row.
Inventory therefore **mints a minimal execution_record when none exists**
(inventory-only, no env_ref) — this is a deliberate semantic extension of
that table and must be coordinated with the snapshot plan
(`tasks/2026-08-03-snapshot-reproducibility-plan.md`, whose Phase 1b makes
records automatic anyway; this design is a forcing function, not a
conflict). Multi-node: one record per host, keyed by host coord.

## Server changes (the one surgical PR)

Phase 1 is client-only **except** one small research-os PR, no migrations:

1. **Resume receipt excludes hardware** — REVISED during implementation
   (2026-08-06): `POST /v1/runs/{id}/reopen` (research-os#364) turned out to
   have **no server implementation yet** — the SDK shipped ahead and
   translates the 404. So there is no receipt to fix today; the risk is
   latent, not live. What ships instead: the predicate is a RECORDED
   REQUIREMENT for #364's implementation (`last_step` computed over
   `kind <> 'hardware'`; the `METRIC_KIND_HARDWARE` constant is staged in
   `app/telemetry/schemas.py` and the requirement documented in the branch
   commit body), and the CLIENT ships both halves of its defense now:
   kind-scoped guard/auto-floor, plus a suspect-receipt fallback (a
   `last_step` ≥ 10M is in hardware's epoch range ⇒ warn and skip arming —
   covers any server that later builds #364 without the exclusion).
2. **Hardware series budget** (SHIPPED on `feat/hw-rail-server-guards`):
   series-cap accounting counts `kind=hardware` against its own budget —
   default **25,000 per run** (covers ~100-node fleets at ~220/node; a
   runaway-mapping guard, not a normal-use limit) — and hw series are
   excluded from the user-facing 10k cap, so default-on hardware can never
   displace a user's training series. App-logic change in the existing cap
   check (`store.py` `_guard_series_cardinality`, rail-split count).

## Volume and budget analysis (corrected in review)

Per node: ~25 GPU keys × 8 devices + ~20 system/proc keys ≈ **~220
series/node**. Points at 60s windows: 220 × 1,440 = **~317k
points/node/day** (a 4× cut from 15s raw ~1.27M — an earlier draft
overstated this as 20×; the adversarial pass caught the arithmetic). A
32-node month is ~300M rows: real, bounded, and the reason **retention/
rollup for the hw rail is a tracked TODO** (TODOS.md: raw kept ~90 days →
1h rollups; triggered by observed volume, not built speculatively).
Hardware points are unlabeled (coords only) so they never touch the
labeled-point budget. Windows align to the epoch grid, so re-sent windows
dedup rather than double-count. Client governor 2,500 series/node; server
budget 25,000/run — both config-raisable, neither reachable in normal
operation.

## Testing (ships with the code, not after it)

Unit (14): monitor lifecycle; window reduces per agg class; circuit breaker
(source disabled after N failures, ONE warning, run unaffected);
**non-finite dropped**; backpressure drop-oldest under stalled transport;
epoch-grid property test (same instant ⇒ same step across processes);
mapping tables against captured DCGM/node_exporter/cAdvisor exposition
fixtures; counter rate state (first scrape, **reset ⇒ drop window +
re-baseline**, wrap); psutil + cgroup-v2 quota paths (fake /sys/fs/cgroup
tree); NVML availability probe (no-NVML machine ⇒ None, silent);
`CUDA_VISIBLE_DEVICES` mapping (integer, UUID, **and MIG** forms ⇒ physical
NVML index); non-blocking discovery (unreachable ports ⇒ zero init
latency); collector election (LOCAL_RANK heuristics + file-lock fallback,
two-process race); kill switch (`PROBE_HW=0` ⇒ zero hw artifacts);
identity snapshot (daemon thread emits coords captured at start, not
ambient).

E2E, docker-gated, auto-skip (5): real Prometheus + node_exporter compose
fixture ⇒ scrape ⇒ mapping ⇒ points; duplicate-window redelivery ⇒ deduped
(real SDK → uvicorn → Postgres, the sim-harness pattern); backfill
`query_range` chunking + still-running guard + gap-fill-not-overwrite proof;
inventory ⇒ execution_record (incl. minimal-mint path); zero-config run on
a plain box ⇒ hw points in PG. Plus one `@gpu`-marked live NVML test
(Nebius smoke).

The resume-invisibility server change is proven by a sim test: hw points at
epoch steps + reopen ⇒ receipt `last_step` reflects training rail only.

## Dashboard implications (follow-on, not phase 1)

- Run page: hardware section grouping the `hw/` prefix, plotted on
  `wall_clock`; per-device lines via `grouped by=gpu` with the reduced rail
  muted (both shipped server-side).
- Step drill-down: "hardware around this step" — step → wall-clock window →
  hw rail (the join that replaces step-stamping).
- Inventory chip from `execution_records`.

## Phasing

- **1a — floor + scraper** (+ the surgical server PR): `probe.hw`,
  election, psutil/NVML/cgroup-v2 floor, exporter scraper with non-blocking
  discovery + label filters, mappings layer, wire model, governor,
  durability hint, inventory→execution_records, full unit suite + E2E 1/2/4/5.
- **1b — PromQL + backfill** (GATED: starts only after 1a is live on the
  Nebius fleet and the mapping pack is validated against its real labels —
  adversarial finding #16 accepted): PromQL source, mapping overrides,
  `probe hw doctor`, `probe hw backfill`, zero-overhead mode, E2E 3.
- **2 — only on demonstrated need**: sidecar for vendor code (the
  committed-static-Go-binary precedent + W&B's portfile/UDS/parent-pid
  contract), event-triggered span-boundary samples, Apple/TPU native.

## Resolved decisions (eng review + outside voice, 2026-08-05)

1. **Default ON**, first-run log line, `PROBE_HW=0` / `run(hw=False)` kill
   switches.
2. **Epoch × fixed 60s protocol grid** for step_index; superseded
   run-relative derivation (shared-state drift risk).
3. **Resume invisibility**: kind-scoped client guards + suspect-receipt
   fallback shipped; server-side predicate is a recorded requirement for
   #364, whose reopen endpoint turned out to be unbuilt (see §Server
   changes). [outside-voice P0]
4. **Non-blocking discovery/init** on first collector tick; explicit config
   skips discovery.
5. **Kubelet cAdvisor opt-in** (`PROBE_HW_KUBELET=1`); localhost exporter
   discovery default-on. [security posture]
6. **Per-node collector election** (LOCAL_RANK heuristics + file lock).
7. **Series budgets kept high** per review: client governor 2,500/node,
   server hw budget 25,000/run, hw excluded from the user 10k cap.
8. **All 5 E2E docker-gated tests in-plan**; container-smoke standing rule.
9. **1b gated on 1a shipping** on the real fleet.
10. **Backfill = gap-fill**; live wins by first-write; zero-overhead mode is
    post-mortem-capable and does not depend on `finish()`.
11. Backpressure: hw `durable=False` through the single write funnel;
    drop-oldest, never spool.
12. Retention/rollup: tracked TODO gated on observed volume (TODOS.md).

## Pointers

Source read for this design (scratchpad clones, 2026-08-05):
`wandb/core/internal/monitor/{monitor,system,openmetrics,xpuresourcemanager}.go`,
`wandb/xpu/src/monitors.rs`, `wandb/core/internal/filestream/updatestats.go`,
`mlflow/mlflow/system_metrics/`. Verified in-repo during review:
`src/probe/sdk/run.py` (resume guard, `_next_step`, unit-context
thread-locality), `src/probe/sdk/client.py` (receipt `last_step`, write
funnel); research-os `app/telemetry/{metrics_router,series_router}.py`
(kind filters, @ main e4537d9). Related: 2026-07 metric-store decisions
(coords/labels D17-D18, first-write-wins, 0062 declared agg, grouped
reads — research-os #177/#190); `run.log_hw` (kind=hardware precedent,
fold #9); July PR #17 design memo (H1–H4 watcher/backfill ideas — backfill
here is their concrete mechanism); adversarial review 2026-08-05 (16
findings: 4 confirmed-major folded in as decisions 3/6/7/12-adjacent,
9 folded as amendments, arithmetic correction in §Volume).
