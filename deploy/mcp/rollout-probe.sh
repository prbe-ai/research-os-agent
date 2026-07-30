#!/usr/bin/env bash
# Rollout probe: measure — don't sample — MCP availability across a deploy.
#
# Hits /healthz once per second and (with PROBE_MCP_TOKEN set) fires an
# authenticated search_knowledge tool call every 5 seconds, logging one line
# per probe with the HTTP status. Run it BEFORE merging (baseline) and across
# the deploy rollout window; a hardened rollout shows zero non-2xx lines.
#
# NOTE (2026-07-30 plan, D10): the deploy that SHIPS the rollout hardening is
# still exposed — its outgoing pods carry the old spec. Judge the fix on the
# NEXT deploy's window, not the shipping one.
#
# Usage:
#   PROBE_MCP_TOKEN=probe_pat_... ./rollout-probe.sh [host] [outfile]
# Summarize:
#   grep -c ' healthz=200' out.log ; grep -v '=20[0-9]' out.log
set -u

HOST="${1:-mcp.research.prbe.ai}"
OUT="${2:-rollout-probe-$(date +%Y%m%d-%H%M%S).log}"
TOKEN="${PROBE_MCP_TOKEN:-}"

TOOL_BODY='{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"rollout probe","top_k":1}}}'

echo "probing https://$HOST -> $OUT (tool probe: $([ -n "$TOKEN" ] && echo on || echo off — set PROBE_MCP_TOKEN)); ctrl-c to stop" >&2

tool_probe() {
  # Bounded above the 20s queue-shed + backend budget; a shed still returns
  # HTTP 200 with a tool error, which is availability — only 5xx/ERR is not.
  curl -s -o /dev/null -w '%{http_code}' --max-time 60 \
    -X POST "https://$HOST/mcp" \
    -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$TOOL_BODY" 2>/dev/null || echo ERR
}

i=0
while true; do
  ts=$(date -u +%FT%TZ)
  h=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "https://$HOST/healthz" 2>/dev/null || echo ERR)
  line="$ts healthz=$h"
  if [ -n "$TOKEN" ] && [ $((i % 5)) -eq 0 ]; then
    # Backgrounded so a slow tool call never blocks the 1/s healthz cadence.
    ( t=$(tool_probe); echo "$(date -u +%FT%TZ) tool=$t" >> "$OUT" ) &
  fi
  echo "$line" >> "$OUT"
  i=$((i + 1))
  sleep 1
done
