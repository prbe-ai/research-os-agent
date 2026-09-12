#!/bin/bash
# UserPromptSubmit: make sure this session still has a capture daemon.
#
# Nothing else does. SessionStart spawns one and SessionEnd stops it, so a
# daemon that never started (spawn failed, machine asleep at start) or died
# mid-session (crash loop exhausted, a sibling agent's teardown SIGTERMed the
# shared watcher prefix) stays dead until a genuinely NEW session starts.
#
# THE COMMON PATH MUST BE FREE. This runs on every prompt, alongside two other
# hooks. A live daemon costs one file read and one `kill -0`: no python, no
# subprocess, no network. Only the rare unhealthy case pays for a spawn.
set -uo pipefail

ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
ROOT="${ROOT%/}"
[ -x "$ROOT/hooks/session-start.sh" ] || exit 0

HOOK_INPUT=$(cat 2>/dev/null || true)
SID=$(printf '%s' "$HOOK_INPUT" | python3 -c \
    'import json,sys; v=json.load(sys.stdin).get("session_id"); print(v if isinstance(v,str) else "")' \
    2>/dev/null || echo "")
[ -n "$SID" ] || exit 0

SOURCE="${PROBE_TAP_SOURCE:-claude_code}"
if [ "$SOURCE" = "codex" ]; then
    PREFIX="prbe-codex-tap"
else
    PREFIX="probe-research-tap"
fi

PIDF="/tmp/${PREFIX}-watcher-${SID}.pid"
if [ -s "$PIDF" ]; then
    PID=$(cat "$PIDF" 2>/dev/null || echo "")
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        exit 0
    fi
fi

# --- cold path only, from here down ---
#
# The state dir must be the SAME path `tap/config.py::plugin_dir()` computes,
# because the marker below is `heal_marker()`'s file and the ten-minute bound
# is one rule shared with `probe`'s ensure_capture. Two spellings would be two
# independent windows, each blind to the other and neither reporting an error.
# tests/test_ensure_daemon_hook.py pins this against `cfg.heal_marker()`.
case "$SOURCE" in
    codex)
        STATE="${PRBE_CODEX_TAP_PLUGIN_DIR:-}"
        if [ -z "$STATE" ]; then
            STATE="$HOME/.codex/state/probe-research-tap"
            # plugin_dir()'s one-way legacy fallback: a standalone tap's durable
            # state keeps its path, clean installs get the unified name.
            if [ -e "$HOME/.codex/state/prbe-codex-tap-plugin" ] && [ ! -e "$STATE" ]; then
                STATE="$HOME/.codex/state/prbe-codex-tap-plugin"
            fi
        fi
        ;;
    pi)
        STATE="${PROBE_PI_TAP_PLUGIN_DIR:-$HOME/.pi/agent/state/probe-research-tap}"
        ;;
    *)
        STATE="${PROBE_RESEARCH_TAP_PLUGIN_DIR:-$HOME/.claude/plugins/probe-research-tap}"
        ;;
esac

# Ten-minute bound, the same rule `probe`'s ensure_capture uses and the same
# path (tap/config.py::heal_marker), so the two cannot drift into two rules.
MARKER="$STATE/heal/$SID"
if [ -f "$MARKER" ]; then
    NOW=$(date +%s)
    THEN=$(stat -f %m "$MARKER" 2>/dev/null || stat -c %Y "$MARKER" 2>/dev/null || echo 0)
    [ $((NOW - THEN)) -lt 600 ] && exit 0
fi

mkdir -p "$STATE/heal" 2>/dev/null || true
: >"$MARKER" 2>/dev/null || true

# Never `exec`, and never propagate session-start.sh's status. This hook is
# fail-open by contract, and on UserPromptSubmit an exit code of 2 BLOCKS the
# user's prompt — so a spawn that went wrong must not be able to cost the
# researcher their turn. session-start.sh already logs its own failures.
printf '%s' "$HOOK_INPUT" | "$ROOT/hooks/session-start.sh" || true
exit 0
