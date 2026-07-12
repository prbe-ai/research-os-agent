---
description: Install the Research OS CLI and connect this project (login + MCP read token).
---

# Set up Research OS

Run these in order. Show the user each command before running any write.

1. **CLI present?** If `exp --version` fails, install it:
   `uv tool install research-os-agent` (or `pipx install research-os-agent`, or `pip install research-os-agent`).

2. **Log in (write token).** Ask for the base URL (default `https://api.research.prbe.ai`)
   and a **write** API token (`ros_pat_…`, minted in the dashboard), then:
   `exp login --base-url <URL> --token <ros_pat_…>` and confirm it prints the signed-in identity.

3. **MCP read token.** Ask for a **read-only** PAT (a `scopes:['read']` token minted in the
   dashboard) for the MCP server, and have the user export it so the plugin's MCP declaration
   (`Authorization: Bearer ${ROS_MCP_TOKEN}`) can read it:
   `export ROS_MCP_TOKEN=<ros_pat_read_…>` (add it to their shell profile to persist).
   A separate read-only token keeps the MCP surface read-only; reusing the write token works
   but is not recommended.

4. **Verify the hosted MCP** is reachable: `curl -s https://mcp.research.prbe.ai/healthz`
   should return `{"status":"ok"}`.
   *Self-host / air-gap:* instead of the hosted URL, run a local server
   (`uv tool run --from research-os-agent research-os-mcp` with `ROS_MCP_TOKEN` +
   `ROS_BASE_URL`) and point the plugin's `.mcp.json` at it.

5. **Confirm.** The `research-os` MCP server should be connected (its `research_*` tools
   available) and `exp` logged in. You can now use the **track-experiment**,
   **manage-research-asset**, and **publish-experiment** skills.
