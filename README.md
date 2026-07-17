# probe-research (`probe` SDK/CLI + `probe-research` plugin)

CLI + SDK client for **Probe Research**, Probe's experiment-tracking platform. It is a
thin client over the v3 ingestion contract (`CONTRACT.md` in the Probe Research backend).
Implemented experiment calls map onto real endpoints. Target asset-registry
methods are present as an explicit client contract but fail closed with a
capability error until the backend routes exist.

## Two client surfaces

Probe Research exposes experiment tracking through two separate surfaces over the same backend, for two different workflows:

- **`probe` — SDK + CLI (non-agent).** A Python library (`import probe`) and the `probe` command-line tool for integrating with existing setups and manual experimentation. Drop it into a training script or pipeline to record runs, metrics, spans, and artifacts. No agent required.
- **`probe-research` — plugin: skills + MCP (agent-centric).** Installed into a coding agent (e.g. Claude Code). Its skills teach the agent the experiment workflow, its read-only MCP server lets the agent query experiment state, and writes flow through the `probe` CLI. This is the surface for agent-driven research loops such as Anthrogen.

Same backend, two entry points: humans-in-code reach for the SDK/CLI; agents-in-the-loop use the plugin.

## Package boundaries

```text
src/probe/
├── sdk/       # typed client, uploads, local capture, session adapter ABI
├── cli/       # `probe`: thin shell over the SDK
└── mcp/       # `probe-research-mcp`: strictly read-only tools and resources
skills/
├── track-experiment/
├── manage-research-asset/
└── publish-experiment/
```

The SDK is the implementation. The CLI, MCP source adapter, future hooks,
Python experiments, and passive platform integrations all use it. CLI and SDK
therefore have capability parity; they differ only in ergonomics.

| Surface | SDK | CLI | Intended caller |
|---|---|---|---|
| Experiment upload | `Client.run`, `Run.log/span/log_artifact/snapshot/link/execute`, `Client.events`, `Client.promote` | `run`, `log`, `span`, `artifact`, `snapshot`, `link`, `exec`, `event`, `promote` | Researchers, agents, notebooks, training/platform code |
| Session adapter | `Client.sessions.attach/checkpoint/detach` | `probe hook session ...` | **Future deterministic hooks/broker only** |
| Asset read/selection | `Client.assets.resolve`, normally behind MCP | No normal read verb | Agent through read-only MCP |
| Asset effects | `Client.assets.materialize/fork/propose/promote` | `probe asset ...` | Agent/researcher after selecting an exact asset ref |
| Passive ingestion | `Client.ingest` | No convenience command yet | Install-once platform integration |
| Read plane | SDK reads used by `probe.mcp` | `get`/`bundle` diagnostics | MCP for agents; CLI for humans/scripts |

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

You can also skip `probe login` entirely: the first `client.run()` /
`probe run start` with no token triggers the same browser approval inline (TTY only)
and persists the result. Disable with `PROBE_AUTO_LOGIN=0`; headless/CI keeps the
crisp `AuthError` and should set `PROBE_TOKEN`.

The MCP server prefers `PROBE_MCP_TOKEN`, which should be a separately minted
read-only token. It falls back to `PROBE_TOKEN` for local development, but exposes
no mutation tools.

On rented compute (RunPod) with no standing config, the `/track-experiment` skill seeds
`PROBE_TOKEN` at session start.

## SDK (agent-driven / interactive)

