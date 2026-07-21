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

Add the marketplace, then install the plugin:

```
/plugin marketplace add prbe-ai/research-os-agent
/plugin install probe-research-tap@research-os-agent
```

(the CLI equivalents are `claude plugin marketplace add prbe-ai/research-os-agent`
and `claude plugin install probe-research-tap@research-os-agent`.)

## Pairing (setup)

Pair this device with your Research OS workspace:

1. In the dashboard, open **Integrations → Pair Claude Code**
   (<https://research.prbe.ai/integrations>) and copy the pairing token.
2. Run:

   ```bash
   python3 -m tap pair <token>
   ```

`tap pair` exchanges the pairing token for a device token, writes it to
`~/.claude/plugins/probe-research-tap/.token` (mode 0600), and pins the backend
host — read from the token's `iss` claim, so the daemon reaches the same backend
with no hardcoded host. Re-running `pair` on an already-paired device rotates the
token and retires the old device server-side. To unpair:

```bash
python3 -m tap revoke
```

which revokes the device server-side and wipes the local token, meta, and any
queued outbox (offline-safe — local state is cleared even if the server is
unreachable).

### Advanced / self-host

If you run the probe CLI, its credentials work without pairing:

```bash
probe login
```

writes `base_url` and the ingest token (`ingest_token`) to
`$XDG_CONFIG_HOME/probe/config.json` (default `~/.config/probe/config.json`;
`PROBE_CONFIG_PATH` overrides the file path for tests/dev). You can also set
`PROBE_INGEST_TOKEN` / `PROBE_BASE_URL` directly.

Resolution, highest precedence first:

- **token:** `.token` (from `tap pair`) → `PROBE_INGEST_TOKEN` →
  `ingest_token` in the probe CLI config
- **base URL:** `PROBE_BASE_URL` → host pinned by `tap pair` → `base_url` in
  the probe CLI config

No token → the hooks no-op (nothing to authenticate with). No base URL → the
daemon refuses to start rather than guess a host; there is deliberately no
hardcoded default.

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
raw}]}`. `device_id` comes from the pairing exchange (or is minted locally as
a uuid4 on first daemon start when self-hosting without pairing) and persisted
in meta; the backend passes it through to the engine as the device external id.
Session completion is handled backend-side — the plugin sends no finalize
message.

## State files

State lives at `~/.claude/plugins/probe-research-tap/` (override via
`PROBE_RESEARCH_TAP_PLUGIN_DIR`) — separate from the plugin code, which CC
manages under its plugin cache.

| File | Purpose |
|------|---------|
| `.token` | Device token written by `tap pair` (mode 0600). Absent for self-host users who auth via the probe CLI / env. |
| `.config` | JSON: cadence overrides (see below) + the backend host pinned at pair time. |
| `.disabled` | Presence disables the daemon entirely. |
| `.disabled_paths` | Newline-separated cwd prefixes to skip. |
| `state.db` | sqlite: file_offsets, outbox, meta (device_id, customer_id, paired_at, …). |
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
python3 -m tap pair <token>   # exchange a dashboard pairing token for a device token
python3 -m tap watch          # daemon (called by SessionStart hook)
python3 -m tap status         # print local state
python3 -m tap revoke         # revoke device server-side + wipe local state
```

## Development

```bash
cd plugins/probe-research-tap
python3 -m pytest tests/ -v
# or, with uv:
uv run --with pytest python -m pytest tests/ -v
```
