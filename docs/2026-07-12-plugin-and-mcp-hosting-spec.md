# research-os plugin + MCP hosting — spec

**Status:** Implemented (Phase A merged). MCP = **both** models, **default remote HTTP**; distribution = **prbe-ai marketplace**. Update: **PyPI skipped** — Claude Code plugins/marketplaces are GitHub-repo-hosted, and the `exp`/MCP CLI installs from `git+https://github.com/prbe-ai/research-os-agent` (§5 below superseded). Hosted MCP image `ghcr.io/prbe-ai/research-os-mcp:0.4.0` is pushed; deploy applied to `probe-research` (pending package-public + DNS).
**Goal:** turn the 3 skills + read-only MCP + `exp` CLI into a one-command install, and host the MCP so reads need zero local setup.

---

## 0. What a user gets (end state)

**Managed (default):**
```
claude plugin install research-os@prbe-ai      # skills + MCP wiring
/research-os-setup                              # installs `exp`, logs in, sets the read token
```
After that:
- **Reads** (context, search, prior runs, asset resolve, lineage) come from a **hosted** MCP endpoint — nothing runs locally.
- **Writes** (log metrics, spans, artifacts, link, snapshot, promote to a version) go through the pip-installed `exp` CLI / `ros` SDK.
- The 3 skills (`/experiment`, `manage-research-asset`, `publish-experiment`) drive both.

**Self-host / air-gap:** same plugin, but the MCP runs locally (`uvx research-os-agent research-os-mcp`) pointed at a self-hosted API. No dependency on our hosted endpoint.

---

## 1. The plugin

A Claude Code plugin published to the **prbe-ai marketplace**. Contents:

```
plugins/research-os/
  .claude-plugin/plugin.json      # name, version, description, author
  skills/
    experiment/SKILL.md
    manage-research-asset/SKILL.md
    publish-experiment/SKILL.md
  commands/research-os-setup.md   # /research-os-setup
  .mcp.json                       # MCP server declaration (remote HTTP default)
```

