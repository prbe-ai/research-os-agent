# probe-research plugin

Consolidates the Probe Research research-tracking **skills** + the read-only **MCP
server** into one Claude Code or Codex plugin. Reads come from the MCP server; writes go
through the `probe` CLI.

## Two client surfaces

Probe Research tracks the team's ML work — experiments and training runs, but also surveys, design decisions, data processing and provisioning — through two separate surfaces over the same backend, for two different workflows:

- **`probe` — SDK + CLI (non-agent).** A Python library (`import probe`) and the `probe` command-line tool for integrating with existing setups and manual experimentation. Drop it into a training script or pipeline to record runs, metrics, spans, and artifacts. No agent required.
- **`probe-research` — plugin: skills + MCP (agent-centric).** Installed into Claude Code or Codex. Its skills teach the agent the tracking workflow, its read-only MCP server lets the agent query research state, and writes flow through the `probe` CLI. This is the surface for agent-driven research loops such as Anthrogen.

Same backend, two entry points: humans-in-code reach for the SDK/CLI; agents-in-the-loop use the plugin.

## Install

### Codex

```bash
codex plugin marketplace add /path/to/research-os-agent
codex plugin add probe-research@research-os-agent
codex mcp login probe-research
```

Start a new Codex thread after installation. Codex uses its native OAuth flow for the
hosted MCP server; no token helper or shell-profile credential is needed.

### Claude Code

```bash
claude plugin marketplace add prbe-ai/research-os-agent
claude plugin install probe-research@research-os-agent
/probe-research-setup
```

`/probe-research-setup` installs the `probe` CLI, runs `probe login` (write token), and stores a
separate read-only token for the MCP via `probe mcp token set`. (Once the plugin is added to the
central prbe-ai marketplace, the install becomes `probe-research@prbe-ai`.)

## What's inside

- **Skills:** `start-research-work` (open a tracked project, experiment and run),
  `track-research-work` (capture, verify and close it), `capture-run-inputs`
  (record reproducibility inputs), and `show-research-timeline` (render the
  research arc in-session). Claude Code and Codex load this same directory.
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
- **Hooks** (`hooks/`): a SessionStart version nudge, and session-funnel
  telemetry (`telemetry.py`) that reports which step of the tracking workflow
  actually happened — session started, MCP used, tracking skill invoked,
  `probe` CLI write ran — as metadata-only PostHog events (ids, versions,
  whitelisted names; never prompts, commands, paths or file contents).
  Observability only: hooks never block, the sender runs detached with a 3s
  timeout, and every failure is silent. Disable entirely with
  `PROBE_TELEMETRY=off`; a non-hosted `base_url` (self-host) also disables it.
  The shared contract (key, killswitch, hosted gate, identity, batch shape)
  lives in `hooks/_telemetry_core.py` — a vendored copy of the CLI's
  `src/probe/cli/_telemetry_core.py`, refreshed by `make sync-telemetry-core`
  and byte-parity-tested, so plugin events stay joinable with the CLI's
  `wizard.*`/`backfill.*` install funnel.

Skills here are copies of the repo's canonical `skills/` (kept in sync with
`make sync-plugin-skills`). The two agent-specific manifests are thin packaging
adapters around this one implementation.
