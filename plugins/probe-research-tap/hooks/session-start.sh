#!/usr/bin/env bash
# SessionStart hook for probe-research-tap.
#
# Reads {session_id, transcript_path, cwd} from stdin and spawns the tap
# daemon detached, wrapped in a crash-recovery loop. Wrapper PID is recorded
# in /tmp/probe-research-tap-watcher-<sid>.pid for SessionEnd cleanup.

set -euo pipefail

PLUGIN_ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}}"
SOURCE="${PROBE_TAP_SOURCE:-claude_code}"
if [ "$SOURCE" = "codex" ]; then
    if [ -n "${PRBE_CODEX_TAP_PLUGIN_DIR:-}" ]; then
        PLUGIN_DIR="$PRBE_CODEX_TAP_PLUGIN_DIR"
    elif [ -d "$HOME/.codex/state/prbe-codex-tap-plugin" ] && [ ! -e "$HOME/.codex/state/probe-research-tap" ]; then
        # One-way compatibility: existing pairings/outboxes keep their path;
        # clean installs use the unified package name.
        PLUGIN_DIR="$HOME/.codex/state/prbe-codex-tap-plugin"
    else
        PLUGIN_DIR="$HOME/.codex/state/probe-research-tap"
    fi
    WATCHER_PREFIX="prbe-codex-tap"
else
    PLUGIN_DIR="${PROBE_RESEARCH_TAP_PLUGIN_DIR:-$HOME/.claude/plugins/probe-research-tap}"
    WATCHER_PREFIX="probe-research-tap"
fi
LOG_DIR="$PLUGIN_DIR/logs"
mkdir -p "$LOG_DIR"

# Prune leaked shutdown sentinels. session-end.sh deliberately never deletes
# one (it is the last-resort stop signal for an orphaned daemon), and only a
# later SessionStart *for the same session id* clears it — but session ids are
# UUIDs and never recur, so without this every session leaks a file into /tmp
# forever. Observed: 120 stale sentinels against 0 live daemons. 2 days is far
# beyond any live session, so this can never race a running wrapper.
#
# The trailing slash on /tmp/ is load-bearing on macOS: /tmp is a symlink to
# private/tmp and find defaults to -P (never follow symlinks), so `find /tmp`
# matches the symlink itself, descends into nothing, and exits 0 having done
# nothing at all — which the `|| true` would have hidden forever.
find /tmp/ -maxdepth 1 -name "${WATCHER_PREFIX}-watcher-*.shutdown" -mtime +2 -delete 2>/dev/null || true

HOOK_INPUT="$(cat)"
SESSION_ID=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null || echo "")
TRANSCRIPT_PATH=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys; v=json.load(sys.stdin).get("transcript_path"); print(v if isinstance(v,str) else "")' 2>/dev/null || echo "")
CWD=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys,os; print(json.load(sys.stdin).get("cwd") or os.getcwd())' 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ] || { [ "$SOURCE" != "codex" ] && [ -z "$TRANSCRIPT_PATH" ]; }; then
    printf '{"continue": true}\n'
    exit 0
fi

LOG_FILE="${LOG_DIR}/${SESSION_ID}.log"

# --- Version self-heal (runs on the first session after an install/update) ---
# CC's marketplace owns install and the versioned cache/ path, but the state
# dir (PLUGIN_DIR) persists across versions and can carry stale artifacts into
# a new one. We never touch live state (.token, .config, state.db, logs), and
# never prune CC's cache (an older version may still back a concurrent session).
RUNNING_VER=""
MANIFEST="$PLUGIN_ROOT/.claude-plugin/plugin.json"
[ "$SOURCE" = "codex" ] && MANIFEST="$PLUGIN_ROOT/.codex-plugin/plugin.json"
if [ -f "$MANIFEST" ]; then
    RUNNING_VER=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("version",""))' \
        "$MANIFEST" 2>/dev/null || echo "")
fi

