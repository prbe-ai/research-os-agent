# Reproducibility capture & enforcement — design

**Date:** 2026-08-04
**Status:** Draft for review
**Repos:** `research-os-agent` (capture, CLI, MCP, skills) + `research-os` (two read endpoints, CONTRACT note — **zero migrations**)
**Audited against:** research-os `a9fcc32` (v0.111.0.0), research-os-agent `1812d80` (v0.52.0)

## Problem

The 2026-08-04 audit of the reproducibility record found the storage layer strong but
the capture and read loops broken in practice:

1. **Capture is opt-in.** Nothing snapshots unless someone calls `snapshot()` or
   follows a skill. `probe exec` correlates a process to a run but captures nothing
   about it. Coverage depends on discipline — exactly the failure mode the skills
   were written to paper over.
2. **Known slots are never filled.** Nothing captures argv/command line, seeds,
   env-var names, container image, OS/CUDA versions, or the lockfile *as a file*
   (only the enumerated interpreter packages survive; a dirty `uv.lock` is silently
   lost). `execution_records.settings`/`paths` exist and sit empty.
3. **Pull is fragmented.** A coworker reconstructing "what was happening when this
   ran" must join `runs` + `execution_records` + artifacts + notes + lineage +
   foreign_keys by hand. MCP `view="reproduce"` returns only hypothesis + config +
   env_ref + a summarized execution record. There is no experiment-level pull and no
   CLI verb.
4. **`experiment_versions` — the literal per-experiment manifest — has no
   ergonomic mint path**, so it likely has near-zero rows in prod.

## Goal

Every run and experiment carries — automatically, without researcher discipline —
enough to (a) **rebuild it** and (b) **answer questions about what was happening
when the coworker ran it**, including normally-ephemeral context (how it was
launched, on what machine, with which visible devices, under which scheduler job,
with which decisions made along the way). All of it pullable in one call, at every
granularity: experiment → run → span/trial.

### Principles (locked with Mahit, 2026-08-04)

