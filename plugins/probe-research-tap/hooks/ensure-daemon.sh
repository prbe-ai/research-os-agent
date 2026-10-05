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

SOURCE="${PROBE_TAP_SOURCE:-claude_code}"
ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"
case "$SOURCE" in
    claude_code | codex | pi) ;;
    # Any other hook-plugin harness (Kimi Code) runs this from its own copy of
    # the plugin, and sets neither variable.
    *) [ -n "$ROOT" ] || ROOT="$(cd "$(dirname "$0")/.." && pwd)" ;;
esac
ROOT="${ROOT%/}"
[ -x "$ROOT/hooks/session-start.sh" ] || exit 0

HOOK_INPUT=$(cat 2>/dev/null || true)
# Its values, and its session id without the harness's own prefix, from its
# registry row (tap/hook_env.py): ONE python start, the same cost as the parse
# below.
REGISTRY_ENV=""
case "$SOURCE" in
    claude_code | codex | pi) ;;
    *)
        REGISTRY_ENV=$(printf '%s' "$HOOK_INPUT" | PROBE_TAP_SOURCE="$SOURCE" PYTHONPATH="$ROOT" \
            python3 -m tap hook-env --source "$SOURCE" --payload 2>/dev/null || echo "")
        ;;
esac
if [ -n "$REGISTRY_ENV" ]; then
    eval "$REGISTRY_ENV"
    SID="$TAP_SESSION_ID"
    HOOK_INPUT="$TAP_HOOK_INPUT"
else
    SID=$(printf '%s' "$HOOK_INPUT" | python3 -c \
        'import json,sys; v=json.load(sys.stdin).get("session_id"); print(v if isinstance(v,str) else "")' \
        2>/dev/null || echo "")
fi
[ -n "$SID" ] || exit 0

if [ -n "$REGISTRY_ENV" ]; then
    PREFIX="$TAP_WATCHER_PREFIX"
elif [ "$SOURCE" = "codex" ]; then
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
# A registry harness: the folder its row names (tap/config.py::plugin_dir()).
[ -z "$REGISTRY_ENV" ] || STATE="$TAP_PLUGIN_DIR"

# Ten-minute bound, the same rule `probe`'s ensure_capture uses and the same
# path (tap/config.py::heal_marker), so the two cannot drift into two rules.
MARKER="$STATE/heal/$SID"
if [ -f "$MARKER" ]; then
    NOW=$(date +%s)
    # GNU first, and then refuse anything that is not digits. On GNU coreutils
    # `-f` is *filesystem* status and `%m` is not one of its directives, so
    # `stat -f %m` reads `%m` as a FILENAME: it prints the marker's filesystem
    # block on stdout and exits 1, and the `||` appends the real mtime to that
    # block. `$((NOW - THEN))` then evaluates the whole thing as arithmetic,
    # reaches the bare word `File`, and `set -u` makes that fatal -- killing
    # this hook two lines above the respawn it exists to perform. BSD `stat`
    # rejects `-c` outright, so this order serves both. The digit guard is the
    # belt: no `||` chain can ever put prose into the arithmetic again.
    THEN=$(stat -c %Y "$MARKER" 2>/dev/null || stat -f %m "$MARKER" 2>/dev/null || echo 0)
    case "$THEN" in '' | *[!0-9]*) THEN=0 ;; esac
    [ $((NOW - THEN)) -lt 600 ] && exit 0
fi

mkdir -p "$STATE/heal" 2>/dev/null || true
: >"$MARKER" 2>/dev/null || true

# Never `exec`, and never propagate session-start.sh's status. This hook is
# fail-open by contract, and on UserPromptSubmit an exit code of 2 BLOCKS the
# user's prompt — so a spawn that went wrong must not be able to cost the
# researcher their turn. session-start.sh already logs its own failures.
if [ -n "$REGISTRY_ENV" ]; then
    # Kimi Code injects a UserPromptSubmit hook's stdout into the model's
    # context as text, so session-start.sh's Claude Code JSON stays out of it.
    printf '%s' "$HOOK_INPUT" | "$ROOT/hooks/session-start.sh" >/dev/null || true
else
    printf '%s' "$HOOK_INPUT" | "$ROOT/hooks/session-start.sh" || true
fi
exit 0
