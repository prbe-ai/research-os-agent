# Codex integration: implementation and release gates

The Codex integration is split across two repositories:

1. `research-os-agent/plugins/probe-research` packages the tracking skills and
   read-only MCP server for Codex. Codex authenticates the MCP connection with
   native OAuth.
2. `research-os` pairs `source=codex` devices and accepts only their tokens at
   `POST /ingest/v1/sessions/codex`, then forwards to the engine's
   `/webhooks/codex` connector after tenant/source validation.
3. `research-os-agent/plugins/probe-research-tap` is one dual-target capture
   plugin. Claude Code and Codex share its pairing, durable outbox, HTTP,
   lifecycle, and storage core; only rollout discovery/sanitization is
   source-specific. Existing standalone Codex state is intentionally reused.
4. The SDK emits Codex session attribution only when the local Codex tap is
   paired; the backend records it as `codex_session_id` plus `run_sessions`.
5. Search retrieves both `claude_code` and `codex` transcript connectors, and
   transcript links preserve `?source=codex` through the viewer.
6. The canonical `npx probe-research` onboarding asks which installed agents to
   connect. Selecting Claude Code and Codex installs both plugin targets and one
   browser approval returns two independent, source-bound capture credentials.
   For Codex, the wizard then runs Codex's native MCP OAuth flow and verifies
   that `probe-research` reports `o_auth`; the token minted for Claude's headers
   helper is not treated as proof that Codex is logged in.
   Headless automation can express the same choice with
   `probe wizard --agent both --yes`.
7. The optional global-guidance capability writes one versioned, managed block
   to `$CODEX_HOME/AGENTS.md` (normally `~/.codex/AGENTS.md`). It tells Codex to
   search the Research OS knowledge base through the installed `probe-research`
   MCP before research design, then use the shared tracking skills. Existing
   user-authored instructions outside the managed markers are preserved.

## Gate 1: contracts and regression tests

Run the single non-deploying gate from `research-os-agent`:

```bash
RESEARCH_OS=/absolute/path/to/research-os make verify-codex-pre-release
```

It validates both plugin manifests through contract tests and the real Codex
installer, runs the agent/tap, backend, and dashboard suites, applies schema
parity checks, and installs both plugins with
the real Codex CLI under a temporary isolated `CODEX_HOME`. It does not publish,
deploy, tag, push, or touch the user's normal Codex configuration.

The equivalent individual checks are:

```bash
pytest -q tests/test_codex_plugins.py tests/test_verify_codex_live.py
pytest -q plugins/probe-research-tap/tests
```

From `research-os` (Docker is required by these integration tests):

```bash
pytest -q tests/integration/test_pairing_api.py tests/integration/test_claude_code_ingest_api.py
```

Do not deploy if source binding, plugin installation, or adapter tests
fail.

## Gate 2: clean Codex installation

Use an isolated Codex home or disposable macOS user, then:

```bash
codex plugin marketplace add /absolute/path/to/research-os-agent
codex plugin add probe-research@research-os-agent
codex plugin add probe-research-tap@research-os-agent
codex plugin list --json
```

Run `npx probe-research`, select Codex (and Claude Code if both should be
connected), then authorize the combined API, MCP, and capture request. The
wizard completes `codex mcp login probe-research` and verifies the resulting
host-owned OAuth state. Start a new Codex thread and use `/hooks` to review and
trust the Probe capture hook. Hook trust is mandatory for capture execution,
but is intentionally separate from plugin installation and MCP authentication;
Codex skips untrusted hooks rather than refusing the install.

Check `python3 -m tap status` and the per-session log under
`~/.codex/state/probe-research-tap/logs/`. Upgrades from the retired standalone
tap may continue using `~/.codex/state/prbe-codex-tap-plugin/` so an existing
pairing and outbox are not discarded. The device returned by
`GET /v1/devices` must report `source: codex`.

## Gate 3: full live canary

After the backend and both plugins are deployed, generate a marker such as
`probe-codex-canary-20260806-<random>`. In a fresh Codex thread, send a prompt
containing that exact marker, wait for a response, and end the session. Then run:

```bash
python scripts/verify_codex_live.py probe-codex-canary-20260806-<random>
```

The verifier uses `PROBE_MCP_TOKEN`, then `PROBE_TOKEN`, then the configured
MCP/read token. Pass `--token` to check a different team explicitly. The read
credential must resolve to the same team where the Codex capture device was
paired; otherwise the tenant-safe transcript/search APIs correctly return no
match.

The command passes only when `/v1/search` returns the marker from a document
whose `source_system` is exactly `codex`. This proves the real hook, rollout
parser, durable sender, source-bound gateway, engine connector, and searchable
index—not merely that an endpoint returned 202.

The verifier scans only captured source content. A generated relevance
explanation that repeats the query cannot make the canary pass.

If the canary fails, inspect in order: `/hooks` trust, local tap status/log,
`GET /v1/devices`, gateway 401/403/5xx logs, engine queue/DLQ, then search state.

## Release boundary and rollout order

Stop before release unless all three gates pass. When release is authorized,
apply in this order so no client can request a contract the server lacks:

1. Migrate the control database through
   `011_device_authorization_capture_source.sql` and the experiment database
   through `0049_ingest_token_authorization_sources.sql`.
2. Deploy the Research OS API/dashboard and verify health plus Claude
   regression checks.
3. Publish the marketplace snapshot and plugin versions.
4. Start a new Codex session, approve hook trust, and run the live canary.
5. Only after a passing canary, announce Codex support and begin the separate
   standalone-tap deprecation window.

Rollback is source-safe: remove the Codex marketplace entries and roll back the
API/dashboard deployment while leaving migrations 011 and 0049 in place. Their
defaults/backfill preserve the historical Claude-only shape, so old CLIs remain
compatible; reversing either migration during an incident adds risk and is
unnecessary.