```python
import probe

client = probe.Client()  # resolves creds from env / `probe login`

run = client.run(experiment="dockq-sweep", hypothesis="temp 0.7 wins", name="run-1",
                 project="folding", source="runpod", external_id="rp-9931")
# …or with zero identity args: `client.run()` defaults experiment to the git repo /
# script name, name to a timestamp (the server adds a petname short_id), and a NEW
# experiment gets a marked "[auto] …" hypothesis composed from context. Set the real
# one later: client.update_experiment(id, hypothesis="…")  /  probe experiment set.

run.snapshot()                                   # non-disruptive git + deps + GPU capture
run.link(wandb_run_id="abc123", s3_prefix="s3://x/y")

for step in range(100):
    run.log({"loss": ..., "dockq": ...}, step=step)     # POST /v1/runs/{id}/metrics

sid = run.span("rollout", name="rollout-0", step_index=1)   # trajectory span
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
(or `probe flush`) replays the spool. Pass `strict=True` to make a write raise.

## SDK (install-once / passive push)

```python
client.ingest(
    experiment_slug="dockq", experiment_hypothesis="...",
    run={"name": "r1", "source": "temporal", "external_id": "wf-1", "status": "running"},
    metrics=[{"kind": "model", "key": "loss", "value": 0.5, "step_index": 1}],
    batch_id="deadbeef",          # idempotent redelivery
)
```

One idempotent push (bearer ingest token + optional HMAC), keyed on
`(customer_id, source, external_id)`.

## CLI (`probe`)

```bash
RUN=$(probe run start --experiment dockq --hypothesis "temp 0.7 wins" --name run-1 \
        --project folding --source runpod --external-id rp-9931)
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
# retroactively expand a stored trajectory (e.g. after a fork's parser ships);
# deterministic span ids make this idempotent — re-runs upsert, never duplicate
probe trial expand $RUN <manifest-artifact-id> --max-spans 0
```

Query it back: `client.list_run_artifacts(run_id, kind="harbor_trial",
step_from=599, step_to=601)`. Fork parsers plug in via
`probe.connectors.atif.register_trajectory_parser("their-format", fn)`;
unknown formats are captured raw (never rejected) and expanded later.

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

Run the stdio server with `probe-research-mcp`. It exposes exactly five tools:

| Tool | Function |
|---|---|
| `research_context` | Project/session bootstrap, prior experiments, active runs, capability warnings |
| `research_search` | One-index exact+semantic backend search (`POST /v1/search`, corpora: assets/procedures → files, documents → github+files, transcripts unsupported); keyword fallback on pre-search backends |
| `research_get` | One entity through a purpose-shaped `view` — see below |
| `research_compare` | Server-side comparison of runs, experiments, and asset versions |
| `research_resolve` | Compatible asset resolution against the live registry |

**Thin harness, fat skills.** Coverage grows through `research_get`'s `view` and `filters`
parameters, never through more tools — fewer tools means less tool-selection confusion, and
capability that lives inside a tool is code-enforced rather than smeared across entrypoints.
Which view to ask for when is taught in `skills/track-experiment`.

`research_get(ref, view=..., filters=..., token_budget=..., cursor=...)`, where `ref` is
`run:<id>`, `experiment:<id>`, `project:<id>`, `group:<id>`, or a bare id:

| Kind | Views |
|---|---|
| run | `card` · `trajectory` · `metrics` · `artifacts` · `reproduce` · `handoff` · `lineage` · `events` |
| experiment | `card` · `artifacts` · `lineage` · `groups` · `versions` |
| project, group | `card` |

`trajectory` reads a run's spans (the run bundle carries span_type *counts* only, so this is
the only way to read one). `metrics` returns series summaries, and `filters={"key": "<key>"}`
drills to raw points. `reproduce` resolves `env_ref` through its execution record. `groups`
lists an experiment's sweeps; read one with `ref="group:<id>"`. `token_budget` bounds the
row-shaped part of a view and hands back a `next_cursor`; `reproduce` is atomic and reports
`token_budget_exceeded` rather than truncating a manifest into something that reproduces
nothing.

There is no `research_trace_file`: no backend trace index has ever existed, so it answered
`matches: []` to every query, which agents read as "this file has no lineage". To trace a
path/URI/hash, use `research_search` (its exact channel matches artifacts) and follow
`research_get view="lineage"`.

MCP reads through the Probe Research API—never directly from Postgres or R2. Its
logical sources are control identity/tenant scope, the structured experiment
store, the asset/manifest registry, the one-index search door (`POST /v1/search`:
exact SQL channel + the KB engine's semantic channel; search capabilities are
discovered against the live backend with one cached probe), and object-store
resource pointers returned by the API. W&B, RunPod, Kubernetes,
Git, and local transcript paths are not live MCP sources; adapters upload their
identifiers and evidence first.

## Skills

- `experiment` mentally boxes result-producing work and uploads concise evidence.
- `manage-research-asset` resolves before create, reuses exact versions, forks
  immutable bases, and proposes candidates without filename-based sprawl.
- `publish-experiment` requires explicit approval and refuses to imitate official
  promotion when manifest/asset capabilities are unavailable.

Asset-reuse hooks are deliberately deferred. The track-experiment skill contains the
reuse-before-create rule; deterministic enforcement can be added later without
changing the SDK, CLI, MCP, or skill contracts.

## What maps to what (v3 endpoints)

| Client call | Endpoint |
|---|---|
| `client.run()` / `run.child()` | `POST /v1/experiments`, `POST /v1/experiments/{id}/runs` |
| `run.log()` / `run.log_hw()` | `POST /v1/runs/{id}/metrics` |
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
- **Asset registry.** `client.assets.register()` + `add_version()` + `resolve()`
  (fold #5). The aspirational fork/propose/promote-candidate surface was dropped
  (promotion tiers rejected upstream).
- **Experiment versions.** `client.experiment_version()` mints the immutable manifest
  (fold #6). This replaces the removed run-level `promote`.
- **Lineage edges.** `client.add_edge()` / `run.edges()` (fold #2).
- **foreign_keys.** first-class on the ingest path (`run['foreign_keys']`, fold #8) and
  surfaced on reads (`run.foreign_keys`, `run.short_id`).
- **Events read.** `client.events.list()` / `for_run()` (server-emitted lifecycle log).
  Research notes moved to `client.notes.add()` (stored as `kind="note"` artifacts).

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
paths (`log`/`span`/`log_artifact`/`ingest`/`assets`/`edges`/`execution-records`)
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