# One-time cleanup of a superseded in-place install (plugin *code* living in
# the state dir). `.orphaned_at` is written by Claude Code when it supersedes
# an in-place plugin dir, so its presence proves this is a stale leftover and
# NOT a developer checkout pointed at via PROBE_RESEARCH_TAP_PLUGIN_DIR (which
# carries no such marker). PLUGIN_DIR != PLUGIN_ROOT means CC runs our code
# from the cache, so the code files here are dead weight.
if [ "$SOURCE" != "codex" ] && [ -f "$PLUGIN_DIR/.orphaned_at" ] && [ "$PLUGIN_DIR" != "$PLUGIN_ROOT" ]; then
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

# Without a token there's nothing to authenticate with. Resolution mirrors
# tap/config.py's load_token(): a paired device token ($PLUGIN_DIR/.token,
# written by `tap pair` — the primary path) OR the PROBE_INGEST_TOKEN env OR
# the probe CLI's config file ($XDG_CONFIG_HOME/probe/config.json, default
# ~/.config/probe/config.json, written by `probe login`; PROBE_CONFIG_PATH
# overrides for tests/dev). Surface once and no-op when none is present.
TOKEN_ENV="${PROBE_INGEST_TOKEN:-}"
[ "$SOURCE" = "codex" ] && TOKEN_ENV="${PRBE_CODEX_TAP_TOKEN:-}"
if [ ! -f "$PLUGIN_DIR/.token" ] && [ -z "$TOKEN_ENV" ]; then
    if [ "$SOURCE" = "codex" ]; then
        HAS_TOKEN=""
    else
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
    fi
    if [ -z "$HAS_TOKEN" ]; then
        echo "[$(date -u +%FT%TZ)] probe-research-tap: no token configured; run 'python3 -m tap pair <token>' (or 'probe login'); skipping" >>"$LOG_FILE"
        printf '{"continue": true}\n'
        exit 0
    fi
fi

PID_FILE="/tmp/${WATCHER_PREFIX}-watcher-${SESSION_ID}.pid"

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
SHUTDOWN_FILE="/tmp/${WATCHER_PREFIX}-watcher-${SESSION_ID}.shutdown"
rm -f "$SHUTDOWN_FILE"

# Resolve Python interpreter — prefer plugin-local venv.
PY="$PLUGIN_ROOT/.venv/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3 || true)"
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "[$(date -u +%FT%TZ)] no python3 found, daemon disabled" >>"$LOG_FILE"
    printf '{"continue": true}\n'
    exit 0
fi

if [ -n "$TRANSCRIPT_PATH" ]; then
    TRANSCRIPT_ARGS=(--transcript "$TRANSCRIPT_PATH")
else
    TRANSCRIPT_ARGS=(--transcript-dir "${PRBE_CODEX_SESSIONS_DIR:-$HOME/.codex/sessions}")
fi

# Crash-recovery wrapper: respawn up to 5 times per minute.
# Self-terminates when shutdown sentinel exists (SessionEnd touches it).
#
# The wrapper runs as a SESSION LEADER (see the spawn below), so its PID is
# also its PGID and nothing outside its own group can take it down. It still
# forwards SIGTERM/SIGINT to the python child explicitly: the child is in this
# group, but an explicit forward is what makes a plain `kill -TERM <wrapper>`
# (no negation) tear down both, which is the fallback session-end.sh uses for
# pre-0.1.3 wrappers that are NOT group leaders.
#
# First action is rewriting the pid file with the wrapper's own $$: the hook
# records `$!`, which is correct on the fast path but stale if the setsid shim
# had to fork (see the spawn). Making the wrapper authoritative removes that
# race rather than reasoning about when it can happen.
WRAPPER_SCRIPT='
SID="$1"; CWD="$2"; PY="$3"; ROOT="$4"; LOG="$5"; PIDF="$6"; PREFIX="$7"; shift 7
echo $$ >"$PIDF"
SHUTDOWN="/tmp/${PREFIX}-watcher-${SID}.shutdown"
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
    "$PY" -m tap watch --session-id "$SID" --cwd "$CWD" --plugin-root "$ROOT" "$@" >>"$LOG" 2>&1 &
    CHILD_PID=$!
    wait "$CHILD_PID" 2>/dev/null || true
    CHILD_PID=""
    [ -f "$SHUTDOWN" ] && exit 0
    RESTART_COUNT=$((RESTART_COUNT + 1))
    sleep 5
