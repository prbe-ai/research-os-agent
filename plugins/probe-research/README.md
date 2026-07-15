# probe-research plugin

Consolidates the Probe Research experiment-tracking **skills** + the read-only **MCP
server** into one Claude Code plugin. Reads come from the MCP server; writes go through
the `probe` CLI (installed by `/probe-research-setup`).

## Two client surfaces

Probe Research exposes experiment tracking through two separate surfaces over the same backend, for two different workflows:

- **`probe` — SDK + CLI (non-agent).** A Python library (`import probe`) and the `probe` command-line tool for integrating with existing setups and manual experimentation. Drop it into a training script or pipeline to record runs, metrics, spans, and artifacts. No agent required.
- **`probe-research` — plugin: skills + MCP (agent-centric).** Installed into a coding agent (e.g. Claude Code). Its skills teach the agent the experiment workflow, its read-only MCP server lets the agent query experiment state, and writes flow through the `probe` CLI. This is the surface for agent-driven research loops such as Anthrogen.

Same backend, two entry points: humans-in-code reach for the SDK/CLI; agents-in-the-loop use the plugin.

## Install

```
claude plugin marketplace add prbe-ai/research-os-agent
claude plugin install probe-research@research-os-agent
/probe-research-setup
```

`/probe-research-setup` installs the `probe` CLI, runs `probe login` (write token), and stores a
separate read-only token for the MCP via `probe mcp token set`. (Once the plugin is added to the
central prbe-ai marketplace, the install becomes `probe-research@prbe-ai`.)

## What's inside

- **Skills:** `track-experiment` (track a run end to end), `manage-research-asset` (reuse /
  version assets), `publish-experiment` (mint an immutable experiment version).
- **Requires Claude Code ≥ 2.1.195.** The MCP passes its credential through a headers
  helper addressed as `${CLAUDE_PLUGIN_ROOT}/bin/probe-mcp-headers`; that placeholder is
  only interpolated from 2.1.195 on. Older builds pass it through literally, the helper
  never runs, and the server fails to connect with no clue as to why — there is no
  manifest field to declare this, so `claude --version` is the check. (Live rotation
  without a restart additionally wants ≥ 2.1.193, which is implied by the above.)
- **MCP server** (`.mcp.json`): defaults to the hosted endpoint
  `https://mcp.research.prbe.ai/mcp` (read-only). `bin/probe-mcp-headers` supplies the
  Authorization header at connect time, reading `PROBE_MCP_TOKEN` or the stored
  `mcp_token` — so no shell profile is involved and a dock-launched Claude Code works.
  Self-host: point it at a local `probe-research-mcp` (see `deploy/mcp/`).
- **Command:** `/probe-research-setup`.

Skills here are copies of the repo's canonical `skills/` (kept in sync with
`make sync-plugin-skills`).
