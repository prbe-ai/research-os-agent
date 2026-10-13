# Probe in Capy

[Capy](https://capy.ai) is a coding agent like Claude Code or Codex, with one
difference that shapes everything below: its agent loop runs on Capy's servers,
and its tools run either on your computer (a thread on **This Mac**) or on a
Capy machine in the cloud. It runs no Probe hook and keeps no transcript on your
computer, so Probe meets it in two places:

- **Inside Capy**, the agent reads and records Probe work itself: the Probe MCP
  server for search, the `probe` CLI and SDK for writes and instrumented runs
  (your computer's, for a thread on This Mac), and a small skills bundle that
  says how. Capy has no command or API for its settings, so you add these in
  Capy once; the installer prints the steps.
- **On your computer**, Probe copies the Capy threads you start into Probe as
  sessions, every 10 minutes, through Capy's API.

What Capy does not have: hooks or a status line, so there is no `/probe` switch,
no Probe daemon and no Probe footer in Capy. Capy's API shares your prompts, the
agent's replies and a one-line summary per tool call, never tool output, file
diffs or the model's reasoning, so a Capy session in Probe is thinner than a
Claude Code one.

## Install

Pick Capy in the installer like any other agent:

```bash
npx probe-research install --agent capy
```

(or `npx probe-research install` and tick Capy; it is listed when the Capy app
is on this computer). After the review screen it asks for a Capy API key (Capy
**Settings > API**; give it an expiry) and which Capy projects to capture, then
connects capture and prints the steps below for Capy's own settings. Leave the
key empty to skip capture for now.

Without a terminal (a script, a CI box), set `CAPY_API_KEY` and name the
projects: `npx probe-research install --agent capy --yes --capture --capy-project
<name>`. The key is never an argument, where shell history would keep it.

## 1. Connect the Probe MCP server

In Capy: **Settings > MCP & plugins**, add an MCP server.

- Name: `probe`. Scope: personal (or your org's).
- Transport: **Streamable HTTP**, URL `https://mcp.research.prbe.ai/mcp`.
- Save, click **Connect** and approve in Probe.

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

## 3. Threads in Capy's cloud: put the CLI on the Capy machine

A thread on **This Mac** uses your computer's `probe`, which the installer set up,
and needs nothing here. A thread on a Capy machine in the cloud needs the CLI
there too. In the project's **Environment variables > Personal** tab (personal, so your token
never reaches a teammate's thread or a PR review):

| Name | Value |
|---|---|
| `PROBE_TOKEN` | a Probe token: `probe token create --name capy --scope read --scope write` on your computer (no `delete`: the agent never needs it) |
| `PROBE_CAPY` | `1` (tells the `probe` CLI it runs inside Capy: `probe doctor` reports Capy's own checks, and CLI telemetry is labelled Capy; runs are not labelled with an agent on the server). On This Mac, Capy's own variables already tell it. |

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

## 5. How capture works

The installer runs the same steps as `probe capy connect`, which you can also
run yourself (`probe capy connect --project <your Capy project name>`; the
`probe` CLI must be installed for good, version 0.245.0 or later, because the
timer runs it). You must be signed in to Probe with the same email your Capy
account uses.

Either way it takes the key from a hidden prompt (or `$CAPY_API_KEY`, or
`--api-key-stdin`; never type it as an argument, where shell history keeps it),
finds your Capy user by your Probe account's email, asks you to approve capture
once in the browser (or checks the approval it already has belongs to your
team), and installs a timer that syncs every 10 minutes (systemd on Linux,
launchd on macOS; elsewhere it prints a crontab line and syncs once right away).
The first sync starts in the background; a long history can take a while.

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
- Each time a local coding agent (Claude Code, Codex, pi, Kimi Code) starts a
  session, the capture plugin also starts a sync in the background when the last
  one is over 5 minutes old.
- `probe capy status` shows the last sync; `probe capy sync` runs one now;
  `probe capy disconnect` removes the timer, the key, the sync record and the
  capture credential (sessions already uploaded stay in Probe). Signing out in
  the wizard, or uninstalling Probe, disconnects Capy the same way; Probe's MCP
  server and skills stay in Capy until you remove them in its settings.
- Capture is pinned to the Probe server it was connected to: a shell that points
  Probe elsewhere (`$PROBE_BASE_URL`) syncs nothing.

The key is stored owner-only (0600) beside the capture credential and is sent to
Capy's API only, which `probe capy` reads and never writes.
