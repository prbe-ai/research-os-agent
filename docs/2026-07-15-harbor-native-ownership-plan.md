# Harbor-native ownership plan (Osmosis)

**Date:** 2026-07-15 · **Status:** Phase 0 in progress · **Owners:** Mahit (+ Richard's infra design)

The decision: although Andy (Osmosis) framed us as a bolt-on, for the Harbor case we
want to **own as much natively as possible** — sandbox binaries in our store, the
step↔sandbox join as our schema, the forensics UI as our surface. Their compute,
networking (RoCE/IB), and orchestration (SkyPilot/Temporal) stay theirs; the
*record* of what happened becomes ours.

## Why (from the Osmosis calls, Jun 29 + Jul 7)

- Lineage is their #1 gap: *"step 600 the model collapsed → look at step 599 and
  601 at the harbor environment"* — today that's manual S3 UUID archaeology.
- Their dream API is literally our schema: *"step number is the key, the value is
  all the metrics in dictionary form as well as binaries of the sandbox execution."*
- MLflow (self-hosted) holds model metrics only; Harbor trial artifacts are
  manually copied to S3 with no schema; Grafana is "one of three SDKs we maintain."
- W&B rejected on price ($1/GPU·hr) and on trajectories that can't hold
  intermediate file states.

## How their pipeline actually works (internal sessions, Jul 9–12)

Miles trainer → POST `/run` per (prompt, sample) → **private Harbor fork**
(`miles_agent_server.py`) turns each into a trial → SkyPilot provisions an
ephemeral VM on their k8s → agent runs against the SGLang endpoint → verifier
scores → reward returns to Miles; artifacts are hand-copied to S3; **the VM is
destroyed** (anything not copied out dies with it). Harbor is the *requester*,
SkyPilot the *provisioner* — capture hooks differ per layer.

References: agent sessions `36a5336b` (chain analysis), `09736fdc` (probe-research
cluster + data-model design incl. `sandbox_executions`), `21227e8a` (trial output
contract mapping), `019f53c9` (capture-point decision: Harbor bridge host).

---

## Phase 0 — contract + blocking API gaps (days) ← CURRENT

1. **Upload flow carries `kind`/`meta`** (research-os). `POST
   /v1/runs/{id}/artifacts/uploads` hardcodes `kind="file"` and drops meta; you
   cannot own sandbox binaries you cannot label. `apply_artifact` already accepts
   both — this is request-model plumbing, not schema work.
2. **Artifact list filters** (research-os). `GET /v1/runs/{id}/artifacts` gains
   `?kind=&step_from=&step_to=`. `kind` and `step_index` are already real columns —
   no migration; the route just never filters. Step-599-vs-601 forensics needs it.
3. **`harbor_trial` manifest schema v1** (below) — frozen here so the SDK
   connector, importer, and dashboard all speak one shape.
4. Client follow-through (research-os-agent): regen models, `log_artifact`
   passes `kind`/`meta` on the upload path (drop the once-warning),
   `list_run_artifacts(kind=…, step_from=…, step_to=…)`.

## Phase 1 — SDK-native trial capture (1–2 wks)

- `probe.connectors.harbor.capture_trial(run, trial_dir, step_index=…)`:
  parse `result.json`/`reward` → metrics at step; `trajectory.json` → rollout span
  (+ children); every file content-addressed to R2 via the presign flow (CAS
  `have` dedup pays off hugely — env files repeat across thousands of trials);
  one manifest artifact `kind="harbor_trial"` ties it together.
- CLI: `probe trial add RUN <trial-dir> --step N`.
- **S3 backfill importer** (`probe ingest harbor-s3 s3://…`) — value on historical
  data with zero code changes on their side; deliberately independent of the
  private fork so we can demo before asking for integration.
- Demo milestone: step-600 collapse forensics on Osmosis-shaped data.

## Phase 2 — capture at source, zero-code (2–4 wks)

- **Harbor-bridge hook** (the `019f53c9` decision): callback in their fork's
  request loop — after verifier returns, stream the trial dir to Probe ingest
  with `PROBE_RUN_ID` + Miles `rollout_id` as the step key. Their manual S3 copy
  disappears; bytes land in our R2 first. Needs Andy's buy-in (private fork).
- **Miles plugin** (`miles_plugins/`): opens the run, maps `rollout_id → step`,
  logs trainer metrics; MLflow importer keeps their mirror in sync during
  transition instead of demanding a cold switch.
- **Environment collector**: VM id/region/CPU/RAM/GPU/timestamps captured at
  sandbox creation (`environment.json`) — creation-time is the only reliable
  moment, since the VMs are ephemeral and "members of nothing."

## Phase 3 — the platform surface (1–2 mo)

- Deploy the designed `probe-research` cluster (managed Helm mode): CNPG
  (control/experiment/kb), per-tenant R2 (`prbe-<slug>`), RLS tenancy,
  Barman WAL→R2. (`09736fdc` has the full design; `sandbox_executions` becomes a
  first-class table: `experiment → runs → {metrics, steps, sandbox_executions,
  artifacts → R2}`.)
- Dashboard: run timeline (metrics + trial gallery per step); **step-N vs N+1
  sandbox diff** (cheap via CAS hashes); trajectory viewer; verifier output
  rendering. This is "W&B charts + container state in one view," verbatim his ask.
- MCP: `research_get view="sandbox"`, `research_trace_file` over trial manifests.
- Pricing: per-researcher (~$1K/mo signal) + metered storage, positioned against
  the $1/GPU·hr complaint.

**Risk ledger:** (a) private fork ⇒ Phase 2 needs Andy's cooperation — Phase 1's
backfill importer shows value first; (b) they self-host MLflow *for privacy* ⇒
lead with per-tenant buckets + RLS; the air-gapped Helm mode (Anthrogen) is the
credible fallback; (c) storage scale (trials × steps × files) ⇒ CAS dedup +
lifecycle tiering from day one.

---

## `harbor_trial` manifest schema v1

### Environment-agnosticism (the design constraint)

Different companies spin Harbor sandboxes differently — and that's fine, because
**Harbor's trial layer, not the environment, owns the output contract**. All six
upstream providers (Docker, Daytona, Modal, E2B, GKE, Runloop) implement one
`BaseEnvironment` interface (lifecycle, upload/download, exec, capability flags);
for non-mounted providers Harbor itself `download_dir()`s results after
execution; custom environments (e.g. Osmosis's SkyPilot-backed fork) subclass
`BaseEnvironment` and register in the `EnvironmentFactory` without touching the
trial output. Sources: harborframework.com concepts/environments,
api/trial-result; laude-institute/harbor.

Therefore the schema:

1. **anchors on the trial contract** (`TrialResult`: trial/task identity +
   checksum, agent info, verifier reward, four phase timings, exception info) —
   stable across providers;
2. **treats the environment as opaque metadata** — a free-string `type` plus
   capability flags, never structural;
3. **is manifest-based and fork-tolerant** — it enumerates the files that exist
   with optional well-known roles; nothing beyond trial identity is required;
   unknown files pass through as plain artifacts with `role: "other"`.

### Shape

One artifact per trial: `kind="harbor_trial"`, `step_index=<training step /
rollout_id>`, `span_id=<rollout span>`, `meta`:

```jsonc
{
  "schema_version": "1.0",
  "trial": { "name": "swe-fix__bwrhe3y", "task_name": "swe-fix",
             "task_checksum": "…", "trial_uri": "…" },
  "agent": { "name": "…", "version": "…", "model": {"name": "…", "provider": "…"} },
  "verifier": { "reward": 0.0 },
  "phases": {              // started_at/finished_at per phase (TrialResult)
    "environment_setup": {}, "agent_setup": {},
    "agent_execution": {}, "verifier": {} },
  "environment": {          // OPAQUE — never structural
    "type": "skypilot-fork", "capabilities": {"gpus": true}, "collected": {} },
  "exception": null,        // ExceptionInfo when the trial errored
  "source": {                // where this came from (importer vs live hook)
    "mode": "backfill|bridge-hook", "s3_prefix": null, "rollout_id": 612 },
  "files": [                 // the manifest — every file is its own CAS artifact
    { "role": "config",     "path": "config.json",      "artifact_id": "…" },
    { "role": "result",     "path": "result.json",      "artifact_id": "…" },
    { "role": "trajectory", "path": "trajectory.json",  "artifact_id": "…" },
    { "role": "agent_log",  "path": "logs/agent/…",     "artifact_id": "…" },
    { "role": "verifier",   "path": "logs/verifier/…",  "artifact_id": "…" },
    { "role": "output",     "path": "output/report.pdf","artifact_id": "…" },
    { "role": "other",      "path": "whatever-else",    "artifact_id": "…" }
  ]
}
```

Known roles: `config | lock | result | trajectory | reward | agent_log |
verifier | output | other`. Child files upload as ordinary `kind="file"`
artifacts (CAS-deduped, same `step_index`/`span_id`); the manifest references
them by `artifact_id`. Nothing here depends on which provider ran the sandbox.

### Open questions

- Do huge `logs/agent` trees get tarred (`role: "agent_log_archive"`) above a
  file-count threshold, or always per-file CAS? (Lean: per-file for dedup, tar
  above N=200 files.)
- `sandbox_executions` table (Phase 3) vs manifest-artifact (Phase 0–2): the
  manifest ships now and migrates cleanly into the table later — the table can be
  populated from manifests.
- Trajectory formats vary (ATIF vs fork-specific): store raw always; parse into
  spans only for formats we recognize (`atif@1`), record `trajectory_format`.
