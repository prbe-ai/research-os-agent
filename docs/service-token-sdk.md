# `probe.Reader` — service-token read client

A read-only client for a **`probe_svc_` service token**: the surface an external product
uses to pull metrics, metadata, and artifacts out of Probe Research **synchronously**.

The token is **team-scoped, userless, read-only, and rotatable** (mint it in the dashboard:
*Settings → Service tokens*, session + admin). Unlike a personal PAT it survives member
churn, so it's safe to embed in a product many people use. Every method maps to exactly one
server-allowlisted read endpoint; anything else (writes, search, transcripts) is `403` by
design.

## Quickstart

```python
from probe import Reader

r = Reader.from_env()          # reads PROBE_SERVICE_TOKEN (+ PROBE_BASE_URL)
# or: Reader.from_env(base_url="https://api.research.prbe.ai", service_token="probe_svc_…")

# enumerate
for run in r.runs(experiment_id=exp_id):
    print(run["short_id"], run["name"])

# pull a specific point / set by ANY differentiator (synchronous, one request)
pts = r.metrics(run_id, key="reward", labels={"sample": 5}, dimensions={"rank": 0})

# reduce server-side
mean_per_rank = r.metrics_grouped(run_id, "reward", by=["rank"], agg="mean")

# artifact bytes (presigned URL, with a self-host proxy fallback)
blob = r.download_artifact(artifact_id)          # -> bytes
r.download_artifact(artifact_id, dest="ckpt.bin")  # -> {"size_bytes", "sha256"}
```

`Reader` is synchronous and closeable (`with Reader.from_env() as r: ...`).

## Auth

`PROBE_SERVICE_TOKEN` (or `service_token=`) becomes the `/v1` bearer. `Reader.from_env()`
deliberately does **not** fall back to `PROBE_TOKEN` — a Reader is a service-token client.
Config file key: `service_token` (per named context, kubectl-style).

## Method → endpoint map

### Enumeration & metadata
| Reader method | Endpoint |
|---|---|
| `projects(workspace_id=, slug=, tags=)` | `GET /v1/projects` (auto-paginates) |
| `project(id)` | `GET /v1/projects/{id}` |
| `experiments(project_id=, slug=, tags=)` | `GET /v1/experiments` (auto-paginates) |
| `experiment(id)` | `GET /v1/experiments/{id}` |
| `runs(experiment_id=, project_id=, status=, tags=)` | `GET /v1/runs` (auto-paginates) |
| `run(ref)` | `GET /v1/runs/{ref}` (UUID or petname) |
| `browse(scope=, depth=, status=, tags=)` | `GET /v1/browse` |
| `groups(experiment_id)` | `GET /v1/experiments/{id}/groups` |
| `group(id)` | `GET /v1/groups/{id}` |
| `bundle(run)` | `GET /v1/runs/{run}/bundle` (run + series + settings + artifacts + lineage) |
| `lineage(run)` | `GET /v1/runs/{run}/lineage` |

### Metrics
| Reader method | Endpoint |
|---|---|
| `metrics(run, key=, kind=, dimensions=, labels=, span_id=, step_from=, step_to=, limit=)` | `GET /v1/runs/{run}/metrics` — **the synchronous point query** |
| `metrics_grouped(run, key, by=, where=, agg=, step_bucket=, …)` | `GET /v1/runs/{run}/metrics/grouped` |
| `metrics_wide(run, key=, kind=, step_from=, step_to=)` | `GET /v1/runs/{run}/metrics/wide` |
| `series(run)` | `GET /v1/runs/{run}/series` |
| `series_query(run_ids, **body)` | `POST /v1/series/query` (read; downsampled charts) |
| `coordinates(run)` | `GET /v1/runs/{run}/coordinates` |

`dimensions`/`labels`/`where` take **dicts** and are sent as type-faithful JSON containment
(`{"sample": 1}` ≠ `{"sample": "1"}`). A `labels` or `span_id` filter **requires `key`** — the
backend prunes the scan by `(run, key)` first, then filters. This is a bounded live query, not
a bulk export.

### Artifacts
| Reader method | Endpoint(s) |
|---|---|
| `artifacts(run, kind=, prefix=)` | `GET /v1/runs/{run}/artifacts` (metadata) |
| `download_url(id)` | `POST /v1/artifacts/{id}/download` → URL, or `Reference` |
| `download_artifact(id, dest=, mode=)` | `/download` (+ presigned GET) with `/content` fallback |
| `preview(id)` | `GET /v1/artifacts/{id}/preview` (bounded inline bytes) |

**`download_artifact(mode=…)`** negotiates the deployment model for you:

| mode | behavior | fits |
|---|---|---|
| `"auto"` (default) | presigned URL first; if the object store is unreachable, fall back to the API byte **proxy** | cloud + self-host, without knowing which |
| `"url"` | return the presigned URL string (or `Reference`) — no fetch | hand the URL to a browser/worker |
| `"proxy"` | always fetch bytes through the API (`/content`, ≤100 MiB) | air-gapped / non-presignable store |

A **client-owned reference** (`is_reference`) is never server-fetched (the confused-deputy
rule): the Reader returns a `Reference(uri, local_path, host)` for you to resolve with your own
store credentials. Over-cap proxy → `413` (use the presigned URL). Managed cloud presigns
against Probe's R2; BYOB presigns against your bucket via a least-privilege scoped credential.

## Not available on a service token (403 by design)
Writes (create/patch/delete, logging), `POST /v1/search` (KB, user-scoped), agent transcripts,
reproduce views, token management, and the **bulk** `GET /v1/runs/{id}/metrics/export` (this
client is synchronous-live only — use `metrics(...)` for point/range queries).

## Errors
Typed on `probe.errors`: `AuthError` (401), `ScopeError` (403 — off the allowlist),
`NotFoundError` (404), `ConflictError` (409), `ValidationError` (422), `ServerError` (5xx),
`TransportError` (network). A `labels=`-without-`key=` request is a `ValidationError` (422).