done
'

# Detach the wrapper into ITS OWN SESSION via setsid(2).
#
# `nohup ... & disown` was NOT enough and this is the bug it hid: nohup only
# ignores SIGHUP, and disown only clears the shell's job table. Neither changes
# the process group, so the wrapper inherited the hook's PGID and any SIGTERM
# delivered to that group killed the daemon seconds after SessionStart —
# measured: wrapper PID 7006 / PGID 6958, dead in 14-34s, while an otherwise
# identical setsid'd daemon ran indefinitely.
#
# macOS ships no setsid(1), which is what the old comment here concluded was
# fatal — but python3 exposes os.setsid() and this hook already requires
# python3 (it parses the hook payload above), so the capability was always
# available. The shim setsids, then execs the wrapper IN PLACE, so $! stays the
# wrapper's PID and PID == PGID (a true group leader).
#
# setsid(2) fails with EPERM if the caller is already a group leader (a shell
# with job control puts each job in its own group). Hooks run non-interactively
# so the fast path holds, but fall back to fork-then-setsid rather than leave
# the daemon unisolated — the child rewrites the pid file, so a changed PID is
# still recorded correctly.
PROBE_TAP_SOURCE="$SOURCE" PYTHONPATH="$PLUGIN_ROOT" \
    nohup "$PY" -c '
import os, sys
try:
    os.setsid()
except OSError:
    if os.fork() != 0:
        os._exit(0)
    os.setsid()
os.execv(sys.argv[1], sys.argv[1:])
' /bin/bash -c "$WRAPPER_SCRIPT" wrapper \
    "$SESSION_ID" "$CWD" "$PY" "$PLUGIN_ROOT" "$LOG_FILE" "$PID_FILE" "$WATCHER_PREFIX" "${TRANSCRIPT_ARGS[@]}" \
    </dev/null >>"$LOG_FILE" 2>&1 &
disown

# The WRAPPER is the sole author of the pid file (it writes its own $$ first
# thing). This hook deliberately does NOT also write `$!`.
#
# Writing both raced: whichever landed last won, and `$!` can name an
# intermediate that is not the session leader — so the recorded pid was
# sometimes a non-leader, which is precisely what session-end.sh's group-kill
# decision keys on. One author, no race. Wait briefly so a concurrent
# SessionStart for this same session sees the file and does not double-spawn.
for _ in $(seq 1 40); do
    [ -s "$PID_FILE" ] && break
    sleep 0.05
done

# Spawn-failure diagnostics. A resumed session was observed clearing the stale
# sentinel (so this script provably ran to at least that point) and then leaving
# NO pid file, NO process and NO log line — the outage was completely invisible,
# and piping the same payload in by hand worked first try. Whatever the cause,
# the next occurrence should say so in the log. Never fatal: the contract is
# fail-open, and the reconciler recovers the transcript regardless.
if [ ! -s "$PID_FILE" ]; then
    echo "[$(date -u +%FT%TZ)] tap: wrapper wrote no pid file within 2s (spawn failed?); \
session=$SESSION_ID py=$PY root=$PLUGIN_ROOT — transcript will be recovered by the reconciler" \
        >>"$LOG_FILE"
elif ! kill -0 "$(cat "$PID_FILE" 2>/dev/null || echo 0)" 2>/dev/null; then
    echo "[$(date -u +%FT%TZ)] tap: wrapper pid $(cat "$PID_FILE" 2>/dev/null) not alive just \
after spawn; session=$SESSION_ID — transcript will be recovered by the reconciler" >>"$LOG_FILE"
fi

printf '{"continue": true}\n'
