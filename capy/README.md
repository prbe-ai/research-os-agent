# Probe in Capy

[Capy](https://capy.ai) runs its coding agent on its own servers and its tools on a
Capy machine, so Probe meets it in two places:

- **Inside Capy**, the agent reads and records Probe work itself: the Probe MCP
  server for search, the `probe` CLI and SDK on the Capy machine for writes and
  instrumented runs, and a small skills bundle that says how.
- **On your own computer**, `probe capy sync` copies the Capy threads you start
  into Probe as sessions, every 10 minutes.

What Capy does not have: hooks or a status line, so there is no `/probe` switch,
no Probe daemon and no Probe footer in Capy. Capy's API shares your prompts, the
agent's replies and a one-line summary per tool call, never tool output, file
diffs or the model's reasoning, so a Capy session in Probe is thinner than a
Claude Code one.

## 1. Connect the Probe MCP server

In Capy: **Settings > MCP servers > New MCP server**.

- Key: `probe`. Availability: your project (or org-wide).
- Transport: **HTTP**, URL `https://mcp.research.prbe.ai/mcp`.
- Save, open it, click **Connect** and approve in Probe.

Use a **personal** connection (each person connects their own). A shared
connection makes every Probe write from any teammate's thread look like it came
from the admin who connected it.

## 2. Install the skills

The Capy bundle lives in `capy/skills/` of
[prbe-ai/research-os-agent](https://github.com/prbe-ai/research-os-agent):
`probe-capy` (read first), `track-work`, `instrument-code`,
`plan-research-review`, `edit-notes`, `notes-audit`. Install from that folder
(the repository root also carries the Claude Code plugin's own copies, so name
the folder, not just the repository), into your repository's `.agents/skills/`:

```bash
npx skills add https://github.com/prbe-ai/research-os-agent/tree/main/capy/skills --agent universal
```

or ask Capy: *"Install the skills in the capy/skills folder of
prbe-ai/research-os-agent into my personal drive."*

## 3. Put the CLI on the Capy machine

In the project's **Environment variables > Personal** tab (personal, so your token
never reaches a teammate's thread or a PR review):

| Name | Value |
|---|---|
| `PROBE_TOKEN` | a Probe token: `probe token create --name capy --scope read --scope write` on your computer (no `delete`: the agent never needs it) |
| `PROBE_CAPY` | `1` (marks runs and CLI calls as coming from Capy) |

Then add the CLI to the machine recipe, in **Dev environment** or a committed
`.capy/setup.json`:

```json
{
  "$schema": "https://api.capy.ai/schemas/setup.v1.json",
  "format_version": 1,
  "scripts": {
    "initialize": "uv tool install probe-research",
    "refresh": "uv tool upgrade probe-research"
  }
}
```

Code that calls the SDK (`import probe`) also needs `probe-research` in its own
environment (`uv add probe-research`); the CLI tool install above is isolated.

## 4. Tell the agent (AGENTS.md)

Add to your repository's root `AGENTS.md` (or your Capy drive's):

```markdown
## Probe
This team records ML work in Probe. Before ML work, read the `probe-capy` skill and
run `probe notes team --brief`. Check the team's prior work with the Probe MCP
`search` tool before proposing new experiments.
```

## 5. Capture your Capy threads (on your own computer)

You need the `probe` CLI there, version 0.237.0 or later, installed for good
(`uv tool install probe-research`; the timer runs that `probe`, so a throwaway
`npx` copy cannot host it), and a Capy API key (Capy **Settings > API**; give it
an expiry). You must be signed in to Probe (`probe login`) with the same email
your Capy account uses. Then:

```bash
probe capy connect --project <your Capy project name>
```

It asks for the key with a hidden prompt (or reads `$CAPY_API_KEY`, or
`--api-key-stdin`; never type it as an argument, where shell history keeps it),
finds your Capy user by your Probe account's email, asks you to approve capture
once in the browser (or checks the approval it already has belongs to your
team), and installs a timer that syncs every 10 minutes (systemd on Linux,
launchd on macOS; elsewhere it prints a crontab line). The first sync starts in
the background; a long history can take a while.

- Only threads **you** started are captured, in the projects you named: a Capy key
  can read teammates' threads, and capturing them would need their consent.
  Messages teammates add to **your** threads are part of them and are captured.
- Captured: your prompts, the agent's replies, attachment names and a one-line
  summary per tool call. Not captured: tool output, file contents, diffs, the
  model's reasoning. Likely credentials are replaced before upload, which is a
  filter, not a guarantee: keep live credentials out of prompts.
- One thread is one Probe session; each subagent is its own, linked to its
  thread. A thread quiet for a day (or archived) is finished; new messages
  reopen it.
- Your computer being off loses nothing: the next sync catches up. On Linux,
  user timers stop when you log out unless lingering is on
  (`loginctl enable-linger`); `connect` says so when it is off.
- `probe capy status` shows the last sync; `probe capy sync` runs one now;
  `probe capy disconnect` removes the timer, the key, the sync record and the
  capture credential (sessions already uploaded stay in Probe).

The key is stored owner-only (0600) beside the capture credential and is sent to
Capy's API only, which `probe capy` reads and never writes.
