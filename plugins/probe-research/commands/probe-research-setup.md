---
description: Install the Probe Research CLI and connect this project (login + MCP read token). Agent-safe, idempotent, headless-friendly.
---

# Set up Probe Research

Run the steps in order. **Show the user each command before running any command that
writes** (installs, `probe login`, editing a shell profile, `claude mcp add`). Reads
(`--version`, `--help`, `curl` GETs, `git status`) don't need to be pre-announced.

This command is written to be safe for **any coding agent** and safe to **re-run**
(idempotent). Prefer absolute/resolved binary paths over bare names — the user may
have shell aliases (e.g. `claude` aliased) that break bare invocations.

## 0. Preflight (reads only)

- Required tools: `uv`, `curl`, `git`, `python3`. Optional: `gh` (only if the repo is
  private and git has no other credentials), `claude` (only for the plugin/MCP wiring).
  If `uv` is missing: `curl -LsSf https://astral.sh/uv/install.sh | sh`.
- **If using the plugin, check `claude --version` ≥ 2.1.195.** The plugin's MCP resolves
  its token via `headersHelper: "${CLAUDE_PLUGIN_ROOT}/…"`, and that placeholder is only
  interpolated from 2.1.195 on. On an older build it is passed through literally, the
  helper never runs, and you get `✘ Failed to connect` with nothing explaining it. There
  is no manifest field that enforces this. Tell the user to run `claude update`, or use
  the non-plugin path in Step 4 with a static header.
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
command -v probe && probe --version
```

- Pin to `@main` (or a released tag/commit) so you get the **`/v1/me` identity fix (#11)**.
  Older builds identify via the session-only `/auth/me`, so `login --token`/`whoami` 401
  with `missing session cookie`. If you have an old build: `uv tool upgrade probe-agent`
  (the distribution is `probe-agent`; the repo name is not the package name).
- `--force` makes re-runs safe. Installs three executables: `probe`, `probe-research-mcp`
  (local stdio server), `probe-research-mcp-http` (hosted server).
- If `probe` isn't found after install, `~/.local/bin` isn't on this shell's PATH — re-run
  `uv tool update-shell` or `export PATH="$HOME/.local/bin:$PATH"`.

**Skills (interactive Claude Code only, optional):** to get the `track-experiment`,
`manage-research-asset`, and `publish-experiment` skills plus the auto-wired `.mcp.json`,
the user types these **in the Claude Code prompt** (not via an agent shell):

```
/plugin marketplace add prbe-ai/research-os-agent
/plugin install probe-research@research-os-agent
```

Skip this block entirely in a headless/agent session — Steps 2–4 don't need it.

## 2. Log in (write token)

Ask for the base URL (default `https://api.research.prbe.ai`) and a **write** API token
(`probe_pat_…`, minted in the dashboard). Then:

```bash
probe login --base-url https://api.research.prbe.ai --token probe_pat_xxxxxxxx
probe whoami            # prints your identity (resolves via /v1/me)
```