- **Warn, never gate** *(revised 2026-08-06 — supersedes the original "block
  claims" opt-in strict mode, which Mahit rejected after seeing it built)*.
  Capture failure never fails or blocks a research run, and NOTHING — opt-in or
  otherwise — may raise on an incomplete capture. When a run finishes as
  `completed` with capture enabled but incomplete, the SDK emits a warning
  naming what is missing and pointing at `probe run check`; when capture was
  declined (`PROBE_AUTO_SNAPSHOT=0`), it is silent. Enforcement stays social +
  scriptable: skills report the `run check` verdict at handoff, and the
  command's exit code is the hook for anyone who wants their own CI gate.
- **Honesty over completeness.** Every capture is best-effort with recorded
  provenance and an `errors` list (the `probe.sandbox-state/1` `meta.errors`
  pattern). Absence is reported, never guessed. Reproduce views are never silently
  truncated — overflow is reported (existing view philosophy, `mcp/service.py`).
- **Normalized truth, assembled views** (Approach A + export flag). No new tables,
  no materialized manifest as source of truth. The manifest is assembled at read
  time; `--export` renders a portable JSON bundle on demand.

## Non-goals

Dashboard UI (separate effort — explicitly deprioritized: what matters is pull, not
render). Multi-step sandbox trials. Backfill of historical runs ("fresh runs matter
more than history", 2026-07-16). Cursor/Codex transcript capture. Hardware
allocation ledger.

---

## D1. The identity split (load-bearing)

`execution_records` are content-addressed and deduped — two runs with the same
environment share one record (`app/execution/service.py:canonical_hash`). argv,
hostname, timestamps, and env-var values differ per launch; hashing them in would
mint a unique record per run and destroy dedup. Therefore:

| What | Where | Hashed into env identity? |
|---|---|---|
| Code manifest, deps, lockfile hashes, hardware (stable), stable settings | `execution_records` (as today, plus new fields) | Yes |
| argv, cwd, entrypoint, hostname, user, launcher chain, env-var names + allowlisted values, detected seeds, container image, start time | **`launch` block in `runs.metadata`** via existing RunPatch | No |

The `launch` block is a reserved key `runs.metadata["launch"]`, SDK-written,
documented in CONTRACT.md alongside the other server/SDK-written metadata
conventions. Precedent: `experiments.metadata` already carries server-written keys.
This is why the backend needs zero migrations.

Container image is *provenance, not correctness* — reaffirming the 2026-07-29
begin-state-bytes decision (a Dockerfile is a recipe, not a state).

## D2. Capture expansion (SDK `snapshot.py`)

Four new capture functions, composed into `Run.snapshot()`:

### `capture_process()` → `launch.process`
argv (scrubbed, see D7), cwd, entrypoint script (resolved `sys.argv[0]` /
exec target), hostname, username, start time, parent-PID chain walked to a
recognized launcher (`sbatch`/`srun`/`kubelet`/`containerd`/`dockerd`/shell), with
the launcher name recorded when detected.

### `capture_runtime()` → split across the two homes
Per-launch, into `launch.runtime`:
- **Env vars:** all *names*; *values* only for a safe allowlist —
  `CUDA_VISIBLE_DEVICES`, `WORLD_SIZE`, `RANK`, `LOCAL_RANK`, `MASTER_ADDR`,
  `MASTER_PORT`, `OMP_NUM_THREADS`, `PYTHONHASHSEED`, `SLURM_JOB_ID`,
  `NCCL_*` (names-with-values list extensible via
  `PROBE_ENV_ALLOWLIST=+VAR1,VAR2`). Values never captured outside the allowlist.
- **Container:** image/ID detection via cgroup v2, `/.dockerenv`, K8s downward-API
  env; provenance-tagged (`detected_via`), best-effort.

Machine-stable and identity-relevant, into `execution_records.hardware` (hashed):
- **OS:** platform, distro, kernel, glibc.
- **Accelerator stack:** CUDA runtime + cuDNN via `torch` if importable, else
  `nvcc`/`nvidia-smi`; existing GPU inventory unchanged.
- **CPU/RAM:** model, core count, total memory (joins GPU; stable per machine
  class, dedup-safe — two runs on identical nodes still share one record).

### `capture_determinism()` → `launch.determinism`
`PYTHONHASHSEED`; seed-like keys parsed from argv and `config` (`--seed`, `seed=`,
`*_seed`); framework introspection if importable (`torch.initial_seed()`, numpy
generator presence). Every entry tagged `detected` | `declared`. The declared path
is a documented convention: `config["seeds"] = {"torch": 42, ...}` (skills teach
it; no new API surface).

### `capture_dependency_files()` → snapshot manifest entries
Lockfiles by well-known name (`uv.lock`, `poetry.lock`, `requirements*.txt`,
`pyproject.toml`, `environment.yml`, `package-lock.json`, `Cargo.lock`)
force-included as `source="blob"` **even when dirty, untracked, or gitignored** —
today's silent-loss case. Size-capped (1 MiB/file default); inclusion and any cap
hit reported in the manifest `skipped`/`included` accounting like every other
entry. Their sha256s join `execution_records.deps` (identity: same lockfile bytes
⇒ same env contribution).

## D3. Automatic invocation

- **`probe exec RUN -- cmd…`** (CLI `main.py:2482`) snapshots by default before
  exec: process/runtime/determinism captured *for the child command* (argv = the
  command being launched, not the probe CLI), snapshot of cwd. This folds in
  Phase 1b of the (lost) 2026-08-03 snapshot plan. Opt out: `PROBE_AUTO_SNAPSHOT=0`.
- **SDK in-process:** `client.run()` auto-snapshots on open. Opt out via the same
  env var or `run(snapshot=False)`.
- **Idempotence:** re-snapshot of an unchanged tree is already content-deduped
  (`tree_sha256`, execution-record hash); repeated `run()` opens must not re-upload.
- **Prerequisite:** the `pushed_base` N+1 batching fix (TODOS.md P2; 2.6–6.6 s →
  64 ms measured) lands **before** this, since every run now pays snapshot cost.
- Capture failure logs, records honest markers, and continues. Never fails the run.

## D4. Completeness & enforcement

- **`probe run check`** grows named checks: `process`, `runtime`, `determinism`,
  `lockfiles`, `inputs_decision` (decision artifact present), `notes` (any
  note/decision recorded). States stay `unverified`/`complete`/`incomplete`;
  exit 2 on incomplete. `--verify` additionally resolves lockfile blobs, as it
  already resolves the code commit.
- **Completion warning (no strict mode — revised 2026-08-06):** when
  `run.finish("completed")` runs with capture enabled (`PROBE_AUTO_SNAPSHOT`
  not `0`) and `check_run` reports `incomplete`, the SDK warns with the
  `missing` list and a pointer to `probe run check`. It fires after the journal
  drain (so it never complains about writes the same finish delivers), never
  raises, and a failure of the completeness probe itself never breaks the
  close-out. There is no `PROBE_REQUIRE_COMPLETE`, no `--strict`: runs are
  never blocked on capture, per Mahit. Scripted enforcement, where wanted,
  belongs to the caller via `probe run check`'s exit code.
- **`completeness.missing`** markers in reproduce views extend 1:1 with the new
  checks (`mcp/contract.py` MissingMarker).

## D5. Pull surface (hierarchical)

### Server (research-os; the only backend code in this spec)

- **`GET /v1/runs/{id}/reproduce`** — one response assembling:
  run core (config, tags, description, notes, status, timestamps, `foreign_keys`,
  parent + relation); full execution record; `launch` block; snapshot manifest
  summary + a copyable restore command; lockfile and `inputs-decision` artifact
  contents (inline when small, refs otherwise — with size reported); note/decision
  artifacts (kind `note`, `inputs_decision`); lineage edges (stored + derived);
  per-span `env_ref` summary (span/trial granularity — a rollout that pinned its
  own environment is visible here); completeness verdict. Never silently
  truncated; overflow is REPORTED with the uncapped door named (existing bundle
  precedent, `_view_handoff`).
- **`GET /v1/experiments/{id}/reproduce`** — hypothesis, description, notes, tags;
  minted `experiment_versions` list; per-run reproduce *summaries* (id, short_id,
  status, completeness, env_ref, one-line description) with drill-down refs.
  `?version=N` resolves against a frozen manifest's pinned refs instead of live
  rows.

### MCP (research-os-agent)

- Run `view="reproduce"` (`mcp/service.py:_view_reproduce`) delegates to the run
  endpoint instead of client-side assembly; keeps atomic never-truncate semantics.
- **New** experiment-level `view="reproduce"` over the experiment endpoint.

### CLI (research-os-agent)

- `probe run reproduce RUN [--export FILE] [--materialize DIR]` — prints the
  assembled record; `--export` writes it as a portable JSON bundle (a *rendering*,
  never the source of truth); `--materialize` = `snapshot-restore` + lockfiles +
  inputs into a directory, ready to run.
- `probe experiment reproduce EXP [--version N]`.
- `probe experiment freeze EXP [--label L]` — ergonomic mint of the existing
  `experiment_versions` row (`POST /v1/experiments/{id}/versions`). No new backend.

## D6. Skill enforcement (research-os-agent `skills/`)

- **start-research-work:** the snapshot step becomes a *verify* step — exec/SDK
  now auto-snapshot; confirm with `probe run check`; snapshot explicitly only when
  launching outside `probe exec`/SDK. Recording `foreign_keys` (W&B, scheduler
  job, pod, storage) at launch stays mandatory.
- **track-research-work:**
  1. Record decisions, user overrides, and tools-behaving-differently as notes
     *when they happen* (existing trigger; wording tightened to name the new
     `launch`/determinism context as things worth annotating when surprising).
  2. **Claim gate:** before reporting a run done/handoff-ready, run
     `probe run check` and state the verdict verbatim. If `incomplete`: fix it, or
     say why not in the handoff note. Machine-checkable via exit code — not prose.
  3. At experiment completion/publication: `probe experiment freeze`.
- **capture-run-inputs:** core unchanged. Lockfiles drop off the manual checklist
  (automatic now); datasets, checkpoints, out-of-tree configs and the
  `inputs-decision.json` artifact remain its job.

## D7. Error handling & secret hygiene

- Every capture function returns `(data, errors)`; errors land in the launch
  block's `errors` list and surface in `run check` / reproduce views.
- Env-var values outside the allowlist: never captured, never logged.
- argv scrubbing: secret-shaped tokens (`--api-key=…`, `--token=…`, bearer-like
  strings, AWS-key shapes) replaced with `[redacted]` plus a `scrubbed: true`
  marker so the reader knows the command line is not verbatim.
- Lockfile capture respects the existing secret/exclude filters
  (`snapshot.py` secret list) — a `.env` matching a lockfile glob still loses.
- Non-git directories: all of the above works over the existing
  `capture_directory_manifest()` path.

## D8. Testing

- Unit per capture function with fixture `/proc`, env, and fake interpreters;
  scrubbing gets adversarial cases.
- Contract tests for both `/reproduce` endpoints (ASGI round-trip, the
  `test_ros_client_contract.py` pattern).
- Black-box: `probe exec` → child writes files/env → `probe run reproduce` returns
  argv, seeds, lockfile, env allowlist values; `run check` exit codes for each
  missing slot.
- Skill-level: fixture run exercising the claim gate (incomplete → exit 2 →
  handoff note must carry the reason).
- Live smoke against uvicorn + Docker Postgres before claiming done (standing
  rule: pytest alone is not enough for cross-repo features).

## Landing order

1. `pushed_base` batching (research-os-agent, standalone perf PR).
2. SDK capture expansion + launch block (research-os-agent; server accepts it
   already — metadata is an open dict).
3. `probe exec` / `run()` auto-snapshot + `run check` extensions + strict mode.
4. research-os `/reproduce` endpoints + CONTRACT.md documentation of the `launch`
   reserved key.
5. MCP view delegation + experiment view; CLI `reproduce`/`freeze` verbs.
6. Skill updates (last — they document behavior that now exists).

Steps 2–3 are useful with no backend change; 4–5 are useful without 6.

## Open questions

- Probe team-memory (Slack/Linear) was not searchable this session (MCP OAuth
  pending) — before implementation, run `search_knowledge` for prior art on
  launch capture and any incumbent conventions for `runs.metadata` keys.
- Exact env-var allowlist contents — seed list above, finalize in review.
- Whether `?version=N` on experiment reproduce should 409 or degrade when pinned
  runs were force-deleted (410-with-metadata precedent suggests degrade + report).
