# Hosting the read-only Research OS MCP

The hosted MCP is a **stateless** streamable-HTTP service. It holds no tenant token —
each request carries the caller's read-scoped `ros_pat` as `Authorization: Bearer …`,
and the server forwards that identity to the Research OS API (tenant isolation is the
API's existing RLS). So it scales horizontally and stores nothing.

Endpoint: `https://mcp.research.prbe.ai/mcp` · health: `/healthz`.

## Run locally (dev / self-host)

```bash
pip install -e ".[mcp-http]"
PROBE_BASE_URL=https://api.research.prbe.ai probe-research-mcp-http   # serves :8080/mcp
curl -s localhost:8080/healthz            # {"status":"ok"}
```

Or stdio (single-tenant): `probe-research-mcp`. It takes the token from
`PROBE_MCP_TOKEN`, falling back to the `mcp_token` that `probe mcp token set` stores.
(The pre-rename `ROS_MCP_TOKEN` still works, deprecated, with a warning.)

## Deploy to DOKS — automatic

`.github/workflows/deploy-mcp.yml` builds and rolls this service on every push to `main`
that touches `src/probe/**`, `pyproject.toml`, or `deploy/mcp/**`. **Do not deploy by hand.**

It runs the test suite, pushes `ghcr.io/prbe-ai/research-os-mcp:sha-<git-sha>` (immutable),
applies the manifest with that exact image, waits for the rollout, asserts the image that
actually landed is the one it built, and curls `/healthz`.

The filter is `src/probe/**` and not `src/probe/mcp/**` on purpose: this server builds a
`probe.sdk.Client` and imports the shared models, so an SDK change reaches it too.

### Enabling it (one-time)

The build always runs; the **deploy is dormant** until both exist:

| What | Where | Value |
| --- | --- | --- |
| Repo variable | Settings → Secrets and variables → Actions → Variables | `DEPLOY_MCP_ENABLED` = `true` |
| Environment secret | Settings → Environments → `Production` | `DIGITALOCEAN_ACCESS_TOKEN` (DO API token, write scope) |

Keep the token an **environment** secret, not a repo secret, so only the gated `Production`
job can read it. Until then the workflow still builds and publishes every image, so turning
the deploy on later can roll to any past SHA.

### Break-glass: deploying by hand

`k8s.yaml` carries an `__IMAGE__` placeholder rather than a tag, so it will not apply as-is —
that is deliberate. The old manifest pinned a mutable `:0.5.0` that nobody re-pushed, and the
deployed service drifted from `main` with no way to tell what was running. Substitute a real,
immutable SHA:

```bash
SHA=$(git rev-parse HEAD)          # or any commit whose image was built
sed "s|__IMAGE__|ghcr.io/prbe-ai/research-os-mcp:sha-${SHA}|" deploy/mcp/k8s.yaml \
  | kubectl apply -f -
kubectl -n research rollout status deploy/research-os-mcp
kubectl -n research get deploy/research-os-mcp \
  -o jsonpath='{.spec.template.spec.containers[0].image}'   # confirm what landed
curl -s https://mcp.research.prbe.ai/healthz
```

Prereqs already on the `research` cluster: ingress-nginx, cert-manager (`letsencrypt-prod`),
and DNS for `mcp.research.prbe.ai` pointed at the same ingress LB IP as `api.research.prbe.ai`.

Notes:
- `k8s.yaml` points `PROBE_BASE_URL` at the in-cluster API service to avoid an LB hairpin;
  swap to `https://api.research.prbe.ai` if you prefer the public endpoint.
- No secrets in the manifest — auth is per-request. Rate-limit at the ingress if needed.
