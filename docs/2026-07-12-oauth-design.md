# Research OS OAuth — design

**Status:** design. Static-bearer (read `ros_pat` in `ROS_MCP_TOKEN` / `Authorization: Bearer`)
shipped as v1 (Connect page, PR research-os#19). This doc plans the move to OAuth for the
hosted MCP and browser-assisted auth for the CLI.

**Why:** hosted MCP clients (claude.ai custom connectors, Claude Code, Cursor) expect the
OAuth "click Connect → browser flow → auto-refresh" UX; a pasted PAT is friction and can't
refresh. Anthropic's connector vault supports both `static_bearer` and `mcp_oauth` — we use
the former today; `mcp_oauth` is the better end-state.

**Precedent (mirror this):** the team already shipped a full MCP OAuth 2.1 provider for the KB
product — `prbe-backend` "Phase G" (ES256 JWTs, JWKS, RFC 8414 metadata, RFC 7591 dynamic
registration, PKCE authorize/consent/token, refresh rotation) + `prbe-knowledge` resource
server (RFC 9728 protected-resource metadata, `WWW-Authenticate` challenge, dual `oauth`/`static`
auth middleware). We adapt that blueprint to research-os rather than invent one.

---

## Two workstreams

### B. CLI OAuth — browser-assisted `exp login` (small, client-only, do first)

research-os **already has** the device flow: `app/auth/device_router.py` (RFC 8628 + mandatory
S256 PKCE), and the dashboard `/authorize` page already renders the approval UI. Endpoints:

- `POST /auth/device/code` → `{user_code, device_code, verification_uri, verification_uri_complete, interval, expires_in}`
- `GET  /auth/device/requests/{user_code}` → status (dashboard polls)
- `POST /auth/device/requests/{user_code}/decision` → approve/deny (session-gated, dashboard)
- `POST /auth/device/token` → device_code + PKCE verifier → `ros_pat_…`

So "OAuth for the CLI" is a **pure client wire-up** — no backend work:

- Add `exp login --device` (make it the default, keep `--token` as the air-gap fallback):
  1. generate a PKCE verifier/challenge, `POST /auth/device/code`;
  2. open `verification_uri_complete` in the browser (print it for headless);
  3. poll `POST /auth/device/token` at `interval` until approved → store the `ros_pat` via the
     existing `exp login` config write.
- This is the lowest-risk, highest-value slice and needs only `research-os-agent`. Ship it
  independent of the MCP OAuth backend work.

### A. MCP OAuth 2.1 — hosted connector (larger, backend + MCP, prod-gated)

Goal: adding `https://mcp.research.prbe.ai/mcp` as a connector triggers a browser OAuth flow;
no manual token. Two halves:

**A1. research-os API = authorization server** (mirror Phase G). New surface on `api.research.prbe.ai`:

- `GET  /.well-known/oauth-authorization-server` — RFC 8414 metadata
- `GET  /oauth/jwks` — public ES256 signing key
- `POST /oauth/register` — RFC 7591 dynamic client registration (auto-approve, public clients, PKCE-required)
- `GET  /oauth/authorize` — PKCE auth-code. Reuse the existing human session: no `research_session`
  cookie → 302 to `/auth/login?redirect=…` (already built); no active team → 302 to onboarding;
  else render a minimal consent page.
- `POST /oauth/consent` — record consent, issue code
- `POST /oauth/token` — `authorization_code` / `refresh_token`, both rotate refresh (OAuth 2.1 §6.1)

Token: ES256 JWT, `iss=https://api.research.prbe.ai`, `aud=https://mcp.research.prbe.ai/`,
`sub=<customer_id>` (or user+team), `scope="research:read"`, `kid` in header.

**A2. Accept the JWT on `/v1/*` reads.** The single seam is `_resolve_principal` in
`app/auth/dependencies.py` (today: session cookie OR `ros_pat` bearer). Add a
`_resolve_jwt_principal` branch: if the bearer is a JWT (not `ros_pat_`), validate ES256 against
our own JWKS, check `aud`/`iss`/`exp`, map `sub`→tenant, and grant `research:read` scope. RLS is
unchanged (tenant from `sub`, never a header). The hosted MCP stays a **thin proxy** — it forwards
whatever bearer it received; the API validates. (This is simpler than prbe-knowledge, where the
MCP validates the JWT itself because it holds DB access.)

**A3. MCP resource server = discovery + challenge** (`research-os-agent`, `src/ros/mcp/server.py`,
additive, safe — does not break static bearer):

- Serve `GET /.well-known/oauth-protected-resource` (RFC 9728) pointing at the API issuer.
- On missing/invalid bearer, return `401` with
  `WWW-Authenticate: Bearer realm="research", resource_metadata="https://mcp.research.prbe.ai/.well-known/oauth-protected-resource", scope="research:read"`
  (add `error="invalid_token"` for a rejected token). Copy the exact shape from
  `prbe-knowledge/services/mcp/dependencies/auth_context.py::_oauth_challenge_headers`.
- Keep the current `static` path working (env `ROS_MCP_TOKEN` / forwarded `ros_pat`) behind an
  auth-mode flag, exactly like prbe-knowledge's `resolved_auth_mode` (`oauth` | `static`).

**A4. Ingress.** `mcp.research.prbe.ai` already routes `/` to the MCP service, so the
`/.well-known/oauth-protected-resource` path is served by the same pod — no split-role routing
needed (unlike prbe-backend PR #188, whose MCP/back-end shared a host).

**Config/keys.** ES256 keypair for signing (secret) + JWKS exposure; `MCP_OAUTH_*` settings
(issuer, audience, key id, TTLs) on the API. Storage for registered clients + auth codes +
refresh tokens (control DB tables, like Phase G's `app/services/mcp_oauth/storage.py`).

---

## Rollout order

1. **B — `exp login --device`** (client only, backend already live). Ship first.
2. **A3 — MCP discovery + challenge, dual-mode** (client only, additive; static bearer still default). Safe to ship; inert until A1 exists.
3. **A1 + A2 — backend authorization server + JWT acceptance.** Prod change to `api.research.prbe.ai`
   → **gated on explicit go-ahead** (merging to research-os `main` deploys prod). Largest piece;
   port Phase G's `mcp_oauth` routers/services.
4. Flip the plugin `.mcp.json` / Connect page to advertise OAuth; keep static bearer as the
   air-gap/self-host fallback.

## Open questions

1. **Subject granularity:** `sub = customer_id` (tenant-level, like Phase G) vs user+team. Reads are
   tenant-scoped, so customer_id is likely enough; confirm whether per-user read audit matters.
2. **Scopes:** single `research:read` for MCP now; reserve `research:write` if we ever expose writes
   over OAuth (today writes stay on the `exp` PAT).
3. **Reuse vs port:** can we depend on `prbe-control-plane-client` / lift Phase G's `mcp_oauth`
   package directly, or do we re-implement in research-os to avoid a control-plane coupling?
4. **Key management:** where the ES256 private key lives (k8s secret) + rotation story.
