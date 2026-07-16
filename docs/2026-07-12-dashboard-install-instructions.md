# Dashboard install instructions — gathered info + plan

> **Superseded — historical.** Written before the #14/#15 rename, so the names below are
> stale: `exp` → `probe`, `research-os-mcp` → `probe-research-mcp`, `ROS_*` → `PROBE_*`,
> plugin `research-os@…` → `probe-research@…`. The token no longer goes in a shell
> profile — `probe mcp token set` stores it and the plugin reads it through a headers
> helper. Kept as a record of the plan; do not follow it as instructions. The live copy is
> generated from `NAMES` in `research-os/dashboard/src/components/connect/install-flow.ts`,
> and the current setup steps are in `plugins/probe-research/commands/probe-research-setup.md`.

**Goal:** add a "Connect the CLI + agent" page to the research-os **dashboard** so a
researcher can install and wire up the plugin + `exp` CLI from the UI (including minting
the tokens). This doc is the gathered context and a build plan; the page lives in
`research-os/dashboard`.

## 0. Live facts (verified)

- **Hosted MCP:** `https://mcp.research.prbe.ai/mcp` — LIVE. `/healthz` → `{"status":"ok"}`,
  `/mcp` returns a valid MCP `initialize`, Let's Encrypt TLS, grey-cloud DNS → the ingress LB
  `134.199.137.104`. Read-only, per-request Bearer auth.
- **API base:** `https://api.research.prbe.ai` (`DEFAULT_API_BASE_URL`).
- **Plugin install (no PyPI, no central marketplace yet)** — these are **interactive
  Claude Code REPL commands** typed in the prompt, *not* shell commands. Run headless
  (via an agent's `Bash` tool) they launch the TUI and crash (`Raw mode is not supported`).
  ```
  /plugin marketplace add prbe-ai/research-os-agent
  /plugin install research-os@research-os-agent
  /research-os-setup
  ```
- **CLI install (git, not PyPI):**
  `uv tool install --force "git+https://github.com/prbe-ai/research-os-agent@main"`
  (or pipx/pip with the same git URL). Provides `exp`, `research-os-mcp` (stdio),
  `research-os-mcp-http` (hosted). **Pin `@main`/a tag** so you get the `/v1/me` identity
  fix (#11); older builds `401` on `login --token`/`whoami`. Ensure `~/.local/bin` is on
  PATH (`uv tool update-shell`).
- **Identity:** `GET /v1/me` (bearer-accepting: session, `ros_pat`, or OAuth JWT) → the
  caller's identity. The old `/auth/me` is session-cookie only — do **not** verify tokens
  against it.

## 1. What the page must convey (content)

Three things a user does, in order:

1. **Install** — interactive Claude Code users run the `/plugin …` commands above (skills +
   auto-wired MCP), then `/research-os-setup`. **Any other agent / headless** installs the
   CLI with `uv tool install … @main` and wires the MCP manually (see agent-proofing below).
   Skills are `track-experiment`, `manage-research-asset`, `publish-experiment`.
2. **Mint tokens** (right here on the page):
   - a **read-only** token → `export ROS_MCP_TOKEN=<ros_pat_read_…>` (for the MCP server);
   - a **write** token → `exp login --base-url https://api.research.prbe.ai --token <ros_pat_…>`
     (for the CLI write path).
3. **Verify** — `exp whoami` and that the `research-os` MCP tools (`research_*`) are connected.

Also show the **self-host / air-gap** variant (local stdio MCP via
`uvx --from "git+…/research-os-agent@main" research-os-mcp` + `ROS_MCP_TOKEN`/`ROS_BASE_URL`).

### 1a. Agent-proofing the pasteable prompt

The one-paste prompt is executed by *some* coding agent, often headless. Bake these in so
it succeeds unattended and is safe to re-run (the canonical agent copy lives in
`plugins/research-os/commands/research-os-setup.md` — keep the two in sync):

- **Never emit `claude plugin …` as a shell step.** It's interactive-only and crashes
  headless. Lead with `uv tool install … @main`; mention `/plugin` as an interactive-only
  extra for skills.
- **Resolve binaries, don't trust bare names.** Users alias `claude`; use `command -v` /
  absolute paths. Ensure `~/.local/bin` is on PATH after install (`uv tool update-shell`).
- **Pin the version** (`@main`/tag) — the `/v1/me` fix (#11) is required for `whoami`.
- **Verify identity against `/v1/me`, not `login`'s exit code.** Older CLIs raise before
  persisting config; confirm `~/.config/ros/config.json` contains `"token"` and that
  `curl -H "Authorization: Bearer …" /v1/me` returns 200.
- **Idempotency:** `uv tool install --force`; `grep`-guard the shell-profile append (blind
  `>>` duplicates on re-run); `claude mcp remove` before `claude mcp add`.
- **MCP transport reality:** the plugin's `.mcp.json` is `type: http` (auto-connects once
  `ROS_MCP_TOKEN` is set). For the manual path, some `claude mcp add` builds support only
  `stdio`/`sse` — offer both the hosted-HTTP config snippet and a local-stdio
  `research-os-mcp` fallback.
- **MCP tools don't hot-load** — they appear in the *next* session. Tell the user to
  restart; verify liveness with `curl /healthz` (retry; it can flap `502` mid-rollover)
  and connection with `claude mcp list`.
- **Secrets:** read-only token for the MCP, write token only for `exp login`; both persist
  in plaintext (`~/.config/ros/config.json` `600`, `~/.claude.json`) — don't log or commit.

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
