# Probe Research clients — distribution mirror

This repository is a **CI-generated distribution mirror**. Nobody commits
here: every commit is pushed by the release pipeline of Probe's private
monorepo, and the entire tree is derivable from that repo at any commit.
Pull requests and hand-pushed commits are not accepted — the next sync would
overwrite them.

What it serves:

- **Claude Code / Codex plugin marketplace** — `probe-research@research-os-agent`
  (experiment-tracking skills + read-only MCP wiring),
  `probe-research-daemon@research-os-agent` (the daemon profile: the Probe daemon
  records and reads, installed by `probe wizard` in place of `probe-research`) and
  `probe-research-tap@research-os-agent` (client-sanitized transcript capture).
  Installed plugins update from this repo's `main`. pi installs the
  `plugins/probe-research-pi` package from this repo's root `package.json`, and
  Kimi Code installs these plugins from a checkout of it.
- **`capy/`** — Probe for [Capy](https://capy.ai), a cloud coding agent: one
  plugin added in Capy (Settings > MCP & plugins > Add > From GitHub repository,
  this folder's URL) carrying Probe's MCP server and `capy/skills/`, plus
  `capy/README.md`, the setup guide. Capy's threads are captured from the
  researcher's own computer by `probe capy connect`.
- **`client-version.json`** — the version/staleness advisory manifest the
  Probe Research backend reads over GitHub raw.
- **`CHANGELOG.md`** and the [Releases](../../releases) page — the public
  release record for the `probe-research` CLI/SDK
  ([PyPI](https://pypi.org/project/probe-research/),
  `npx probe-research` on npm).

Install / getting started: https://research.prbe.ai/connect

Issues and contributions: the source of truth is private; report problems via
your Probe contact or the support channels on the site above. Licensed under
Apache-2.0 (see LICENSE).
