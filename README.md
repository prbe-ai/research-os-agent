# research-os-agent (`ros` / `exp`)

CLI + SDK client for **research-os**, Probe's experiment-tracking platform. It is a
thin client over the v3 ingestion contract (`CONTRACT.md` in `research-os`).
Implemented experiment calls map onto real endpoints. Target asset-registry
methods are present as an explicit client contract but fail closed with a
capability error until the backend routes exist.

> Names (`ros` import, `exp` CLI) are placeholders per the SDK/CLI primitives sketch.

## Package boundaries

```text
src/ros/
├── sdk/       # typed client, uploads, local capture, session adapter ABI
├── cli/       # `exp`: thin shell over the SDK
└── mcp/       # `research-os-mcp`: strictly read-only tools and resources
skills/
├── experiment/
├── manage-research-asset/
└── publish-experiment/
```

The SDK is the implementation. The CLI, MCP source adapter, future hooks,
Python experiments, and passive platform integrations all use it. CLI and SDK
therefore have capability parity; they differ only in ergonomics.

| Surface | SDK | CLI | Intended caller |
|---|---|---|---|
| Experiment upload | `Client.run`, `Run.log/span/log_artifact/snapshot/link/execute`, `Client.events`, `Client.promote` | `run`, `log`, `span`, `artifact`, `snapshot`, `link`, `exec`, `event`, `promote` | Researchers, agents, notebooks, training/platform code |
| Session adapter | `Client.sessions.attach/checkpoint/detach` | `exp hook session ...` | **Future deterministic hooks/broker only** |
| Asset read/selection | `Client.assets.resolve`, normally behind MCP | No normal read verb | Agent through read-only MCP |
| Asset effects | `Client.assets.materialize/fork/propose/promote` | `exp asset ...` | Agent/researcher after selecting an exact asset ref |
| Passive ingestion | `Client.ingest` | No convenience command yet | Install-once platform integration |
| Read plane | SDK reads used by `ros.mcp` | `get`/`bundle` diagnostics | MCP for agents; CLI for humans/scripts |

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
exp login --base-url https://api.research.prbe.ai --token ros_pat_xxxxxxxx
```

This verifies the token (`GET /auth/me`) and writes `~/.config/ros/config.json`.
Or set env: `ROS_BASE_URL`, `ROS_TOKEN` (user token, `/v1`), `ROS_INGEST_TOKEN`
(ingest token, `/ingest`), `ROS_HMAC_SECRET` (optional body-signature secret).

The MCP server prefers `ROS_MCP_TOKEN`, which should be a separately minted
read-only token. It falls back to `ROS_TOKEN` for local development, but exposes
no mutation tools.

On rented compute (RunPod) with no standing config, the `/experiment` skill seeds
`ROS_TOKEN` at session start.

## SDK (agent-driven / interactive)

```python
import ros

client = ros.Client()  # resolves creds from env / `exp login`

run = client.run(experiment="dockq-sweep", hypothesis="temp 0.7 wins", name="run-1",
                 project="folding", source="runpod", external_id="rp-9931")

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
(`~/.local/state/ros/spool`) and return, never blocking the training loop. `run.finish()`
(or `exp flush`) replays the spool. Pass `strict=True` to make a write raise.

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

## CLI (`exp`)

```bash
RUN=$(exp run start --experiment dockq --hypothesis "temp 0.7 wins" --name run-1 \
        --project folding --source runpod --external-id rp-9931)
exp snapshot $RUN
exp link $RUN --set wandb_run_id=abc --set gpu_job=rp-9931
exp log $RUN loss=0.42 dockq=0.71 --step 42
exp span add $RUN --type rollout --name rollout-0 --step 1
exp artifact add $RUN ./final.sif --kind artifact
exp event add $RUN --kind decision --statement "Use DockQ scorer v3" --evidence tool:91
exp exec $RUN -- python train.py --config dockq.yaml
exp run check $RUN
exp run end $RUN --status completed
exp bundle $RUN            # read: run + series + artifacts
```

The following commands are reserved for future hook configuration and are not
part of the normal researcher workflow:

