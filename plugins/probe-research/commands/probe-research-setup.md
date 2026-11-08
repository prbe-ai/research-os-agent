---
description: Set up Probe Research on this computer from this chat - sign in, connect the coding agents, and bring in work the team already has (past chats, project folders, W&B runs). Agent-run, idempotent; the person only answers questions and approves in the browser.
---

# Set up Probe Research

Fetch https://research.prbe.ai/install.md and follow it. It signs the person in (one
browser approval), installs the CLI, sets Probe up in this computer's coding agents and
offers to bring in past work. On a computer already set up it opens the menu instead:
repair, switch Full / Runs only, settings, account, past work, update, uninstall.

Run every command yourself, in this chat, and spell it `npx -y probe-research …` (`probe`
may not be on PATH yet). Never hand the person a command to type: they answer your
questions and click Allow in the browser tab sign-in opens.

## The commands the pages use

```bash
npx -y probe-research sign-in --json           # opens the approval tab; then:
npx -y probe-research sign-in --wait --json    # waits for the approval and finishes the sign-in
npx -y probe-research install --yes --json     # set up or reinstall, keeping every choice
npx -y probe-research status --json            # what is set up here
npx -y probe-research repair --json            # checks every part, fixes what is broken
npx -y probe-research update --json            # the CLI and the plugins
```

What records less (Runs only, new chats off, sign out, removing an agent, uninstall) also
takes `--directed`, added only after the person said yes to it.

## Confirm

```bash
npx -y probe-research status --json   # account, agents, who records, versions, the daemon
probe session status                  # which state THIS conversation is in (on / daemon / read / off), and why
probe mcp status                      # where the read credential comes from, is it valid
```

In a fresh agent session, the MCP is connected when its tools appear
(`browse`, `entity`, `search`, `metrics`, `find_papers`, `query_sql`). Claude Code
defers MCP tools, so they may be names only until something loads them.

## The MCP wired by hand

```bash
npx -y probe-research sign-in --json               # sign in FIRST: it mints the read token the plugin's
npx -y probe-research sign-in --wait --json        # headers helper reads on first use
/plugin install probe-research@research-os-agent
```

Use it when the MCP has to be wired by hand:

- **Hosted HTTP:** add to the MCP config (`~/.claude.json` or a project
  `.mcp.json`):
  `{"mcpServers": {"probe-research": {"type": "http",
  "url": "https://mcp.research.prbe.ai/mcp",
  "headersHelper": "<path to>/probe-mcp-headers"}}}`
  A client without `headersHelper` can use a static header, but then rotating
  the token means editing that file by hand.
- **Local stdio:** works with any `claude mcp add`, and reads the stored token
  itself. Do **not** pass `-e PROBE_MCP_TOKEN=…` - that pins a literal copy into
  `~/.claude.json` which outlives every rotation and silently wins over the
  token you just set.

The CLI and the plugins ship together. Running one without the other is the usual cause of
a capability that is configured but does nothing.

## Security notes

- The write token and the read-only MCP token are different credentials. The
  MCP one cannot write.
- Who records → the daemon mints a third token for it: it can read and write,
  and its deletes go to the trash or ask the researcher first (unless the agent
  runs with permission prompts off). Setting Who records back to the agent
  revokes it, once no open session uses it. Without that token the daemon
  cannot record: a session it records for reads `on (daemon) | daemon not recording`.
- Never echo a token. `probe mcp status` reports health without printing it.
- Signing out (`npx -y probe-research wizard --action logout --yes --json --directed`)
  revokes this computer's token; it does not touch other devices.
