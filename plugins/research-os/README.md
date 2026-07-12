# research-os plugin

Consolidates the Research OS experiment-tracking **skills** + the read-only **MCP
server** into one Claude Code plugin. Reads come from the MCP server; writes go through
the `exp` CLI (installed by `/research-os-setup`).

## Install

```
claude plugin marketplace add prbe-ai/research-os-agent
claude plugin install research-os@research-os-agent
/research-os-setup
```

`/research-os-setup` installs the `exp` CLI, runs `exp login` (write token), and captures a
read-only token into `ROS_MCP_TOKEN` for the MCP server. (Once the plugin is added to the
central prbe-ai marketplace, the install becomes `research-os@prbe-ai`.)

## What's inside

- **Skills:** `experiment` (track a run end to end), `manage-research-asset` (reuse /
  version assets), `publish-experiment` (mint an immutable experiment version).
- **MCP server** (`.mcp.json`): defaults to the hosted endpoint
  `https://mcp.research.prbe.ai/mcp` with `Authorization: Bearer ${ROS_MCP_TOKEN}`
  (read-only). Self-host: point it at a local `research-os-mcp` (see `deploy/mcp/`).
- **Command:** `/research-os-setup`.

Skills here are copies of the repo's canonical `skills/` (kept in sync with
`make sync-plugin-skills`).