```bash
exp hook session attach RUN --session-id SESSION --transcript-path PATH --cwd DIR
exp hook session checkpoint RUN --session-id SESSION --transcript-path PATH --reason pre_compact
exp hook session detach RUN --session-id SESSION --reason session_end
```

They currently encode session links in `run.metadata.agent.sessions[]` and
transcript checkpoints as redacted local-reference artifacts. Until managed
artifact upload exists, transcript portability remains explicitly false.

## Read-only MCP server

Run the stdio server with `research-os-mcp`. It exposes exactly six tools:

| Tool | Function |
|---|---|
| `research_context` | Project/session bootstrap, prior experiments, active runs, capability warnings |
| `research_search` | Structured keyword fallback now; semantic/KB fusion when the backend lands |
| `research_get` | Progressive card, handoff, reproduction, lineage, metrics, and artifact views |
| `research_compare` | Server-side comparison of runs, experiments, and future asset versions |
| `research_resolve` | Compatible asset resolution; honest partial result on API v3 |
| `research_trace_file` | Producer-consumer and cleanup lineage; partial until trace indexing lands |

MCP reads through the Research OS API—never directly from Postgres or R2. Its
logical sources are control identity/tenant scope, the structured experiment
store, the future asset/manifest registry, the future KB projection, and
object-store resource pointers returned by the API. W&B, RunPod, Kubernetes,
Git, and local transcript paths are not live MCP sources; adapters upload their
identifiers and evidence first.

## Skills

- `experiment` mentally boxes result-producing work and uploads concise evidence.
- `manage-research-asset` resolves before create, reuses exact versions, forks
  immutable bases, and proposes candidates without filename-based sprawl.
- `publish-experiment` requires explicit approval and refuses to imitate official
  promotion when manifest/asset capabilities are unavailable.

Asset-reuse hooks are deliberately deferred. The experiment skill contains the
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

## v0.4.0.0 ingestion fold-in (Phase 1)

Most earlier gaps are closed by research-os v0.4 (PR #13). Now wired:

- **Real metric dimensions.** `log_hw(..., device=3, host="n1")` sends `dimensions`
  (fold #9); `log(..., dimensions={...})`. No more key-encoding.
- **Presign artifact upload.** `log_artifact(path=...)` runs presign → PUT to R2 →
  confirm (fold #16). Fails open to a reference on error. (`kind`/`meta` aren't carried
  by the upload flow yet, warned once — a Phase-2 backend follow-up.)
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

### Remaining (Phase 2)

- **`foreign_keys` / `env_ref` on the interactive path.** Settable only via ingest
  today; `RunPatch` can't set them, so interactive `link()`/`snapshot()` write
  `metadata.foreign_keys` / `metadata.env_ref` as interim. Needs a small backend PR
  adding those fields (merge) to `RunPatch`.
- **Asset `materialize`.** Deferred until the backend exposes an asset-version download.
- **Upload `kind`/`meta`.** The presign flow doesn't carry them yet.
- **MCP semantic/KB search** and **session hooks** remain later work.

## Typed models (generated from the OpenAPI contract)

Request/response models are generated from the backend's OpenAPI schema, not
hand-written, so the client cannot silently drift from the contract. The write
paths (`log`/`span`/`log_artifact`/`ingest`/`assets`/`edges`/`execution-records`)
build their payloads through the generated models, so a renamed or removed field
fails client-side instead of as a server 422. `/ingest/v1/runs` is now declared in
the schema too (research-os PR #12), so the passive push is generated and validated
like every other path.

- `schema/openapi.json` - a snapshot of research-os's FastAPI schema.
- `src/ros/_generated/models.py` - generated, never hand-edited.
- `src/ros/models.py` - the stable import seam the SDK uses.

Refresh when the contract moves:

```bash
make regen        # dump-openapi (RESEARCH_OS=../../research-os) + gen-models
# or step by step:
RESEARCH_OS=/path/to/research-os python scripts/dump_openapi.py
python scripts/gen_models.py
```

## CLI grammar note

The CLI is built on **typer**. Connection flags are global and go *before* the
command: `exp --token ros_pat_x log RUN loss=0.1`. `exp login` also accepts them
directly (`exp login --token ...`).

## Tests

```bash
pytest        # 29 mocked/unit tests + a real-git snapshot test; no live server
```