- **`plugin.json`** — metadata; version tracks the client (`0.4.x`).
- **skills/** — the same skills, corrected to the shipped Phase-2 surface (see §4). Source of truth stays the repo's `skills/`; the plugin build copies them (one folder, no drift).
- **`.mcp.json`** — declares the MCP server. Default (managed):
  ```json
  { "mcpServers": { "research-os": {
      "type": "http",
      "url": "https://mcp.research.prbe.ai/mcp",
      "headers": { "Authorization": "Bearer ${ROS_MCP_TOKEN}" } } } }
  ```
  Documented alternative (self-host / local): `command: uvx, args: [--from, research-os-agent, research-os-mcp]`, `env: { ROS_MCP_TOKEN, ROS_BASE_URL }`.
- **`/research-os-setup`** — a slash command that: (1) ensures `exp` is installed (`uvx`/`pip install research-os-agent`); (2) runs `exp login` (writes the write-scoped PAT); (3) captures a **read-scoped** PAT into `ROS_MCP_TOKEN`; (4) verifies the MCP endpoint responds. One prompt, done.

**Distribution:** the plugin dir lives in the `research-os-agent` repo (`plugins/research-os/`); the prbe-ai marketplace's `marketplace.json` references it. `claude plugin marketplace update prbe-ai` picks up new versions.

---

## 2. MCP hosting

### 2a. Remote HTTP (default, managed)

**What it is:** the existing `ros.mcp` FastMCP server, run over **Streamable HTTP** as a small stateless service on the DOKS cluster at **`mcp.research.prbe.ai`**, behind the ingress-nginx + cert-manager already in place. It is a **thin proxy**: it holds no database or R2 access and no tenant data — it only calls the research-os HTTP API (`https://api.research.prbe.ai`).

**Per-request auth (the key change).** Today the server reads one `ROS_MCP_TOKEN` from env and builds one `Client` (single tenant). Hosted, it must be multi-tenant:
- each MCP call carries the **caller's** read-scoped `ros_pat` as `Authorization: Bearer …`;
- the server reads that header from the request context, builds a `Client(token=<caller token>)` **per request**, and routes the tool through a service bound to that client;
- the server itself carries **no** tenant credential. Tenancy + isolation are the research-os API's existing RLS on the caller's PAT. Read scope is enforced by the token's scopes (`['read']`).

So a leaked/rotated token is a per-tenant, read-only concern, and the hosted server is a stateless forwarder we can scale horizontally.

**Deploy shape:**
- Container image `ghcr.io/prbe-ai/research-os-mcp` (the `research-os-agent` package, entrypoint = the HTTP MCP app).
- k8s `Deployment` (2+ replicas) + `Service` + `Ingress` host `mcp.research.prbe.ai`, TLS via the existing `letsencrypt-prod` issuer.
- Env: `ROS_BASE_URL=https://api.research.prbe.ai`. No secrets (auth is per-request).
- Health: `/healthz`. Stateless → HPA-friendly.

**Clients it serves:** Claude Code (plugin `.mcp.json` `type: http`), claude.ai, the Claude API `mcp_servers` param, and any other MCP client — all with just a URL + read token.

### 2b. Local stdio (self-host / air-gap / power users)

Unchanged server: `research-os-mcp` runs as a local subprocess (`uvx --from research-os-agent research-os-mcp`), reads `ROS_MCP_TOKEN`, calls whatever `ROS_BASE_URL` points at (our API or a self-hosted one). This is the current code path; it stays as the offline/self-host option and needs no server from us.

### 2c. How they coexist

One server module, two entrypoints: `main()` (stdio, today) and `main_http()` (streamable HTTP for hosting). The tool/service logic is shared; only transport + where the token comes from differ (env for stdio, per-request header for HTTP). The plugin defaults to 2a; self-host docs use 2b.

---

## 3. Auth / token model

| Surface | Token | Scope | Where |
|---|---|---|---|
| Writes (`exp` / SDK) | `ros_pat_…` | read+write(+delete) | `exp login` → `~/.config/ros/config.json` |
| Reads (MCP, hosted or local) | `ros_pat_…` | **read only** (notebook token) | `ROS_MCP_TOKEN` env / plugin header |

Two tokens (a write PAT and a read-only PAT) keep the MCP blast radius to read-only. `/research-os-setup` mints/collects both. Minting stays session-gated (dashboard), unchanged.

---

## 4. Skills corrections (must-do, part of consolidation)

The skills reference removed/renamed surface:
- **`manage-research-asset`**: uses `exp asset fork` / `exp asset propose` and "candidate/promote" + the `versioned_assets: false` capability guard. Phase 2 shipped a registry (`register` / `version` / `materialize`) and **removed** fork/propose/promote-candidate. Rewrite to the shipped verbs; drop the capability-unavailable branch (the registry exists).
- **`publish-experiment`**: mentions imitating "official promotion when manifest/asset capabilities are unavailable." Now `client.experiment_version()` is the manifest and run-level `promote` is gone. Rewrite to mint an experiment version.
- **`experiment`**: audit for any stale CLI (`event add` → `note add`, metric dims, upload) and align with the current `exp` surface.

The `agents/openai.yaml` variants get the same corrections (the codex/OpenAI distribution path).

---

## 5. PyPI packaging

Publish `research-os-agent` to PyPI so `uvx`/`pip install research-os-agent` works (enables both the `exp` CLI install and `uvx research-os-mcp`).
- Add classifiers, project URLs, `Requires-Python`, long-description = README.
- Keep `fixtures/` out of the wheel (already the case); it stays example-only in the repo.
- Version = `0.4.0` (aligns with the research-os v0.4 contract).

---

## 6. Work breakdown

**Phase A — code/config (no external credentials; one PR):**
1. Fix the 3 skills + their `openai.yaml` to the Phase-2 surface (§4).
2. MCP: add HTTP transport + per-request token pass-through; keep stdio (§2a/2c). Unit test the auth extraction.
3. Plugin scaffold: `plugin.json`, `.mcp.json`, `/research-os-setup`, marketplace entry (§1).
4. Dockerfile + k8s manifest for `mcp.research.prbe.ai` (§2a).
5. PyPI metadata (§5).

**Phase B — needs your credentials / approval:**
6. Publish `research-os-agent` to PyPI (needs the PyPI token/account).
7. Add the plugin to the prbe-ai marketplace repo (needs marketplace repo write).
8. Build+push `ghcr.io/prbe-ai/research-os-mcp`, deploy to DOKS, add the `mcp.research.prbe.ai` DNS + TLS. **This is a production change** — gated on your go-ahead.

---

## 7. Security / tenancy

- Hosted MCP is **stateless** and holds no tenant token or data; it forwards the caller's read PAT. Isolation = research-os RLS (unchanged). Read scope enforced by token scopes.
- TLS via cert-manager; rate-limit at ingress. A leaked read token = one tenant, read-only, revocable.
- Transcripts/logs returned by tools are labeled evidence, never instructions (already in the server instructions).

## 8. Open questions

1. **Endpoint:** dedicated host `mcp.research.prbe.ai` (recommended, its own deploy) vs a path `api.research.prbe.ai/mcp` (would mean the backend imports/serves this client-repo code — messier). Recommend the dedicated host.
2. **Marketplace repo:** is prbe-ai its own marketplace repo, or folded into the gstack marketplace? Where does `marketplace.json` live?
3. **Read-token UX:** does `/research-os-setup` mint the read PAT for the user (needs a dashboard/session round-trip) or ask them to paste one?
4. **PyPI name/org:** confirm `research-os-agent` on the prbe-ai PyPI org.
