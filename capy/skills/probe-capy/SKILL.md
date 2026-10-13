---
name: probe-capy
description: How Probe works inside Capy (capy.ai). Read it at the start of any ML work in a Capy thread - training, evaluation, sweeps, paper reproduction, research planning - before you record anything in Probe or check the team's prior work.
---
# Probe in Capy

Probe is the team's system of record for ML work: projects, experiments, runs,
notes and files. In Capy you record your own work in it; nothing records it for
you.

## What is different here

- **No switch, no daemon.** There is no `/probe on|read|off` and no Probe daemon
  in Capy. Recording is on: follow the `track-work` skill as written. Where a
  skill mentions the daemon or the switch, it does not apply here.
- **Prior work: search, not `probe ask`.** `probe ask` needs the daemon and
  exits with an error in Capy. Search the team's history with the Probe MCP
  server's `search` tool (Capy lists it as `<server key>__search`, e.g.
  `probe__search`; its `entity` tool reads results in full). If no Probe MCP
  tool is available, tell the researcher once that Probe's MCP server is not
  connected in Capy (Settings > MCP & plugins: add the Probe Research plugin,
  or Connect its `probe` server), then use
  `probe project list` and `probe run list --project <slug>`.
- **The team note.** At the start of ML work run `probe notes team --brief` (or
  the MCP `entity` tool with `refs=["team-note"]`) and follow it: it is the team's working
  memory. After editing it, run `probe notes sync`; nothing syncs it for you.
- **Sessions.** `probe session ...` commands need a session id Capy does not
  give this machine, so they fail here; skip them. Your Capy thread itself is
  captured as a Probe session from the researcher's own machine
  (`probe capy sync`), so do not try to upload it.

## When `probe` is missing or signed out

The project's `.capy/setup.json` should install the CLI
(`uv tool install probe-research`) and the researcher's Capy environment
variables should carry `PROBE_TOKEN` and `PROBE_CAPY=1`. If `probe` is not
found, or answers that it is not signed in, say so once, name what is missing,
and continue the work without recording; never invent results or claim
something was recorded. Never print `PROBE_TOKEN` or any other credential: the
output of every command you run is kept in the Capy transcript.

## Instrumenting training code

Use the `instrument-code` skill as written: `probe.init(...)` / `probe.log(...)`
inside the script. The SDK reads `PROBE_TOKEN` from the environment; the
script's environment needs `probe-research` installed (`uv add probe-research`
or `pip install probe-research`), which the CLI tool install does not provide.
