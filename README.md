# probe-research (`probe` SDK/CLI + `probe-research` plugin)

CLI + SDK client for **Probe Research**, Probe's experiment-tracking platform. It is a
thin client over the v3 ingestion contract (`CONTRACT.md` in the Probe Research backend).
Implemented experiment calls map onto real endpoints.

## Two client surfaces

Probe Research exposes experiment tracking through two separate surfaces over the same backend, for two different workflows:

- **`probe` — SDK + CLI (non-agent).** A Python library (`import probe`) and the `probe` command-line tool for integrating with existing setups and manual experimentation. Drop it into a training script or pipeline to record runs, metrics, spans, and artifacts. No agent required.
- **`probe-research` — plugin: skills + MCP (agent-centric).** Installed into a coding agent (e.g. Claude Code). Its skills teach the agent the experiment workflow, its read-only MCP server lets the agent query experiment state, and writes flow through the `probe` CLI. This is the surface for agent-driven research loops such as Anthrogen.

Same backend, two entry points: humans-in-code reach for the SDK/CLI; agents-in-the-loop use the plugin.

## Package boundaries

```text
src/probe/
├── sdk/       # typed client, uploads, local capture, session adapter ABI
│   ├── fluent.py    # probe.init/log/finish — ambient run, contextvar-bound
│   └── analysis.py  # client.compare(): N runs aligned on step
├── cli/       # `probe`: thin shell over the SDK
└── mcp/       # `probe-research-mcp`: strictly read-only tools and resources
skills/
├── start-research-work/
└── track-research-work/
```

The SDK is the implementation. The CLI, MCP source adapter, future hooks,
Python experiments, and passive platform integrations all use it. CLI and SDK
therefore have capability parity; they differ only in ergonomics.

| Surface | SDK | CLI | Intended caller |
|---|---|---|---|
| Experiment upload | `Client.run`, `Run.log/span/log_artifact/snapshot/link/execute`, `Client.events`, `Client.promote` | `run`, `log`, `span`, `artifact`, `snapshot`, `link`, `exec`, `event`, `promote` | Researchers, agents, notebooks, training/platform code |
| Ambient upload | `probe.init/log/log_hw/log_artifact/span/finish`, `probe.active_run` | n/a (a CLI call has no ambient run) | Training scripts, and library code with no handle to pass |
| Session adapter | `Client.sessions.attach/checkpoint/detach` | `probe hook session ...` | **Future deterministic hooks/broker only** |
| Artifact reuse check | `Client.list_anchored(Anchor.SHARED, prefix=…)` + `list_artifact_versions`, normally behind MCP | `probe artifact versions` | Agent through read-only MCP |
| Passive ingestion | `Client.ingest` | No convenience command yet | Install-once platform integration |
| Read plane | SDK reads used by `probe.mcp`; `Client.compare` for N-run analysis | `get`/`bundle` diagnostics | MCP for agents; CLI for humans/scripts |

Session commands do not upload metrics or experiment outputs. They correlate a
coding-agent session with a run and checkpoint redacted transcript metadata.
Conversely, `event add` is normal experiment knowledge upload even when a hook
eventually calls it. No hooks are installed in this release.

## Install

```bash
pip install -e ".[dev]"     # from this directory
```

## Auth

```bash
probe login       # browser device flow (RFC 8628 + PKCE) — the default; nothing to paste
```

Air-gap paste path: `probe login --token probe_pat_xxxxxxxx` (verified via `GET /v1/me`);
`probe login --endpoint-only --base-url …` saves the endpoint without minting a token.
Both write `~/.config/probe/config.json`.
Or set env: `PROBE_BASE_URL`, `PROBE_TOKEN` (user token, `/v1`), `PROBE_INGEST_TOKEN`
(ingest token, `/ingest`), `PROBE_HMAC_SECRET` (optional body-signature secret).
SDK-created runs heartbeat every 60s so the server can reap crashed ones;
`PROBE_HEARTBEAT_SECONDS` tunes the interval (`<=0` disables).

You can also skip `probe login` entirely: the first `client.run()` /
`probe run start` with no token triggers the same browser approval inline (TTY only)
and persists the result. Disable with `PROBE_AUTO_LOGIN=0`; headless/CI keeps the
crisp `AuthError` and should set `PROBE_TOKEN`.

