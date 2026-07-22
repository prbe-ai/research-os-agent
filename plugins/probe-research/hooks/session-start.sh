#!/usr/bin/env bash
# Probe Research SessionStart hook: nudge when the installed CLI/plugin is out
# of date. SYNCHRONOUS and FAIL-OPEN by contract — the version check must finish
# before this returns its JSON (a SessionStart systemMessage can't come from a
# detached process), and ANY failure degrades to `{"continue": true}` so a
# broken check never blocks a session. Network is throttled to once/24h by a
# cache file (version_check.py), so most session starts do zero network yet the
# nudge still renders every session from the cached manifest.
set -u

# Claude Code sends {session_id, transcript_path, cwd, source} on stdin. We do
# not need it; drain it so nothing blocks on a full pipe.
cat >/dev/null 2>&1 || true

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

# python3 required (same dependency the tap hook and mcp helper already assume);
# degrade silently if absent rather than erroring.
PY="$(command -v python3 2>/dev/null || true)"
if [ -z "$PY" ]; then
  printf '{"continue": true}\n'
  exit 0
fi

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
export PROBE_PLUGIN_JSON="$PLUGIN_ROOT/.claude-plugin/plugin.json"
# PROBE_BASE_URL (self-host) is honored by version_check.py if exported; otherwise
# it reads the CLI config, then falls back to the hosted API.

out="$("$PY" "$PLUGIN_ROOT/hooks/version_check.py" 2>/dev/null)" || out=""
if [ -n "$out" ]; then
  printf '%s\n' "$out"
else
  printf '{"continue": true}\n'
fi
exit 0
