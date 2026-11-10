# Probe

Experiment tracking for ML teams and the coding agents that run their experiments: Claude Code,
Codex, Cursor, pi and Kimi Code. Keep working the way you do. From now on:

- You'll always know how any result was made.
- Your agent won't repeat what your team already tried.
- You can pick up any experiment where it left off, or hand it off.

Set it up from your coding agent. Paste this:

> Install Probe on this machine and its coding agents. Read https://research.prbe.ai/install.md and follow it.

Or run `npx probe-research` yourself. For a training script, `pip install probe-research` gives
you the Python SDK. To add only the skill that wires the SDK into training code:
`npx skills add prbe-ai/research-os-agent --skill instrument-code`.

[Website](https://www.useprobe.ai) · [Docs](https://docs.prbe.ai) ·
[PyPI](https://pypi.org/project/probe-research/) · [npm](https://www.npmjs.com/package/probe-research)

## This repository

This repository is a **CI-generated distribution mirror**. Nobody commits
here: every commit is pushed by the release pipeline of Probe's private
monorepo, and the entire tree is derivable from that repo at any commit.
Pull requests and hand-pushed commits are not accepted — the next sync would
overwrite them.

What it serves:

- **Claude Code / Codex plugin marketplace** — `probe-research@research-os-agent`
  (experiment-tracking skills + read-only MCP wiring),
  `probe-research-daemon@research-os-agent` (the daemon profile: the Probe daemon
  records and reads, installed by `probe wizard` in place of `probe-research@research-os-agent`) and
  `probe-research-tap@research-os-agent` (client-sanitized transcript capture).
  Installed plugins update from this repo's `main`. pi installs the
  `plugins/probe-research-pi` package from this repo's root `package.json`, and
  Kimi Code installs these plugins from a checkout of it.
- **`skills/instrument-code`** — the skill that wires the Probe SDK into training
  code, on its own, for `npx skills add` (rendered from the `probe-research@research-os-agent` plugin).
- **`client-version.json`** — the version/staleness advisory manifest the
  Probe backend reads over GitHub raw.
- **`CHANGELOG.md`** and the [Releases](../../releases) page — the public
  release record for the `probe-research` CLI/SDK
  ([PyPI](https://pypi.org/project/probe-research/),
  `npx probe-research` on npm).

Issues and contributions: the source of truth is private; report problems via
your Probe contact or the support channels on the site above. Licensed under
Apache-2.0 (see LICENSE).