The MCP server prefers `PROBE_MCP_TOKEN`, which should be a separately minted
read-only token. It falls back to `PROBE_TOKEN` for local development, but exposes
no mutation tools.

On rented compute (RunPod) with no standing config, the `/start-research-work` skill
seeds `PROBE_TOKEN` at session start.

## SDK (agent-driven / interactive)

Two forms, same implementation. The module-level one when you want to log from
code that has no handle to pass around:

```python
import probe

probe.init(
    project="folding",
    experiment="dockq-sweep",
    hypothesis="temp 0.7 wins",
)
probe.log({"loss": 0.42, "dockq": 0.71}, step=42)    # from anywhere, any thread
probe.finish()
```

`probe.init()` takes everything `client.run()` does and returns the same `Run`
handle, so `with probe.init(...) as run:` works and the rest of the API is one
attribute away. The binding is a contextvar backed by a process default: a worker
thread finds the run (a bare contextvar would not — threads start with an empty
context), while a second `init()` inside a thread or block *shadows* rather than
hijacking the outer one. That is the part of `wandb.init()`'s global that
silently corrupts concurrent runs. `probe.active_run()` returns the current
binding, and a script that exits without `finish()` is closed at exit as
`completed` / `failed` / `canceled` rather than left to the crash reaper.

The explicit form has no ambient state at all:

```python
import probe

client = probe.Client()  # resolves creds from env / `probe login`

# run() resolves by default. `hypothesis=` is the one opt-in to creation, so the
# FIRST run in a new experiment says what you expect to see:
run = client.run(experiment="dockq-sweep", hypothesis="temp 0.7 wins",
                 name="run-1", project="folding",
                 description="DockQ baseline at temperature 0.7",
                 source="runpod", external_id="rp-9931")

# …and every run after that is bare — the experiment already exists, and its
# hypothesis is first-write-wins so reopening never rewrites it:
run = client.run(experiment="dockq-sweep")

# No hypothesis and no experiment? That is a project-direct run, which is the honest
# home for work with none — better than an experiment named after your cwd:
run = client.run(project="folding")

# `name` defaults to a timestamp (the server adds a petname short_id). A slug that is
# a near-miss of an existing one is REFUSED, not created: a warning is invisible from
# a training loop. Creation is SDK-only — `probe run start` never creates, because on
# the CLI the slug is hand-typed every time, which is where typos come from.

client.update_run(run.id, name="DockQ baseline",
                  description="Stable reference run")
child = run.child("retry-1", relation="retry",
                  description="Retry after fixing the data loader")
run.snapshot()                                   # non-disruptive git + deps + GPU capture
run.link(wandb_run_id="abc123", s3_prefix="s3://x/y")

for step in range(100):
    run.log({"loss": ..., "dockq": ...}, step=step)     # POST /v1/runs/{id}/metrics

# Omit `step` and it auto-increments per metric kind, so the bare loop shape
# still produces a curve. `step=None` explicitly means no step at all
# (wall-clock axis) — that is what the CLI and the passive importers pass.
for batch in loader:
    run.log({"loss": loss})                             # steps 0, 1, 2, …

# Values of any type are accepted. Numbers (and bools, numpy scalars, 0-d
# tensors) become metric points and plot; strings, dicts, lists and None go into
# that step's record and read back through the trajectory view.
run.log({"loss": 0.4, "phase": "eval", "cfg": {"lr": 3e-4}})

# Below-run coordinates: `coords` are bounded grouping axes (series identity:
# rank/split/..., never a per-sample id), `labels` per-sample drill-down ids
# (point identity only). Everything logged inside the unit carries them, and
# nested units merge (child wins per key).
with run.unit(coords={"rank": 0}, labels={"sample": 3}):
    run.log({"reward": 0.71}, step=12)   # -> dimensions={"rank": 0}, labels={"sample": 3}

with run.span("rollout", name="rollout-0", step_index=1) as span:  # trajectory span
    span.attributes["reward"] = 0.8      # closes with ended_at + a terminal status,
    ...                                  # `failed` if the body raises; nests inside
run.log_artifact("final.sif", uri="r2://bucket/final.sif", kind="artifact")
run.finish()                                     # flushes spool, sets status+ended_at
```

