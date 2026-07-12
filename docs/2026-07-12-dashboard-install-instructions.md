# Dashboard install instructions — gathered info + plan

**Goal:** add a "Connect the CLI + agent" page to the research-os **dashboard** so a
researcher can install and wire up the plugin + `exp` CLI from the UI (including minting
the tokens). This doc is the gathered context and a build plan; the page lives in
`research-os/dashboard`.

## 0. Live facts (verified)

- **Hosted MCP:** `https://mcp.research.prbe.ai/mcp` — LIVE. `/healthz` → `{"status":"ok"}`,
  `/mcp` returns a valid MCP `initialize`, Let's Encrypt TLS, grey-cloud DNS → the ingress LB
  `134.199.137.104`. Read-only, per-request Bearer auth.
- **API base:** `https://api.research.prbe.ai` (`DEFAULT_API_BASE_URL`).
- **Plugin install (no PyPI, no central marketplace yet):**
  ```
  claude plugin marketplace add prbe-ai/research-os-agent
  claude plugin install research-os@research-os-agent
  /research-os-setup
  ```
- **CLI install (git, not PyPI):** `uv tool install "git+https://github.com/prbe-ai/research-os-agent"`
  (or pipx/pip with the same git URL). Provides `exp` + `research-os-mcp`.

## 1. What the page must convey (content)

Three things a user does, in order:

1. **Install the plugin** (Claude Code) — the two `claude plugin …` commands above, then
   `/research-os-setup`. This gives the skills (`track-experiment`, `manage-research-asset`,
   `publish-experiment`) and wires the hosted MCP.
2. **Mint tokens** (right here on the page):
   - a **read-only** token → `export ROS_MCP_TOKEN=<ros_pat_read_…>` (for the MCP server);
   - a **write** token → `exp login --base-url https://api.research.prbe.ai --token <ros_pat_…>`
     (for the CLI write path).
3. **Verify** — `exp whoami` and that the `research-os` MCP tools (`research_*`) are connected.

Also show the **self-host / air-gap** variant (local stdio MCP via
`uvx --from "git+…/research-os-agent" research-os-mcp` + `ROS_MCP_TOKEN`/`ROS_BASE_URL`).

## 2. Where it goes in the dashboard

- **New route:** `src/app/setup/page.tsx` (or `/connect`). App-router page inside the
  authed app shell (not a BARE_PATH). Add to nav: an item in the **account menu**
  (`src/components/shell/account-menu.tsx`, next to `/team` + `/settings`) labeled
  "Connect CLI & agent", and a card/link on **`/onboarding`** so new users find it.
- Alternatively fold it into **`/settings`** as a section — but a dedicated page is cleaner
  and linkable (e.g., from docs / the empty-state of a new project).

## 3. What to reuse (don't rebuild)

- **Token minting:** `src/components/auth/manual-token-authorization.tsx` already mints via
  `mintToken({ name, scopes })` with a **`readOnly` toggle** (`[ApiScope.Read]` vs full role)
  and copy-once UX ("shown only once", Copy button, `lucide-react` icons). The setup page
  should render **two** mint actions — one **read-only** (for `ROS_MCP_TOKEN`) and one
  **read+write** (for `exp login`) — reusing this component or its `mintToken` call
  (`src/lib/api/tokens.ts`).
- **Scopes:** `ApiScope` = Read / Write / Delete / Admin (`src/lib/constants.ts`).
- **Code snippets:** `src/components/ui/json-block.tsx` (or a small `<CodeBlock>` with a copy
  button) for the shell commands. Existing `dash-*` CSS classes + Tailwind + `lucide-react`.
- **Brand/shell:** `ProbeMark`, `app-shell`, the same card styling used on `/authorize`.

## 4. Token model on the page

| Use | Scope to mint | Where it goes |
|---|---|---|
| MCP reads (hosted or local) | **read** only | `ROS_MCP_TOKEN` env |
| CLI writes (`exp`) | read + write (+ delete) | `exp login --token …` |

Two separate tokens keep the MCP surface read-only. Minting stays session-gated (the page is
behind auth), which the existing widget already relies on.

## 5. Notes / open questions

- **Device flow:** the backend + dashboard support browser device-auth (`/authorize` device
  consent), but the `exp` CLI is **paste-token** today (device flow is a future client
  enhancement). So the page's CLI step is paste-token for now; a "Login with browser" button
  is a later upgrade once `exp login --device` exists.
- **Central marketplace:** deferred — install stays `@research-os-agent`. When the plugin is
  added to the central prbe-ai marketplace, update the command to `research-os@prbe-ai`.
- **Copy accuracy:** the exact skill names are `track-experiment`, `manage-research-asset`,
  `publish-experiment`. The MCP tools are `research_context/search/get/compare/resolve/trace_file`.
- **Where to build:** this page is in `research-os/dashboard`; this doc is the spec to hand to
  that work (I can implement it there when you want).
