# probe-research-tap

A Claude Code plugin that ships sanitized per-session Claude Code
transcripts to the Research OS backend's `/ingest/v1/sessions/claude-code`
endpoint. Identity is injected server-side from the ingest token (the
plugin never sends employee fields), and the backend applies its own
secret gate + per-session quarantine on top of the client-side sanitizer.
Runs as a session-scoped daemon spawned by CC's `SessionStart` hook and
torn down on `SessionEnd`.

Zero runtime dependencies (stdlib only); Python 3.11+.

## Install

This plugin is published through the `research-os-agent` marketplace:

```
claude plugin install probe-research-tap@research-os-agent
```

(or from inside Claude Code: `/plugin install probe-research-tap@research-os-agent`)

## Auth

There is no pairing step. The plugin reuses the probe CLI's credentials:

```bash
probe login
```

which writes `base_url` and the ingest token (`ingest_token`) to
`~/.config/probe/config.json`. The daemon resolves, in order:

- `PROBE_INGEST_TOKEN` / `PROBE_BASE_URL` environment variables
- `ingest_token` / `base_url` in `$XDG_CONFIG_HOME/probe/config.json`
  (default `~/.config/probe/config.json`; `PROBE_CONFIG_PATH` overrides
  the file path for tests/dev)

No ingest token → the hooks no-op (nothing to authenticate with). No
`base_url` → the daemon refuses to start rather than guess a host; there
is deliberately no hardcoded default.

## How it works

```
┌─ Claude Code session ───────────────────────────────────────────────┐
│                                                                      │
│  SessionStart hook ──► spawns tap daemon (detached, crash-loop)      │
│                              │                                       │
│                              ▼                                       │
│                       every tick (adaptive, see Cadence):            │
│                       1. tail transcript JSONL (byte-offset cursor)  │
│                       2. validate each new line as JSON              │
│                       3. sanitize (strip API metadata, tool bodies)  │
│                       4. build batch body, enqueue to sqlite outbox  │
│                       5. drain outbox:                               │
│                          POST /ingest/v1/sessions/claude-code        │
│                          - 2xx → mark success                        │
│                          - 401 → halt + clear outbox (bad token)     │
│                          - 400/403/404 (poison) → drop the batch;    │
│                            403 = session QUARANTINED server-side,    │
│                            daemon keeps running                      │
│                          - else → exponential backoff retry          │
│                                                                      │
│  SessionEnd hook ──► SIGTERMs daemon, cleans up sentinel             │
└──────────────────────────────────────────────────────────────────────┘
```

The sanitizer (`tap/sanitize.py`) ships the *conversation* — user prompts,
assistant text + thinking, plus a one-line marker per tool call — and
strips Anthropic API metadata, CC bookkeeping events, full tool inputs,
and full tool results before anything leaves the machine.

The batch body is `{device_id, session_id, batch_seq, cwd, events:[{line_no,
raw}]}`. `device_id` is minted locally (uuid4) on first daemon start and
persisted; the backend passes it through to the engine as the device
external id. Session completion is handled backend-side — the plugin sends
no finalize message.

## State files

State lives at `~/.claude/plugins/probe-research-tap/` (override via
`PROBE_RESEARCH_TAP_PLUGIN_DIR`) — separate from the plugin code, which CC
manages under its plugin cache. Credentials do NOT live here — they belong
to the probe CLI's config file.

| File | Purpose |
|------|---------|
| `.config` | JSON for cadence overrides — see below. |
| `.disabled` | Presence disables the daemon entirely. |
| `.disabled_paths` | Newline-separated cwd prefixes to skip. |
| `state.db` | sqlite: file_offsets, outbox, meta (incl. device_id). |
| `logs/<session_id>.log` | Per-session log file. |

## Cadence

The daemon is adaptive by default:

- **Active mode (60s)** while the transcript is advancing
- **Idle mode (300s)** after two consecutive empty ticks (≈2 min of no
  new transcript content)

Active resumes the moment new lines appear. This keeps ingestion near
real-time during work without flooding the backend on idle CC sessions.

Override either side via `.config`:

```bash
# Tighter active cadence; same idle.
echo '{"active_interval_seconds": 30}' \
  > ~/.claude/plugins/probe-research-tap/.config

# Both knobs.
echo '{"active_interval_seconds": 30, "idle_interval_seconds": 600}' \
  > ~/.claude/plugins/probe-research-tap/.config
```

Or disable adaptive switching entirely with the legacy single knob — sets
both active and idle to the same value:

```bash
echo '{"sync_interval_seconds": 60}' \
  > ~/.claude/plugins/probe-research-tap/.config
```

## Killswitches

Server-side: the daemon polls `GET /ingest/v1/sessions/status`
(`{"ingest_enabled": bool, "reason": str|null}`) with a 5-minute cache and
skips whole ticks while ingestion is paused. Poll failures fail OPEN — the
killswitch is for graceful pause, not fail-secure.

Local — disable for one specific repo:

```bash
echo "/Users/me/private-repo" >> ~/.claude/plugins/probe-research-tap/.disabled_paths
```

Disable entirely:

```bash
touch ~/.claude/plugins/probe-research-tap/.disabled
```

## Environment variables

| Variable | Purpose |
|----------|---------|
| `PROBE_INGEST_TOKEN` | Override the ingest token from the probe CLI config file. |
| `PROBE_BASE_URL` | Override the backend base URL. Default: `base_url` from the probe CLI config file — no hardcoded fallback. |
| `PROBE_CONFIG_PATH` | Override the probe CLI config file path (tests/dev). |
| `PROBE_RESEARCH_TAP_ACTIVE_INTERVAL_SECONDS` | Override active interval. |
| `PROBE_RESEARCH_TAP_IDLE_INTERVAL_SECONDS` | Override idle interval. |
| `PROBE_RESEARCH_TAP_INTERVAL_SECONDS` | Legacy single-knob — applies to both. |
| `PROBE_RESEARCH_TAP_PLUGIN_DIR` | Override state directory (for tests). |

## Subcommands

```bash
python -m tap watch    # daemon (called by SessionStart hook)
python -m tap status   # print local state
```

## Development

```bash
cd plugins/probe-research-tap
python3 -m pytest tests/ -v
# or, with uv:
uv run --with pytest python -m pytest tests/ -v
```