Structured knowledge and local process capture use the same SDK:

```python
run.execute(["python", "train.py", "--config", "dockq.yaml"])
client.events.add(run.id, "decision", "Use DockQ scorer v3", evidence_refs=["tool:91"])
report = client.check_run(run.id)
```

Data writes are **fail-open** by default: on failure they spool to disk
(`~/.local/state/probe/spool`) and return, never blocking the training loop. `run.finish()`
(or `probe flush`) replays the spool. Appends and queue rewrites are fsync'd and
atomic. On rented compute, put the queue on durable storage with
`PROBE_SPOOL_DIR=/shared/probe/spool` or `probe --spool-dir /shared/probe/spool …`.
Pass `strict=True` to make a write raise.

## Miles tracking backend (`probe.connectors.miles`)

Drop-in wandb-parity backend for [Miles'](https://github.com/MoonshotAI/miles)
`TrackingManager`. Zero miles commits: the registry is a plain dict checked
against args flags at init, so activation is two lines in your launcher,
before `init_tracking(args)`:

```python
from probe.connectors.miles import register
register(args)          # registers the backend + sets args.use_probe
```

Every `TrackingManager.log()` lands with its own step counter (`train/step`
and `rollout/step` map to `step_index` per key, the counter entry itself is
stripped), values arrive after Miles' DP-rank reduction — exactly what wandb
sees — and the run declares its labeled-point plan (`num_rollout x
rollout_batch_size x n_samples_per_prompt`) so later per-sample capture never
trips the server's default budget mid-training. Config: `PROBE_BASE_URL` /
`PROBE_TOKEN`, optional `args.probe_experiment` / `args.probe_run_name`
(fall back to the wandb names). Fail-open end to end: a broken tracker never
costs a training step; non-finite values are dropped per-point. Per-rank and
per-sample detail is enabled without replacing aggregate logging by passing:

```text
--custom-rollout-log-function-path probe.connectors.miles.per_sample_rollout_log
```

The hook logs labeled sample points through the same durable queue as Miles'
aggregate `tracking.log()` calls. Every point carries `metric_scope=sample`,
the Miles sample id, an optional group id, and—when the sample carries Harbor's
returned capture `external_key`—the exact deterministic rollout span. The
dashboard can therefore separate aggregate and sample points while resolving
sample → trial → trajectory/sandbox without a Miles-core change.

Reward and effective response length are captured by default. Applications can
publish arbitrary numeric sample measurements without connector changes by
putting an inline dictionary on the sample:

```python
sample.metadata["probe_metrics"] = {
    "agent/input_tokens": input_tokens,
    "agent/output_tokens": output_tokens,
    "quality/custom_score": score,
}
```

If values already live elsewhere on the Miles sample, declare metric-name to
dotted-path mappings on the same args object Miles sends to RolloutManager:

```python
args.probe_sample_metrics = {
    "agent/observed_tokens": "metadata.agent_metrics.observed_tokens",
    "agent/turns": "metadata.agent_metrics.turns",
    "agent/tool_calls": "metadata.agent_metrics.tool_calls",
}
```

Stock Miles launchers that do not expose arbitrary rollout args can put the
same inline mapping in a tiny importable hook module instead:

```python
# my_project/probe_metrics.py
from probe.connectors.miles import make_per_sample_rollout_log

per_sample_rollout_log = make_per_sample_rollout_log({
    "agent/observed_tokens": "metadata.agent_metrics.observed_tokens",
    "agent/turns": "metadata.agent_metrics.turns",
    "agent/tool_calls": "metadata.agent_metrics.tool_calls",
})
```

Then set `--custom-rollout-log-function-path` to
`my_project.probe_metrics.per_sample_rollout_log`. This stays entirely outside
Miles source while preserving the shipped hook's durable queue, sample labels,
and Harbor span linkage.

Missing, non-numeric, boolean, and non-finite values are omitted; an explicit
numeric zero is retained. Configured paths override a same-named
`metadata["probe_metrics"]` entry. The run reserves 1,024 sample metric points per
sample by default; set `args.probe_sample_metric_budget` higher when a sample
schema intentionally exceeds that.

Per-rank detail otherwise rides the capture-at-source arc (`run.unit` +
`capture_trial`). Upstreaming a native `--use-probe` flag into a miles fork is
optional polish (registry docstring's own recipe).

## SDK (install-once / passive push)

```python
client.ingest(
    project_slug="protein-folding",
    experiment_slug="dockq",
    run={"name": "r1", "source": "temporal", "external_id": "wf-1", "status": "running"},
    metrics=[{"kind": "model", "key": "loss", "value": 0.5, "step_index": 1}],
    batch_id="deadbeef",          # idempotent redelivery
)
```

One idempotent push (bearer ingest token + optional HMAC), keyed on
`(customer_id, source, external_id)`.

## SDK (reading runs back for comparison)

```python
comparison = client.compare(experiment_id=exp_id, keys=["dockq"])
aligned = comparison.aligned("dockq")

for label, values in aligned.values.items():
    plot(aligned.steps, values, label=label)     # or aligned.to_pandas()
```

Name the runs with `run_ids=[...]` or select them with the filters `list_runs`
takes (`experiment_id=`, `group_id=`). One `POST /v1/series/query` per 50 runs —
more than that batches rather than truncating, because silently dropping runs 51+
reads as "these are all of them". Columns are labelled by the server's petname
`short_id`; runs of differing length keep `None` holes rather than being cut to
the shortest, since differing length is usually what is being compared. pandas is
optional and only touched by `.to_pandas()`.

There is no separate read client. `wandb.Api()` is a distinct object because W&B
has two transports (a service process for writes, GraphQL for reads); one REST
transport does not need the split, so this is a shaping layer on `Client`.

## CLI (`probe`)

```bash
probe project create folding
probe experiment create dockq --hypothesis "temp 0.7 wins" --project folding
RUN=$(probe run start --experiment dockq --name run-1 \
        --project folding --source runpod --external-id rp-9931 \
        --description "DockQ baseline at temperature 0.7")
probe project patch folding --name "Protein folding" --description "DockQ studies"
probe experiment set EXPERIMENT_ID --name "DockQ sweep" --description "Temperature sweep"
probe run set $RUN --name "DockQ baseline" --description "Stable reference run"
probe snapshot $RUN
probe link $RUN --set wandb_run_id=abc --set gpu_job=rp-9931
probe log $RUN loss=0.42 dockq=0.71 --step 42
probe span add $RUN --type rollout --name rollout-0 --step 1
probe artifact add $RUN ./final.sif --kind artifact
probe event add $RUN --kind decision --statement "Use DockQ scorer v3" --evidence tool:91
probe exec $RUN -- python train.py --config dockq.yaml
probe run check $RUN
probe run end $RUN --status completed
probe bundle $RUN            # read: run + series + artifacts
```

### Harbor trial capture (`probe trial`)

Capture a Harbor trial directory into a run, keyed to the training step —
the sandbox↔step join (see `docs/2026-07-15-harbor-native-ownership-plan.md`
for status: what's shipped vs parked):

```bash
# rollout span + reward metric + labeled CAS file uploads + kind=harbor_trial
# manifest; a recognized trajectory format (ATIF v1.x built in) also expands
# into turn/tool_call spans under the rollout span
probe trial add $RUN jobs/my-job/trials/swe-fix__x1 --step 600 --env-type skypilot-fork
probe trial add $RUN <dir> --step 601 --no-expand      # raw-only capture
# Copy/checksum a host trial tree onto a durable volume without touching the network.
probe trial stage <host-trial-dir> --to /shared/probe/trial-601 \
  --expect result.json --expect lock.json
# Retry one or every Miles bridge request. The descriptor supplies run/step/correlation.
probe trial export /shared/probe/trial-601/export-request.json
probe trial drain /shared/probe/captures
probe trial watch /shared/probe/captures --interval 5
# Bind descriptors produced during an offline run initialization:
probe trial drain /shared/probe/captures --run "$PROBE_RUN_ID"
# retroactively expand a stored trajectory (e.g. after a fork's parser ships);
# deterministic span ids make this idempotent — re-runs upsert, never duplicate
probe trial expand $RUN <manifest-artifact-id> --max-spans 0
```

Query it back: `client.list_run_artifacts(run_id, kind="harbor_trial",
step_from=599, step_to=601)`. Fork parsers plug in via
`probe.connectors.atif.register_trajectory_parser("their-format", fn)`;
unknown formats are captured raw (never rejected) and expanded later.

`probe trial stage` and the `probe-harbor-export/1` consumer keep an atomic
`.probe-capture.json` beside the durable trial bytes. Its collection status is
separate from remote-upload status, so an exporter outage leaves a precise,
retryable list of unconfirmed files instead of losing their paths. Stable
external keys and span IDs make retries update the same rollout; arbitrary
Miles/Osmosis correlation fields are preserved under the `harbor_trial`
manifest's `source.context`. The exporter additionally promotes `sample_id`
and `group_id` to the Probe point labels `sample` and `group`, respectively,
on the reward and `harbor_trial` manifest. Those labels preserve distinct
same-step samples without creating a separate metric series.

**Trajectory sources — Harbor-first, by decision.** Today trajectories enter
Probe through Harbor's on-disk contract: ATIF-supporting agents write
`trajectory.json` into their logs dir, the harness delivers it at
`<trial_dir>/agent/trajectory.json` (the location Harbor's own viewer reads),
and `parse_trial`/`capture_trial` pick it up from there — raw bytes always
stored, recognized formats expanded into turn/tool_call spans, unknown formats
expandable retroactively via `probe trial expand`. Emission is per-agent
opt-in upstream (`SUPPORTS_ATIF`; e.g. the oracle agent emits nothing), so
absence is a normal, captured state. "Traditional" trajectory tracking — live
SDK/OTel-style span streaming from instrumented agent code, the W&B
Weave/MLflow-Tracing model, with no file contract at all — is planned as the
second door: the server-side turn/tool_call span rails it needs already exist,
so it is an SDK-instrumentation arc, not a schema change. For now the scope is
deliberately the Harbor framework.

The completeness claim is intentionally bounded: it covers declared regular
files in the **host Harbor trial directory**. Public Harbor tears down the
sandbox before `Trial.run()` returns, so a post-run SDK consumer cannot know
about undeclared state Harbor never materialized. The ledger reports that state
as unknown, inventories explicitly declared missing files, and treats hidden
files/symlinks as visible skips. A true pre-teardown guarantee requires the
producer or environment implementation to invoke durable collection from its
lifecycle hook.

The following commands are reserved for future hook configuration and are not
part of the normal researcher workflow:

```bash
probe hook session attach RUN --session-id SESSION --transcript-path PATH --cwd DIR
probe hook session checkpoint RUN --session-id SESSION --transcript-path PATH --reason pre_compact
probe hook session detach RUN --session-id SESSION --reason session_end
```

They currently encode session links in `run.metadata.agent.sessions[]` and
transcript checkpoints as redacted local-reference artifacts. Until managed
artifact upload exists, transcript portability remains explicitly false.

## Read-only MCP server

Run the stdio server with `probe-research-mcp`. It exposes **three** tools, plus five
deprecated aliases that are removed next release:

| Tool | Answers |
|---|---|
| `browse_research` | "What exists here?" — the structured project → experiment → run tree |
| `search_knowledge` | "Find things about X" — one-index exact+semantic search with per-result provenance |
| `get_entity` | "Show me this thing" — one entity through a purpose-shaped `view` |

`research_context`, `research_search`, `research_get`, `research_compare` and
`research_resolve` have been REMOVED. They answered as deprecated aliases for one
release; that window has closed. Calls to them now fail as unknown tools — use the
three above.

**Thin harness, fat skills.** Coverage grows through `get_entity`'s `view` and `filters`
parameters, never through more tools. `browse_research` is the one addition that cleared
that bar: it answers a question the others structurally cannot, because search ranks by
relevance to a query and therefore needs you to already know what to search for.

`get_entity(ref, view=..., filters=..., token_budget=..., cursor=...)`, where `ref` is
`run:<id>`, `experiment:<id>`, `artifact:<name>`, `project:<id>`, `group:<id>`, or a bare id:

| Kind | Views |
|---|---|
| run | `card` · `trajectory` · `metrics` · `artifacts` · `reproduce` · `handoff` · `lineage` · `events` |
| experiment | `card` · `artifacts` · `lineage` · `groups` · `versions` |
| artifact | `card` · `versions` |
| project, group | `card` |

`card` (the default) returns `available_views` for that entity, so one call tells you what
else you can ask for — the matrix above is documentation, not something to memorise.
An unrecognised ref kind is rejected outright rather than guessed at: the resolver used to
try every getter in turn, so a retired kind surfaced whichever backend parse error came
first instead of saying the kind was gone.

Artifacts resolve by NAME, because the reuse check has a name and not an id — and because
no `GET /v1/artifacts/{id}` route exists. The lookup runs against the SHARED, lab-wide
level, which is where an official artifact is promoted to and the nearest thing to the
tenant-unique names the retired asset registry enforced.
`get_entity(ref="artifact:<name>", view="versions", filters={"requirement": ">=2"})` is
where `research_resolve` went. A name that does not exist raises not-found; a name that
exists with no satisfying version returns `state="no_match"` **with the versions that do
exist**, so you can see the real ceiling. Requirements match monotonic integers and labels,
not semver — `">=2.0"` is rejected rather than silently matching nothing. A name carried by
more than one shared artifact is a 422 naming both ids: the duplication this check exists
to prevent has already happened, and picking one silently would compound it.

`trajectory` reads a run's spans (the run bundle carries span_type *counts* only). `metrics`
returns series summaries, and `filters={"key": "<key>"}` drills to raw points. `reproduce`
resolves `env_ref` through its execution record. `token_budget` bounds the row-shaped part
of a view and hands back a `next_cursor`; `reproduce` is atomic and reports
`token_budget_exceeded` rather than truncating a manifest into something that reproduces
nothing.

There is no trace-file tool: no backend trace index has ever existed, so it answered
`matches: []` to every query, which agents read as "this file has no lineage". To trace a
path/URI/hash, use `search_knowledge` (its exact channel matches artifacts) and follow
`get_entity(view="lineage")`.

MCP reads through the Probe Research API—never directly from Postgres or R2. Its
logical sources are control identity/tenant scope, the structured experiment
store, the artifact/manifest registry, the one-index search door (`POST /v1/search`:
exact SQL channel + the KB engine's semantic channel; search capabilities are
discovered against the live backend with one cached probe), and object-store
resource pointers returned by the API. W&B, RunPod, Kubernetes,
Git, and local transcript paths are not live MCP sources; adapters upload their
identifiers and evidence first.

## Skills

Two skills, split by moment rather than by entity. `probe run start` opens a run in
an experiment that already exists, or project-direct with no experiment at all; it
never creates. Creation on the CLI is always `probe project create` /
`probe experiment create`.

- `start-research-work` covers that call: orient against what already exists and
  what is already running, create the project and experiment explicitly, resolve
  artifacts before creating them, and snapshot code + env before launch.
- `track-research-work` covers everything after the run exists: metrics, spans,
  artifacts, notes, version pinning, reading back what actually landed, and closing
  with the real lifecycle outcome. Its `reference.md` holds the artifact
  command syntax, the publication sequence, and project admin.

Reuse hooks are deliberately deferred. `start-research-work` contains the
reuse-before-create rule; deterministic enforcement can be added later without
changing the SDK, CLI, MCP, or skill contracts.

## What maps to what (v3 endpoints)

| Client call | Endpoint |
|---|---|
| `client.run()` / `run.child()` | `POST /v1/experiments`, `POST /v1/experiments/{id}/runs` |
| `run.log()` / `run.log_hw()` | `POST /v1/runs/{id}/metrics`, and `/steps` for non-numeric values |
| `run.span()` / `run.step()` | `POST /v1/runs/{id}/spans` \| `/steps` |
| `run.log_artifact()` | `POST /v1/runs/{id}/artifacts` |
| `run.link()` | `PATCH /v1/runs/{id}` (merges `metadata.foreign_keys`) |
| `run.finish()` | `PATCH /v1/runs/{id}` |
| `client.events.add()` | `POST /v1/runs/{id}/artifacts` (`kind=research_event`, v3 encoding) |
| `client.sessions.*` | `PATCH /v1/runs/{id}` + transcript artifact metadata (hook ABI) |
| `client.ingest()` | `POST /ingest/v1/runs` |
| `client.run_bundle()` / `run_lineage()` | `GET /v1/runs/{id}/bundle` \| `/lineage` |
| `client.search()` (used by `research_search`) | `POST /v1/search` (exact+semantic, sectioned) |

## v0.4.0.0 ingestion fold-in (Phase 1)

Most earlier gaps are closed by Probe Research v0.4 (PR #13). Now wired:

- **Real metric dimensions.** `log_hw(..., device=3, host="n1")` sends `dimensions`
  (fold #9); `log(..., dimensions={...})`. No more key-encoding.
- **Presign artifact upload.** `log_artifact(path=...)` runs presign → PUT to R2 →
  confirm (fold #16), carrying `kind`/`meta` so byte uploads are labeled like
  reference artifacts (Harbor-ownership Phase 0). Fails open to a reference on error.
- **Execution records.** `snapshot()` posts a content-addressed `execution-record`
  (fold #7); `client.execution_record(...)`.
- **Artifact versions.** `client.list_artifact_versions()` + `create_artifact_version()`.
  The separate asset registry that used to sit here was folded into artifacts
  (research-os #143/#144): an artifact is a named thing in a container with a chain
  of immutable versions, which is what an asset was.
- **Experiment versions.** `client.experiment_version()` mints the immutable manifest
  (fold #6). This replaces the removed run-level `promote`.
- **Lineage edges.** `client.add_edge()` / `run.edges()` (fold #2).
- **foreign_keys.** first-class on the ingest path (`run['foreign_keys']`, fold #8) and
  surfaced on reads (`run.foreign_keys`, `run.short_id`).
- **Events read.** `client.events.list()` / `for_run()` (server-emitted lifecycle log).
  Research notes moved to `client.notes.add()` (stored as `kind="note"` artifacts).
  They anchor to a run, an experiment or a PROJECT — the project anchor is what lets
  a decision be recorded before any run exists. `client.notes.list()` /
  `probe note list` read them back with `supersedes` resolved.

### Remaining

- **MCP semantic/KB search** is now wired to `POST /v1/search` (workspaces+kb
  fold-in) with an honest keyword fallback on older backends; transcript
  evidence is not indexed yet. **Session hooks** remain later work.
- **Harbor-native ownership Phases 1–3** (trial capture connector, capture-at-source,
  platform surface): see `docs/2026-07-15-harbor-native-ownership-plan.md`.

(Previously listed here and since shipped: `RunPatch` `foreign_keys`/`env_ref` parity,
asset `materialize`, upload `kind`/`meta`, and server-side artifact list filters
`?kind=&step_from=&step_to=`.)

## Typed models (generated from the OpenAPI contract)

Request/response models are generated from the backend's OpenAPI schema, not
hand-written, so the client cannot silently drift from the contract. The write
paths (`log`/`span`/`log_artifact`/`ingest`/`edges`/`execution-records`)
build their payloads through the generated models, so a renamed or removed field
fails client-side instead of as a server 422. `/ingest/v1/runs` is now declared in
the schema too (Probe Research PR #12), so the passive push is generated and validated
like every other path.

- `schema/openapi.json` - a snapshot of Probe Research's FastAPI schema.
- `src/probe/_generated/models.py` - generated, never hand-edited.
- `src/probe/models.py` - the stable import seam the SDK uses.

Refresh when the contract moves:

```bash
make regen        # dump-openapi (RESEARCH_OS=../../research-os) + gen-models
# or step by step:
RESEARCH_OS=/path/to/research-os python scripts/dump_openapi.py
python scripts/gen_models.py
```

`RESEARCH_OS` points at a local checkout of the Probe Research backend source repo
(directory name `research-os`); it is only used to regenerate the schema snapshot.

## CLI grammar note

The CLI is built on **typer**. Connection flags are global and go *before* the
command: `probe --token probe_pat_x log RUN loss=0.1`. `probe login` also accepts them
directly (`probe login --token ...`).

## Tests

```bash
pytest        # 29 mocked/unit tests + a real-git snapshot test; no live server
```
