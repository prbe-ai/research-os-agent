---
description: Install the Research OS CLI and connect this project (login + MCP read token). Agent-safe, idempotent, headless-friendly.
---

# Set up Research OS

Run the steps in order. **Show the user each command before running any command that
writes** (installs, `exp login`, editing a shell profile, `claude mcp add`). Reads
(`--version`, `--help`, `curl` GETs, `git status`) don't need to be pre-announced.

This command is written to be safe for **any coding agent** and safe to **re-run**
(idempotent). Prefer absolute/resolved binary paths over bare names — the user may
have shell aliases (e.g. `claude` aliased) that break bare invocations.

## 0. Preflight (reads only)

- Required tools: `uv`, `curl`, `git`, `python3`. Optional: `gh` (only if the repo is
  private and git has no other credentials), `claude` (only for the plugin/MCP wiring).
  If `uv` is missing: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- If the repo is private, confirm git can read it: `gh auth status` (or a configured git
  credential helper). If neither, stop and have the user run `gh auth login`.
- Do **not** run `claude plugin …` from a shell/`Bash` tool. Those are **interactive
  Claude Code REPL commands**; run headless they launch the TUI and crash with
  `Raw mode is not supported`. Install the CLI with `uv` (Step 1); do the plugin/skills
  install interactively (Step 1, "Skills") only if the user is in an interactive session.

## 1. Install the CLI + MCP servers

**Primary path (works for every agent, headless-safe):**

```bash
uv tool install --force "git+https://github.com/prbe-ai/research-os-agent@main"
uv tool update-shell          # ensure ~/.local/bin is on PATH
command -v exp && exp --version
```

- Pin to `@main` (or a released tag/commit) so you get the **`/v1/me` identity fix (#11)**.
  Older builds identify via the session-only `/auth/me`, so `login --token`/`whoami` 401
  with `missing session cookie`. If you have an old build: `uv tool upgrade research-os-agent`.
- `--force` makes re-runs safe. Installs three executables: `exp`, `research-os-mcp`
  (local stdio server), `research-os-mcp-http` (hosted server).
- If `exp` isn't found after install, `~/.local/bin` isn't on this shell's PATH — re-run
  `uv tool update-shell` or `export PATH="$HOME/.local/bin:$PATH"`.

**Skills (interactive Claude Code only, optional):** to get the `track-experiment`,
`manage-research-asset`, and `publish-experiment` skills plus the auto-wired `.mcp.json`,
the user types these **in the Claude Code prompt** (not via an agent shell):

```
/plugin marketplace add prbe-ai/research-os-agent
/plugin install research-os@research-os-agent
```

Skip this block entirely in a headless/agent session — Steps 2–4 don't need it.

## 2. Log in (write token)

Ask for the base URL (default `https://api.research.prbe.ai`) and a **write** API token
(`ros_pat_…`, minted in the dashboard). Then:

```bash
exp login --base-url https://api.research.prbe.ai --token ros_pat_xxxxxxxx
exp whoami            # prints your identity (resolves via /v1/me)
```

- `login`/`whoami` verify against `/v1/me`, which accepts a `ros_pat` bearer (as of #11).
- **Don't gate success on `login`'s exit code alone.** Confirm the token independently and
  that config actually persisted:
  ```bash
  curl -fsS -H "Authorization: Bearer ros_pat_xxxxxxxx" https://api.research.prbe.ai/v1/me >/dev/null && echo "token OK"
  grep -q '"token"' ~/.config/ros/config.json && echo "config persisted"
  ```
  (Older CLIs raise on the `/auth/me` verify *before* saving config — the `grep` catches that.)

## 3. MCP read token (env, persisted idempotently)

Ask for a **read-only** PAT (a `scopes:['read']` token) — a separate token keeps the MCP
surface read-only. Export it and persist it to the correct shell profile **without
duplicating** on re-run:

```bash
export ROS_MCP_TOKEN=ros_pat_read_xxxxxxxx
PROFILE="$HOME/.zshrc"; [ "$(basename "${SHELL:-}")" = "bash" ] && PROFILE="$HOME/.bashrc"
grep -q 'ROS_MCP_TOKEN' "$PROFILE" || echo "export ROS_MCP_TOKEN=$ROS_MCP_TOKEN" >> "$PROFILE"
```

The plugin's `.mcp.json` reads `${ROS_MCP_TOKEN}`, so it must be present in the environment
Claude Code is launched with (hence persisting to the profile).

## 4. Connect the MCP server

**If installed as a plugin:** the bundled `.mcp.json` (`type: http` → the hosted server)
connects automatically once `ROS_MCP_TOKEN` is set. **Restart the session** so the loader
picks up the token — MCP tools do not hot-load into the running session.

**If NOT using the plugin (uv-only or a non-Claude agent):** register the server manually.
Note a transport caveat — some `claude` builds' `claude mcp add` support only `stdio`/`sse`
(not `http`). Pick whichever your build allows:

```bash
# (a) Hosted HTTP — add to your MCP config (~/.claude.json or project .mcp.json):
#     { "mcpServers": { "research-os": { "type": "http",
#       "url": "https://mcp.research.prbe.ai/mcp",
#       "headers": { "Authorization": "Bearer ${ROS_MCP_TOKEN}" } } } }
#
# (b) Local stdio — works with any `claude mcp add`:
claude mcp remove research-os -s user 2>/dev/null   # idempotent re-add
claude mcp add -s user -e ROS_MCP_TOKEN="$ROS_MCP_TOKEN" \
  research-os -- "$(command -v research-os-mcp)"     # absolute path: claude's spawn env may lack ~/.local/bin
```

Verify the hosted service is reachable (retry — `/healthz` can flap `502` mid-rollover):

```bash
for i in 1 2 3; do curl -fsS https://mcp.research.prbe.ai/healthz | grep -q '"status":"ok"' && { echo ok; break; }; sleep 2; done
```

*Self-host / air-gap:* run a local server
(`uvx --from "git+https://github.com/prbe-ai/research-os-agent@main" research-os-mcp`
with `ROS_MCP_TOKEN` + `ROS_BASE_URL`) and point your MCP config at it.

## 5. Confirm

- `exp whoami` prints the signed-in identity.
- `claude mcp list` shows `research-os` as `✓ Connected` (if registered via the CLI).
- The `research-os` MCP tools (`research_context`, `research_search`, `research_get`,
  `research_compare`, `research_resolve`, `research_trace_file`) are available **in a fresh
  session** — remind the user to restart if they were just added.
- The `track-experiment`, `manage-research-asset`, and `publish-experiment` skills are ready
  (plugin installs only).

## Security notes

- Tokens land in plaintext in `~/.config/ros/config.json` (chmod `600`) and, if you pass
  `-e ROS_MCP_TOKEN=…` to `claude mcp add`, in `~/.claude.json`. Never echo tokens into
  logs or commit them. Use a **read-only** token for the MCP and a **write** token only for
  `exp login`.
