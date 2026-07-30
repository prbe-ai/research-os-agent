#!/usr/bin/env bash
# Rollout probe: measure — don't sample — MCP availability across a deploy.
#
# Hits /healthz once per second and (with PROBE_MCP_TOKEN set) fires an
# authenticated search_knowledge tool call every TOOL_INTERVAL seconds,
# logging one line per probe. A tool probe records BOTH the HTTP status and
# whether the tool result carried isError — an MCP failure rides inside an
# HTTP 200, so status alone would declare a broken rollout successful.
# Only one tool probe is outstanding at a time (a slow call must not stack
# twelve concurrent searches onto a struggling backend).
#
# NOTE (2026-07-30 plan, D10): the deploy that SHIPS the rollout hardening is
# still exposed — its outgoing pods carry the old spec. Judge the fix on the
# NEXT deploy's window, not the shipping one.
#
# Usage:
#   PROBE_MCP_TOKEN=probe_pat_... ./rollout-probe.sh [host] [outfile]
# Summarize:
#   grep -c ' healthz=200' out.log ; grep -vE '=(200|ok)' out.log
#
# Failure sentinels: transport failure logs status 000 (curl's own marker);
# a 2xx with isError=true logs tool=200/err.
set -u

HOST="${1:-mcp.research.prbe.ai}"
OUT="${2:-rollout-probe-$(date +%Y%m%d-%H%M%S).log}"
TOKEN="${PROBE_MCP_TOKEN:-}"
TOOL_INTERVAL=5   # seconds between tool probes (each waits for the last)

TOOL_BODY='{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"search_knowledge","arguments":{"query":"rollout probe","top_k":1}}}'

# Keep the bearer off argv (ps-visible on shared hosts): curl reads the
# header from a 0600 config file that is removed on exit.
CURLCFG=""
TOOL_PID=""
cleanup() {
  [ -n "$TOOL_PID" ] && kill "$TOOL_PID" 2>/dev/null
  [ -n "$CURLCFG" ] && rm -f "$CURLCFG"
}
trap cleanup EXIT INT TERM

if [ -n "$TOKEN" ]; then
  CURLCFG=$(mktemp)
  chmod 600 "$CURLCFG"
  printf 'header = "Authorization: Bearer %s"\n' "$TOKEN" > "$CURLCFG"
fi

echo "probing https://$HOST -> $OUT (tool probe: $([ -n "$TOKEN" ] && echo on || echo off — set PROBE_MCP_TOKEN)); ctrl-c to stop" >&2

tool_probe() {
  # --max-time must stay above the server's 20s queue-shed budget plus one
  # 30s backend attempt; 60s covers both without stacking probes (the caller
  # waits for this one before launching the next).
  local body status
  body=$(mktemp)
  status=$(curl -s -o "$body" -w '%{http_code}' --max-time 60 \
    -X POST "https://$HOST/mcp" \
    --config "$CURLCFG" \
    -H 'Content-Type: application/json' \
    -H 'Accept: application/json, text/event-stream' \
    -d "$TOOL_BODY" 2>/dev/null) || status="000"
  local verdict="ok"
  if [ "$status" != "200" ]; then
    verdict="http"
  elif grep -q '"isError" *: *true' "$body"; then
    verdict="err"
  fi
  rm -f "$body"
  echo "$(date -u +%FT%TZ) tool=$status/$verdict" >> "$OUT"
}

i=0
while true; do
  h=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "https://$HOST/healthz" 2>/dev/null) || h="000"
  echo "$(date -u +%FT%TZ) healthz=$h" >> "$OUT"
  if [ -n "$TOKEN" ] && [ $((i % TOOL_INTERVAL)) -eq 0 ]; then
    # One outstanding tool probe at most: skip this tick if the last one is
    # still in flight rather than stacking load onto a struggling backend.
    if [ -z "$TOOL_PID" ] || ! kill -0 "$TOOL_PID" 2>/dev/null; then
      tool_probe &
      TOOL_PID=$!
    fi
  fi
  i=$((i + 1))
  sleep 1
done
