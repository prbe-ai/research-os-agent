# probe-research-tap

A single Claude Code and Codex plugin that ships sanitized per-session
transcripts to Research OS. The two agents share pairing, consent state,
durable outbox, retries, HTTP transport, lifecycle management, storage, status,
and revocation. Only transcript discovery and event normalization are
agent-specific.

Identity is injected server-side from a source-bound device token. The plugin
never sends employee fields, and the gateway validates tenant and source before
forwarding the client-sanitized batch. Runtime code is Python 3.11+ stdlib only.

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
  -> tail transcript/rollout JSONL from the supplied transcript_path
  -> normalize the agent event shape and remove unsupported payloads
  -> enqueue a durable batch in sqlite
  -> POST /ingest/v1/sessions/{claude-code|codex}
       2xx                 mark delivered
       401                 halt and clear the outbox
       400/403/404         drop rejected/poison batch and continue
       retryable failure   exponential-backoff retry
agent SessionEnd hook
  -> signal the daemon and leave a shutdown sentinel
  -> daemon tails and durably enqueues the final transcript bytes before exit
```

Codex can supply a null `transcript_path` at session start, so the adapter can
also discover the date-partitioned rollout by session id. The Codex transcript
format is not a stable public interface; `tap/codex_sanitize.py` and its real
rollout fixture are therefore a deliberately thin, separately tested adapter.
Claude Code normalization lives in `tap/sanitize.py`.

The batch body is `{device_id, session_id, batch_seq, cwd, events:[{line_no,
raw}]}`. The backend supplies tenant/user identity and forwards the normalized
batch to the matching engine connector. Session completion is backend-owned;
the plugin sends no finalize message.

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
| `state.db` | File offsets, durable outbox, device metadata, batch sequence |
| `logs/<session_id>.log` | Session daemon log |

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
