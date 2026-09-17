---
description: Install Probe Research on this device - sign in, connect the coding agents, and import work the team already has (saved sessions, project folders, W&B runs). Agent-safe, idempotent, headless-friendly.
---

# Set up Probe Research

One command does the whole install:

```bash
npx probe-research
```

It signs the user in, installs the CLI, configures the coding agents on this machine, and
opens a menu for importing work they already have.

**Show the user each command before running anything that writes** (installs, sign-in,
editing a shell profile, plugin installs). Reads (`--version`, `--help`, `probe doctor`)
do not need announcing.

Safe for any coding agent and safe to re-run. Prefer resolved binary paths over bare names
- the user may have aliases that break bare invocations.

## 0. Preflight (reads only)

- Needs `uv`, `curl`, `git`, `python3`. If `uv` is missing:
  `curl -LsSf https://astral.sh/uv/install.sh | sh`
- `node` only for the `npx` launcher; `probe setup` is the same thing once the
  CLI is installed.
- Claude Code users: check `claude --version` >= 2.1.195. The plugin's MCP
  resolves its credential through a headers helper that older builds ignore.
- Already installed? `probe doctor` prints what is on, what is stale, and
  whether sign-in worked. Start there rather than reinstalling.

## 1. Install and configure

```bash
npx probe-research          # install + guided setup
probe setup                 # same menu, once the CLI exists
probe install               # skip the menu, go straight to the guided install
probe setup --action configure   # the same thing, named: tracking, capture, updates
```

**The flags are the contract; the menu is a front end over them.** An omitted flag
preserves whatever is already configured, so `--yes` in CI never silently revokes a
capability someone turned on.

```
--agent claude|codex|pi|both   which coding agents to configure (both = claude+codex)
--tracking / --no-tracking     tracking skills + the read-only MCP search
--capture  / --no-capture      stream those agents' sessions to the knowledgebase
--agent-rules / --no-...       the managed Probe block in each agent's global instructions
--auto-update / --no-...       keep the CLI and plugins current
--yes                          skip the menu and prompts (headless)
```

Headless example:

```bash
probe setup --agent both --tracking --capture --agent-rules --yes
```

## 2. Sign in

Sign-in happens before the menu, so step 1 usually covers it. On its own:

```bash
probe setup <auth_code>          # the 10-character code the website hands you
probe login                      # browser handoff (RFC 8628), nothing to paste
probe login --token probe_pat_…  # air-gapped paste path
probe login --context staging    # a second endpoint or tenant on one machine
```

Only the researcher types `--token`, in their own terminal - never from an agent tool
call: it would land in shell history and in the captured transcript. Both tokens sit in
plaintext in `~/.config/probe/config.json`.

Switching accounts, or signing in as someone else on a device that already holds
credentials:

```bash
probe setup --action account     # sign in, switch to an account saved here, or sign out
```

**Do not gate success on the exit code alone.** Confirm independently:

```bash
probe doctor
```

## 3. Import work the team already has

A fresh install sees only what happens next. Everything below runs as a background job you
can leave and come back to.

```bash
probe setup --action import-research    # the menu: sessions, a folder, or both
probe setup --action imports            # monitor, see results, resume interrupted work
```

**Saved coding sessions.** Past Claude Code / Codex / pi sessions already on disk,
imported into the knowledgebase and searchable alongside everything else. Distinct from
`--capture`, which streams sessions from now on - one is history, the other is the feed.

```bash
probe setup --action transcripts
```

**A project folder.** Point it at work already done: it uploads what it finds, describes
each artifact, and reports how much of the folder it accounted for. Large files
(checkpoints, datasets) are recorded as references, not copied.

```bash
probe setup --action backfill
probe setup --action backfill --folder /path/to/project   # headless
```

**W&B runs.** Mirrors one W&B run's metric history into an existing probe run, so it is a
per-run command and not part of the wizard menu. Register or pick the probe run first,
then:

```bash
probe import wandb …
```

For a continuously mirrored W&B workspace rather than a one-off, that is an integration
configured in the dashboard, not here.

## 4. Confirm

```bash
probe doctor                  # install, sign-in, capture pairing, staleness
probe setup --action diagnose # the same ground from the menu
probe session status          # is THIS conversation being tracked, and why
probe mcp status              # where the read credential comes from, is it valid
probe setup --action imports  # did the background imports finish
```

In a fresh agent session, the MCP is connected when its tools appear
(`browse`, `entity`, `search_knowledge`, `metrics`, `find_papers`). Claude Code
defers MCP tools, so they may be names only until something loads them.

## Manual and air-gapped installs

```bash
probe mcp token set                          # mint the read token FIRST: the plugin's
/plugin install probe-research@research-os-agent   # headers helper reads it on first use
probe setup --action manual
```

Deliberately not in the menu - it is the rarest path. Use it when the browser handoff
cannot work, or when the MCP has to be wired by hand:

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

## Keeping it updated

```bash
probe update                        # CLI + plugins
probe setup --action settings       # turn auto-update on
```

The CLI and the plugins ship together. Running one without the other is the usual cause of
a capability that is configured but does nothing.

## Removing it

```bash
probe setup --action configure --no-capture              # stop capture, keep the plugin
probe setup --action configure --no-capture --uninstall  # stop capture AND remove the plugin
probe setup --action uninstall                           # remove Probe's plugins from the agents you pick, and sign this device out
probe logout                                             # stop imports, revoke this token, clear local config
```

## Security notes

- The write token and the read-only MCP token are different credentials. The
  MCP one cannot write.
- Never echo a token. `probe mcp status` reports health without printing it.
- `probe logout` revokes the calling token; it does not touch other devices.
