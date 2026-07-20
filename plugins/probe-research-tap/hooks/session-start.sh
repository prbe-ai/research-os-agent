#!/usr/bin/env bash
# SessionStart hook for probe-research-tap.
#
# Reads {session_id, transcript_path, cwd} from stdin and spawns the tap
# daemon detached, wrapped in a crash-recovery loop. Wrapper PID is recorded
# in /tmp/probe-research-tap-watcher-<sid>.pid for SessionEnd cleanup.

set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
PLUGIN_DIR="${PROBE_RESEARCH_TAP_PLUGIN_DIR:-$HOME/.claude/plugins/probe-research-tap}"
LOG_DIR="$PLUGIN_DIR/logs"
mkdir -p "$LOG_DIR"

HOOK_INPUT="$(cat)"
SESSION_ID=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null || echo "")
TRANSCRIPT_PATH=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("transcript_path",""))' 2>/dev/null || echo "")
CWD=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys,os; print(json.load(sys.stdin).get("cwd") or os.getcwd())' 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ] || [ -z "$TRANSCRIPT_PATH" ]; then
    printf '{"continue": true}\n'
    exit 0
fi

LOG_FILE="${LOG_DIR}/${SESSION_ID}.log"

# --- Version self-heal (runs on the first session after an install/update) ---
# CC's marketplace owns install and the versioned cache/ path, but the state
# dir (PLUGIN_DIR) persists across versions and can carry stale artifacts into
# a new one. We never touch live state (.config, state.db, logs), and never
# prune CC's cache (an older version may still back a concurrent session).
RUNNING_VER=""
if [ -f "$PLUGIN_ROOT/.claude-plugin/plugin.json" ]; then
    RUNNING_VER=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version",""))' \
        "$PLUGIN_ROOT/.claude-plugin/plugin.json" 2>/dev/null || echo "")
fi

# One-time cleanup of a superseded in-place install (plugin *code* living in
# the state dir). `.orphaned_at` is written by Claude Code when it supersedes
# an in-place plugin dir, so its presence proves this is a stale leftover and
# NOT a developer checkout pointed at via PROBE_RESEARCH_TAP_PLUGIN_DIR (which
# carries no such marker). PLUGIN_DIR != PLUGIN_ROOT means CC runs our code
# from the cache, so the code files here are dead weight.
if [ -f "$PLUGIN_DIR/.orphaned_at" ] && [ "$PLUGIN_DIR" != "$PLUGIN_ROOT" ]; then
    echo "[$(date -u +%FT%TZ)] removing pre-marketplace install leftovers from $PLUGIN_DIR" >>"$LOG_FILE"
    for _stale in .git .gitattributes .gitignore .claude-plugin tap hooks tests \
                  scripts README.md pyproject.toml uv.lock .orphaned_at; do
        rm -rf "${PLUGIN_DIR:?}/${_stale}" 2>/dev/null || true
    done
fi

# Stamp the running version; log the transition the first time it changes so an
# update is visible in the session log (and gives a hook for future migrations).
if [ -n "$RUNNING_VER" ]; then
    PREV_VER=$(cat "$PLUGIN_DIR/.installed_version" 2>/dev/null || echo "")
    if [ "$RUNNING_VER" != "$PREV_VER" ]; then
        echo "[$(date -u +%FT%TZ)] probe-research-tap version ${PREV_VER:-none} -> $RUNNING_VER" >>"$LOG_FILE"
        printf '%s' "$RUNNING_VER" >"$PLUGIN_DIR/.installed_version" 2>/dev/null || true
    fi
fi

# Killswitch: presence of .disabled disables the daemon entirely.
if [ -f "$PLUGIN_DIR/.disabled" ]; then
    echo "[$(date -u +%FT%TZ)] killswitch active, skipping" >>"$LOG_FILE"
    printf '{"continue": true}\n'
    exit 0
fi

