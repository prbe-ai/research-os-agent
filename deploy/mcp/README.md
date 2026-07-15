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

## Deploy to DOKS (Phase B — production; gated)

Prereqs already on the `research` cluster: ingress-nginx, cert-manager (`letsencrypt-prod`).

```bash
# 1. build + push the image (GHCR)
docker build -f deploy/mcp/Dockerfile -t ghcr.io/prbe-ai/research-os-mcp:0.5.0 .
docker push ghcr.io/prbe-ai/research-os-mcp:0.5.0

# 2. DNS: point mcp.research.prbe.ai at the same ingress LB IP as api.research.prbe.ai

# 3. apply
kubectl apply -f deploy/mcp/k8s.yaml
kubectl -n research rollout status deploy/research-os-mcp

# 4. verify (cert may take a minute)
curl -s https://mcp.research.prbe.ai/healthz
```

Notes:
- `k8s.yaml` points `PROBE_BASE_URL` at the in-cluster API service to avoid an LB hairpin;
  swap to `https://api.research.prbe.ai` if you prefer the public endpoint.
- No secrets in the manifest — auth is per-request. Rate-limit at the ingress if needed.
- This is a **production change**; do it only with explicit go-ahead.
