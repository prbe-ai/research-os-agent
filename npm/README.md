# probe-research

The Probe Research setup wizard.

```bash
npx probe-research
```

Installs and configures [Probe Research](https://research.prbe.ai) for Claude Code.
It asks which capabilities you want, installs only those plugins, and finishes
with a single browser approval.

This package is a thin launcher. Probe Research is a Python CLI; this exists so
the entry point matches the `npx <tool>` shape people already expect. It resolves
a real `probe` (via an existing install, `uv`, or `pipx`) and hands over — there
is no reimplementation here that could drift from the CLI.

Re-run it any time to change what's enabled, diagnose a problem, update, print
the manual steps, or remove Probe from the device.
