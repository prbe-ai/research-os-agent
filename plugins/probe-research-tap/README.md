# probe-research-tap

A single Claude Code and Codex plugin that ships sanitized per-session
transcripts to Research OS. The two agents share pairing, consent state,
durable outbox, retries, HTTP transport, lifecycle management, storage, status,
and revocation. Only transcript discovery and event normalization are
agent-specific.

Uploader identity is injected server-side from a source-bound device token.
Authentication does not prove the original author of a historical conversation.
The gateway validates tenant and source before forwarding the client-sanitized
batch. Runtime code is Python 3.11+ stdlib only.

## Install and authorize

The supported setup path offers Claude Code and Codex as checkboxes, installs
both selected plugin targets, and pairs them in one browser approval:

```bash
npx probe-research

# Headless equivalents:
npx probe-research wizard --agent claude --yes
npx probe-research wizard --agent codex --yes
npx probe-research wizard --agent both --yes
```

Manual marketplace installation is also available:

```bash
# Claude Code
claude plugin marketplace add prbe-ai/research-os-agent
claude plugin install probe-research-tap@research-os-agent

# Codex
codex plugin marketplace add prbe-ai/research-os-agent
codex plugin add probe-research-tap@research-os-agent
```

Codex requires a new session after installation. Open `/hooks`, review the
Probe hook definition, and trust it; Codex deliberately skips untrusted plugin
hooks. This approval does not block installation or MCP login: it gates hook
execution. Codex persists trust for the reviewed hook definition; a future
definition change may require review again.

The dashboard authorization creates a source-bound device: a Codex credential
cannot post to the Claude Code route, and a Claude Code credential cannot post
to the Codex route. Re-running setup rotates the device credential. Use
`probe capture off --uninstall` to revoke capture and remove the plugin.

## Data flow

```text
agent SessionStart hook
  -> detached, session-scoped tap daemon
  -> verify native session identity and negotiate protocol-2 receipts
  -> tail transcript/rollout JSONL from the supplied transcript_path
  -> normalize the agent event shape and remove unsupported payloads
  -> persist immutable batch bytes and source/event cursors in shared SQLite
  -> POST /ingest/v1/sessions/{claude-code|codex}
       matching receipt   advance the acknowledged cursor
       lost response      replay identical bytes and sequence
       auth/unavailable   retain pending bytes for retry
       conflict           retain evidence for reconciliation
agent SessionEnd hook
  -> signal the daemon and leave a shutdown sentinel
  -> pin a complete source prefix and send its finalization receipt
```

Codex can supply a null `transcript_path` at session start, so the adapter can
also discover the date-partitioned rollout by session id. The Codex transcript
format is not a stable public interface; `tap/codex_sanitize.py` and its real
rollout fixture are therefore a deliberately thin, separately tested adapter.
Claude Code normalization lives in `tap/sanitize.py`.

Protocol 2 adds stream identity, source byte/line bounds, emitted-event bounds,
prefix hashes and optional snapshot/finalization evidence to each batch. The
engine accepts a sequence once, returns the same receipt for an identical retry,
and rejects changed content at that sequence. Old producers cannot overwrite a
claimed protocol-2 stream. An unavailable protocol never triggers a downgrade.

After a daemon crash, another running local daemon can recover a quiet source
once its process ownership ends. Completion certifies that pinned prefix; a later
append reopens the stream with subsequent sequence numbers. Each source's saved
working directory is checked against current capture settings before staging
and delivery. This metadata never creates a session-to-project association.

## State and compatibility

State is separate from plugin code:

- Claude Code: `~/.claude/plugins/probe-research-tap/`
- Codex: `~/.codex/state/probe-research-tap/`
- Codex upgrade compatibility: an existing
  `~/.codex/state/prbe-codex-tap-plugin/` is reused until migrated, preserving
  its pairing and outbox.

Overrides are `PROBE_RESEARCH_TAP_PLUGIN_DIR` for Claude Code and the legacy
compatible `PRBE_CODEX_TAP_PLUGIN_DIR` for Codex.

| File | Purpose |
|---|---|
| `.token` | Mode-0600 source-bound device token |
| `.config` | Backend origin and optional cadence overrides |
| `.disabled` | Local all-session killswitch |
| `.disabled_paths` | Newline-separated cwd prefixes to skip |
| `state.db` | Legacy file offsets/outbox and device metadata, retained for compatibility |
| `logs/<session_id>.log` | Session daemon log |

Protocol-2 ownership, source snapshots and immutable pending batches live under
`~/.probe/transcripts-v2/`, scoped by backend, tenant and producer. The historical
importer uses this same journal. Set `PROBE_TRANSCRIPT_STATE_DIR` for isolated
tests. Keep this state when upgrading or rolling back; deleting it is not a
conflict-recovery procedure.

The daemon uses a 60-second active cadence and moves to 300 seconds after two
empty ticks. Configure `active_interval_seconds` and `idle_interval_seconds`
in `.config`, or set `sync_interval_seconds` to use one fixed interval.

Server-side ingestion status is polled every five minutes and fails open on a
status-check network error. Local killswitches are immediate:

```bash
touch ~/.codex/state/probe-research-tap/.disabled
echo "/Users/me/private-repo" >> ~/.codex/state/probe-research-tap/.disabled_paths
```

## Configuration

Common overrides:

| Variable | Purpose |
|---|---|
| `PROBE_BASE_URL` | Backend origin override |
| `PROBE_CONFIG_PATH` | Probe CLI config path override (tests/dev) |
| `PROBE_TRANSCRIPT_STATE_DIR` | Shared protocol-2 journal root (tests/dev) |
| `PROBE_RESEARCH_TAP_ACTIVE_INTERVAL_SECONDS` | Active interval |
| `PROBE_RESEARCH_TAP_IDLE_INTERVAL_SECONDS` | Idle interval |
| `PROBE_RESEARCH_TAP_INTERVAL_SECONDS` | Legacy fixed interval |
| `PROBE_RESEARCH_TAP_PLUGIN_DIR` | Claude Code state directory |
| `PRBE_CODEX_TAP_PLUGIN_DIR` | Codex state directory |
| `PROBE_INGEST_TOKEN` | Claude Code token override |
| `PRBE_CODEX_TAP_TOKEN` | Codex token override |

The source is set by the installed plugin hook. For direct development runs,
set `PROBE_TAP_SOURCE=codex` to exercise the Codex adapter; the default is
`claude_code`.

## Development

```bash
pytest -q plugins/probe-research-tap/tests
PROBE_TAP_SOURCE=codex pytest -q \
  plugins/probe-research-tap/tests/test_codex_sanitize.py \
  plugins/probe-research-tap/tests/test_codex_research_os_contract.py
```

The repository-level pre-release gate also validates and installs this plugin
through the real Codex CLI in an isolated `CODEX_HOME`.