# Without an ingest token there's nothing to authenticate with. The token
# comes from the PROBE_INGEST_TOKEN env or the probe CLI's config file
# ($XDG_CONFIG_HOME/probe/config.json, default ~/.config/probe/config.json,
# written by `probe login`; PROBE_CONFIG_PATH overrides for tests/dev).
# Surface once and no-op — mirrors tap/config.py's load_token().
if [ -z "${PROBE_INGEST_TOKEN:-}" ]; then
    HAS_TOKEN=$(python3 - <<'PYEOF' 2>/dev/null || echo ""
import json
import os
from pathlib import Path

p = os.environ.get("PROBE_CONFIG_PATH")
if p:
    path = Path(p)
else:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    path = root / "probe" / "config.json"
try:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        data = {}
    # The probe CLI writes v2 (named contexts) as of the workspace-context pass; a
    # file it has not re-saved yet is still flat v1. This gate decides whether the
    # daemon starts AT ALL, so reading only v1 would silently disable transcript
    # capture on upgrade — and tap/config.py's own v2 support would never be reached.
    contexts = data.get("contexts")
    if isinstance(contexts, dict):
        active = contexts.get(data.get("current_context") or "default")
        data = active if isinstance(active, dict) else {}
    tok = data.get("ingest_token")
except Exception:
    tok = None
print("yes" if isinstance(tok, str) and tok.strip() else "")
PYEOF
)
    if [ -z "$HAS_TOKEN" ]; then
        echo "[$(date -u +%FT%TZ)] probe-research-tap: no ingest token configured; skipping" >>"$LOG_FILE"
        printf '{"continue": true}\n'
        exit 0
    fi
fi

PID_FILE="/tmp/probe-research-tap-watcher-${SESSION_ID}.pid"

# If a daemon is already running for this session_id (e.g. resumed session),
# don't spawn another.
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    printf '{"continue": true}\n'
    exit 0
fi

# No live wrapper for this session_id, so any leftover shutdown sentinel is
# stale (SessionEnd no longer deletes it — see session-end.sh). Clear it before
# spawning, or the wrapper's first `[ -f "$SHUTDOWN" ] && exit 0` check would
# immediately kill the fresh daemon on a resumed session.
SHUTDOWN_FILE="/tmp/probe-research-tap-watcher-${SESSION_ID}.shutdown"
rm -f "$SHUTDOWN_FILE"

# Resolve Python interpreter — prefer plugin-local venv.
PY="$PLUGIN_ROOT/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "[$(date -u +%FT%TZ)] no python3 found, daemon disabled" >>"$LOG_FILE"
    printf '{"continue": true}\n'
    exit 0
fi

# Crash-recovery wrapper: respawn up to 5 times per minute.
# Self-terminates when shutdown sentinel exists (SessionEnd touches it).
#
# Why a SIGTERM trap that forwards to the python child: macOS doesn't ship
# `setsid` so we can't put the wrapper + daemon in their own process group
# and rely on `kill -TERM -<pgid>` to take down both at once. Instead we
# detach via `nohup ... & disown` (POSIX-portable) and have the wrapper
# bash forward SIGTERM/SIGINT explicitly to the python child it spawns.
WRAPPER_SCRIPT='
SID="$1"; TP="$2"; CWD="$3"; PY="$4"; ROOT="$5"; LOG="$6"
SHUTDOWN="/tmp/probe-research-tap-watcher-${SID}.shutdown"
RESTART_COUNT=0
WINDOW_START=$(date +%s)
CHILD_PID=""
trap '\''[ -n "$CHILD_PID" ] && kill -TERM "$CHILD_PID" 2>/dev/null; exit 0'\'' TERM INT
while true; do
    [ -f "$SHUTDOWN" ] && exit 0
    NOW=$(date +%s)
    if [ $((NOW - WINDOW_START)) -ge 60 ]; then
        WINDOW_START=$NOW
        RESTART_COUNT=0
    fi
    if [ "$RESTART_COUNT" -ge 5 ]; then
        echo "[$(date -u +%FT%TZ)] tap: too many restarts in 1min, giving up" >>"$LOG"
        exit 1
    fi
    "$PY" -m tap watch --session-id "$SID" --transcript "$TP" --cwd "$CWD" --plugin-root "$ROOT" >>"$LOG" 2>&1 &
    CHILD_PID=$!
    wait "$CHILD_PID" 2>/dev/null || true
    CHILD_PID=""
    [ -f "$SHUTDOWN" ] && exit 0
    RESTART_COUNT=$((RESTART_COUNT + 1))
    sleep 5
done
'

# Detach the wrapper. nohup ignores SIGHUP so it survives CC's exit; `&`
# backgrounds it; `disown` removes it from this shell's job table so the
# parent (this hook) can exit cleanly without reaping it. On Linux this is
# equivalent to setsid (just without the new process group); on macOS it's
# the only portable option since setsid isn't installed by default.
PYTHONPATH="$PLUGIN_ROOT" \
    nohup /bin/bash -c "$WRAPPER_SCRIPT" wrapper \
    "$SESSION_ID" "$TRANSCRIPT_PATH" "$CWD" "$PY" "$PLUGIN_ROOT" "$LOG_FILE" \
    </dev/null >>"$LOG_FILE" 2>&1 &
WRAPPER_PID=$!
disown
echo "$WRAPPER_PID" >"$PID_FILE"

printf '{"continue": true}\n'
