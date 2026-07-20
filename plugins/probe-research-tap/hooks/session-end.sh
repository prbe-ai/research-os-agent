#!/usr/bin/env bash
# SessionEnd hook — terminate the tap daemon for this session.
#
# Touches the shutdown sentinel before SIGTERMing the wrapper so the
# crash-recovery loop exits instead of respawning the daemon one more time.

set -euo pipefail

HOOK_INPUT="$(cat)"
SESSION_ID=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

PID_FILE="/tmp/probe-research-tap-watcher-${SESSION_ID}.pid"
SHUTDOWN_FILE="/tmp/probe-research-tap-watcher-${SESSION_ID}.shutdown"

touch "$SHUTDOWN_FILE"

if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
    if [ -n "$PID" ]; then
        # Kill the wrapper's process group so the Python child dies too.
        kill -TERM "-$PID" 2>/dev/null || kill -TERM "$PID" 2>/dev/null || true
    fi
    rm -f "$PID_FILE"
fi

rm -f "$SHUTDOWN_FILE"

exit 0