- `login`/`whoami` verify against `/v1/me`, which accepts a `probe_pat` bearer (as of #11).
- **Don't gate success on `login`'s exit code alone.** Confirm the token independently and
  that config actually persisted:
  ```bash
  curl -fsS -H "Authorization: Bearer probe_pat_xxxxxxxx" https://api.research.prbe.ai/v1/me >/dev/null && echo "token OK"
  grep -q '"token"' ~/.config/probe/config.json && echo "config persisted"
  ```
  (Older CLIs raise on the `/auth/me` verify *before* saving config — the `grep` catches that.)

## 3. MCP read token

The MCP surface is read-only, so it gets its own `scopes:['read']` token — separate from
the write token in Step 2, and never the same one.

```bash
probe mcp token set          # interactive: mints a read-only token in the browser
```

Nothing is pasted, so the secret never lands in `ps` output or shell history.

**Agents and headless sessions: always pass `--token`.** Bare `set` waits on a browser
approval that nobody is there to give, and blocks until the code expires:

```bash
probe mcp token set --token probe_pat_read_xxxxxxxx
```

`set` stores it in `~/.config/probe/config.json` and **replaces** on re-run, so this is
also how you rotate. It refuses a token the API already rejects, refuses a write-scoped
token unless you pass `--allow-write`, and tells you whether it actually verified.

Do **not** hand-edit a shell profile. The plugin reads the token through a helper at
connect time, so no environment variable is needed and a dock-launched Claude Code works
too. For a non-Claude MCP client that only reads the environment, `probe mcp env` prints
the export line for you to place yourself.

`PROBE_MCP_TOKEN` still wins if your shell exports it, so nothing breaks on upgrade — but
**an old `export PROBE_MCP_TOKEN=…` left in a profile will shadow every rotation you do
here**, and it cannot self-heal: the helper keeps re-emitting the exported value. If
`probe mcp status` reports the token came from the environment, delete that line from the
profile and open a new shell.

## 4. Connect the MCP server

**If installed as a plugin:** the bundled `.mcp.json` connects on its own — its helper
reads the token at connect time. If the session is already running, restart it or
reconnect the server; a live session does not re-read a server it already loaded.

**If NOT using the plugin (uv-only or a non-Claude agent):** register the server yourself.
Note a transport caveat — some `claude` builds' `claude mcp add` support only `stdio`/`sse`
(not `http`). Pick whichever your build allows:

```bash
# (a) Hosted HTTP — add to your MCP config (~/.claude.json or project .mcp.json):
#     { "mcpServers": { "probe-research": { "type": "http",
#       "url": "https://mcp.research.prbe.ai/mcp",
#       "headersHelper": "<path to>/probe-mcp-headers" } } }
#     A client without headersHelper can use a static header instead, but then
#     rotating the token means editing this file by hand.
#
# (b) Local stdio — works with any `claude mcp add`. It reads the stored token itself,
#     so do not pass -e PROBE_MCP_TOKEN=…: that pins a literal copy into ~/.claude.json
#     which outlives every rotation and silently wins over the one you just set.
claude mcp remove probe-research -s user 2>/dev/null   # idempotent re-add
claude mcp add -s user probe-research -- "$(command -v probe-research-mcp)"  # absolute path: claude's spawn env may lack ~/.local/bin
```

Only register this manually if you are **not** running the plugin. A second server named
`probe-research` alongside the plugin's own breaks the connection.

Verify the hosted service is reachable (retry — `/healthz` can flap `502` mid-rollover):

```bash
for i in 1 2 3; do curl -fsS https://mcp.research.prbe.ai/healthz | grep -q '"status":"ok"' && { echo ok; break; }; sleep 2; done
```

*Self-host / air-gap:* run a local server
(`uvx --from "git+https://github.com/prbe-ai/research-os-agent@main" probe-research-mcp`
with `PROBE_MCP_TOKEN` + `PROBE_BASE_URL`) and point your MCP config at it.

## 5. Confirm

- `probe whoami` prints the signed-in identity.
- `probe mcp status` — the one to reach for when the MCP misbehaves. It reports which
  source the token came from (a shell export shadows the stored one), whether the API
  still accepts it, and whether a stale literal copy is pinned somewhere that outranks it.
- `claude mcp list` shows `probe-research` as `✓ Connected` (if registered via the CLI).
- The `probe-research` MCP tools (`research_context`, `research_search`, `research_get`,
  `research_compare`, `research_resolve`, `research_trace_file`) are available **in a fresh
  session** — remind the user to restart if they were just added.
- The `track-experiment`, `manage-research-asset`, and `publish-experiment` skills are ready
  (plugin installs only).

## Security notes

- Both tokens land in plaintext in `~/.config/probe/config.json` (chmod `600`): `token`
  writes, `mcp_token` is read-only. They stay separate on purpose — the MCP one is handed
  to a client, a wider blast radius than the CLI, so it must not be able to write.
- Never echo a token into logs or commit one. `probe mcp status` prints a fingerprint
  rather than the secret; `probe mcp env` does print it, which is the point of that command.
- Prefer `probe mcp token set` with no `--token`: the browser flow keeps the secret out of
  `ps` output and shell history.
