#!/usr/bin/env bash
# Probe Research SessionStart hook: nudge when the installed CLI/plugin is out
# of date. SYNCHRONOUS and FAIL-OPEN by contract — the version check must finish
# before this returns its JSON (a SessionStart systemMessage can't come from a
# detached process), and ANY failure degrades to `{"continue": true}` so a
# broken check never blocks a session. Network is throttled by a cache file to
# once per version_policy.TTL (15m; it was 24h once and that bought a whole day
# of blindness across a burst of releases), so most session starts do zero
# network yet the nudge still renders every session from the cached manifest.
set -u

# Claude Code sends {session_id, transcript_path, cwd, source} on stdin. The
# telemetry observer wants session_id and source; capture it (draining the pipe
# either way so nothing blocks on a full one).
HOOK_INPUT="$(cat 2>/dev/null || true)"

PLUGIN_ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}}"

# python3 required (same dependency the tap hook and mcp helper already assume);
# degrade silently if absent rather than erroring.
PY="$(command -v python3 2>/dev/null || true)"
if [ -z "$PY" ]; then
  printf '{"continue": true}\n'
  exit 0
fi

# SessionStart's stdin carries `source` (startup|resume|clear|compact) and
# `session_id`. version_check.py turns source=compact into a reconcile-Probe
# nudge on the additionalContext channel -- the one surface that reaches the
# model right after a compaction -- and needs the session id alongside it: a
# session the researcher untracked (`probe session untrack`) gets the off
# contract restated there instead of a nudge to do Probe work. The initial cwd
# seeds the folder-aware tracking default. All three fields come from ONE JSON
# parse and travel over NUL-delimited records: unlike tabs/newlines, NUL cannot
# occur in a filesystem path, and no shell text is ever evaluated. Skipped
# under PreCompact (its stdin has no `source` and the event can carry no context
# anyway). Fail-open: unparseable payload = empty fields, and an absent id reads
# as tracking on -- today's behaviour.
PROBE_SESSION_SOURCE=""
PROBE_SESSION_ID=""
PROBE_SESSION_CWD=""
if [ "${PROBE_HOOK_EVENT:-}" != "precompact" ]; then
  exec 3< <(printf '%s' "$HOOK_INPUT" | "$PY" -c 'import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
def field(value):
    return value if isinstance(value, str) else ""
fields = (d.get("source"), d.get("session_id"), d.get("cwd")) if isinstance(d, dict) else ("", "", "")
for value in fields:
    sys.stdout.buffer.write(field(value).encode("utf-8", "surrogatepass") + b"\0")' 2>/dev/null)
  IFS= read -r -d '' PROBE_SESSION_SOURCE <&3 || PROBE_SESSION_SOURCE=""
  IFS= read -r -d '' PROBE_SESSION_ID <&3 || PROBE_SESSION_ID=""
  IFS= read -r -d '' PROBE_SESSION_CWD <&3 || PROBE_SESSION_CWD=""
  exec 3<&-
fi
export PROBE_SESSION_SOURCE PROBE_SESSION_ID PROBE_SESSION_CWD

# Resolve the `probe` binary without trusting PATH — a dock-launched Claude Code
# sources no profile, so ~/.local/bin may be absent. Mirrors bin/probe-mcp-headers.
PROBE_BIN="$(command -v probe 2>/dev/null || true)"
for _c in \
  "$HOME/.local/bin/probe" \
  "$HOME/.local/share/uv/tools/probe-research/bin/probe"
do
  [ -n "$PROBE_BIN" ] && break
  [ -x "$_c" ] && PROBE_BIN="$_c"
done

export PROBE_BIN="${PROBE_BIN:-probe}"
if [ "${PROBE_AGENT:-}" = "codex" ]; then
  export PROBE_PLUGIN_JSON="$PLUGIN_ROOT/.codex-plugin/plugin.json"
else
  export PROBE_PLUGIN_JSON="$PLUGIN_ROOT/.claude-plugin/plugin.json"
fi
# PROBE_BASE_URL (self-host) is honored by version_check.py if exported; otherwise
# it reads the CLI config, then falls back to the hosted API.

# Funnel telemetry (observability only, PROBE_TELEMETRY=off to disable). The
# observer parses stdin and hands the event to a detached sender, so this adds
# one interpreter start and no network to the hook path. After the exports
# above on purpose: the detached sender inherits PROBE_BIN/PROBE_PLUGIN_JSON.
printf '%s' "$HOOK_INPUT" | "$PY" "$PLUGIN_ROOT/hooks/telemetry.py" session-start 2>/dev/null || true

out="$("$PY" "$PLUGIN_ROOT/hooks/version_check.py" 2>/dev/null)" || out=""
if [ -n "$out" ]; then
  printf '%s\n' "$out"
else
  printf '{"continue": true}\n'
fi
exit 0
