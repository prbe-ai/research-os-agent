# Probe in Capy

[Capy](https://capy.ai) is a coding agent like Claude Code or Codex, with one
difference that shapes everything below: its agent loop runs on Capy's servers,
and its tools run either on your computer (a thread on **This Mac**) or on a
Capy machine in the cloud. It runs no Probe hook and keeps no transcript on your
computer, so Probe meets it in two places:

- **Inside Capy**, the agent reads and records Probe work itself: the Probe MCP
  server for search, the `probe` CLI and SDK for writes and instrumented runs
  (your computer's, for a thread on This Mac), and a small skills bundle that
  says how. Capy has no command or API for its settings, so you add Probe's
  plugin in Capy once; the installer prints where.
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
connects capture and prints the one step below for Capy's own settings. Leave
the key empty to skip capture for now.

Without a terminal (a script, a CI box), set `CAPY_API_KEY` and name the
projects: `npx probe-research install --agent capy --yes --capture --capy-project
<name>`. The key is never an argument, where shell history would keep it.

## 1. Add the Probe plugin

On a Mac with the Capy app, the installer's last page offers **Open Capy**: it
opens Capy's **Add from GitHub** box with Probe's plugin filled in. Click
**Continue**, choose **Only me**, **Add plugin**, then **Connect** its `probe`
server and approve in Probe. Elsewhere, or later: in Capy, **Settings > MCP &
plugins > Add > From GitHub repository**, and paste:

```
https://github.com/prbe-ai/research-os-agent/tree/main/capy
```

On a Mac, `open "capy://app/settings/mcp?github=https%3A%2F%2Fgithub.com%2Fprbe-ai%2Fresearch-os-agent%2Ftree%2Fmain%2Fcapy"`
opens the same box. If Capy says the key `probe` is already taken, you added Probe's server
by hand before: remove that server, then add the plugin. The plugin
carries Probe's MCP server and the skills bundle (`probe-capy`, `track-work`,
`instrument-code`, `plan-research-review`, `edit-notes`, `audit-team-note`). Capy
shows every skill's description to the agent in each thread, so `probe-capy` is
read at the start of ML work without an AGENTS.md line.

Choose **Only me**, not Organization: with Organization, Capy signs the server
in once for everyone ("signed in for the organization"), so every teammate's
thread reads Probe as whoever clicked Connect.

### By hand (a self-hosted Probe, or without the plugin)

The plugin's server is the hosted Probe (`https://mcp.research.prbe.ai/mcp`).
For another deployment, or to add the parts yourself:

- **MCP server:** Settings > MCP & plugins, add an MCP server named `probe`,
  scope personal, transport **Streamable HTTP**, your deployment's MCP URL.
  Save, click **Connect** and approve in Probe.
- **Skills**, into your repository's `.agents/skills/`:

  ```bash
  npx skills add https://github.com/prbe-ai/research-os-agent/tree/main/capy/skills --agent universal
  ```

  or ask Capy: *"Install the skills in the capy/skills folder of
  prbe-ai/research-os-agent into my personal drive."*

## 2. Threads in Capy's cloud: put the CLI on the Capy machine

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

## 3. Optional: an AGENTS.md line

The skills are enough. To make it explicit, add to your repository's root
`AGENTS.md` (or your Capy personal drive's, which applies to all your threads):

```markdown
## Probe
This team records ML work in Probe. Before ML work, read the `probe-capy` skill and
run `probe notes team --brief`. Check the team's prior work with the Probe MCP
`search` tool before proposing new experiments.
```

## 4. How capture works

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
