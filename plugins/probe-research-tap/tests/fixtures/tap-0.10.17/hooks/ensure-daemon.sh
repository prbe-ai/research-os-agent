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
    # No python start on the common path (it was ~19 ms of this hook's ~25):
    # the payload's one "session_id", when it names exactly one and that is
    # a plain id (JSON escaping keeps a prompt's own text from spelling one).
    # Anything else is parsed, as before, and so is a payload past 256 KB
    # (a pasted log), where python's parse is as quick. Only a regex and a
    # whole-string match, both linear: never `${x#*pat}`, which is quadratic
    # in where the match is (12 s at 120 KB).
    SID=""
    if [ "${#HOOK_INPUT}" -le 262144 ] && [[ "$HOOK_INPUT" =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([A-Za-z0-9._-]{1,128})\" ]]; then
        SID="${BASH_REMATCH[1]}"
        case "$HOOK_INPUT" in *\"session_id\"*\"session_id\"*) SID="" ;; esac
    fi
    [ -n "$SID" ] || SID=$(printf '%s' "$HOOK_INPUT" | python3 -c \
        'import json,sys; v=json.load(sys.stdin).get("session_id"); print(v if isinstance(v,str) else "")' \
        2>/dev/null || echo "")
    SID="${SID%$'\r'}"
fi
[ -n "$SID" ] || exit 0

# Codex only (the branch below): say so when Probe's own hooks are not running
# in this session (hooks/probe-hooks-trust.sh). Printed only where this hook
# would otherwise exit silently; the cold path below prints session-start.sh's
# JSON instead, and the next prompt carries the message.
PROBE_HOOKS_CHECK=""
quiet_exit() {
    [ -z "$PROBE_HOOKS_CHECK" ] || probe_hooks_nudge "$SID" "$ROOT" || true
    exit 0
}

if [ -n "$REGISTRY_ENV" ]; then
    PREFIX="$TAP_WATCHER_PREFIX"
elif [ "$SOURCE" = "codex" ]; then
    PREFIX="prbe-codex-tap"
    if [ -f "$ROOT/hooks/probe-hooks-trust.sh" ]; then
        # shellcheck source=probe-hooks-trust.sh
        . "$ROOT/hooks/probe-hooks-trust.sh" && PROBE_HOOKS_CHECK=1
    fi
else
    PREFIX="probe-research-tap"
fi

# A watcher file of this user's: a regular file, not a link, owned by us. /tmp
# is shared: another user's file could name any process, and a link to a tty or
# a FIFO would block the read. OWN_PID: the pid in one, when it is a number
# above 1 (`kill -0 0` asks this hook's own process group, which is always
# there, so a file saying 0 read as a live daemon on every prompt, forever),
# else empty. Set, not printed: called without `$(...)`, and read with bash's
# own `read` (a regular file, so it cannot block), it starts no process, as
# the `cat` it replaced did.
OWN_PID=""
own_pid() {
    local pid=""
    OWN_PID=""
    { [ -f "$1" ] && [ ! -L "$1" ] && [ -O "$1" ] && [ -s "$1" ]; } || return 0
    IFS= read -r -n 32 pid <"$1" 2>/dev/null || true
    pid="${pid%$'\r'}"
    case "$pid" in '' | *[!0-9]*) return 0 ;; esac
    [ "$pid" -gt 1 ] 2>/dev/null || return 0
    OWN_PID="$pid"
}

PIDF="/tmp/${PREFIX}-watcher-${SID}.pid"
PID=""
if [ -f "$PIDF" ]; then
    own_pid "$PIDF"
    PID="$OWN_PID"
fi
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    quiet_exit
fi
# The Probe service records it (plan A2): its pid, in the marker `tap record`
# wrote. A service that restarted resumes the session under a new pid; the cold
# path below (at most every ten minutes) asks it again and rewrites the marker.
# Only a marker of this user's naming a real pid (`own_pid`): pid 1 is init,
# never the service.
HOSTF="/tmp/${PREFIX}-watcher-${SID}.hosted"
HOSTED_GONE=""
if [ -f "$HOSTF" ] && [ ! -L "$HOSTF" ] && [ -O "$HOSTF" ] && [ -s "$HOSTF" ]; then
    own_pid "$HOSTF"
    PID="$OWN_PID"
    if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
        quiet_exit
    fi
    # Git Bash on Windows: its `kill` knows MSYS pids, not the Windows pid the
    # marker holds. The marker is trusted there: the service rewrites it each
    # time it resumes a session, and the OS keeper (a Task Scheduler task)
    # starts a gone service again within a minute.
    case "${OSTYPE:-}" in msys* | cygwin* | win*) [ -n "$PID" ] && quiet_exit ;; esac
    # The service that recorded it is gone (a crash, a kill): no ten-minute
    # wait below; starting it again resumes every session it recorded.
    HOSTED_GONE=1
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
    [ $((NOW - THEN)) -lt 600 ] && [ -z "$HOSTED_GONE" ] && quiet_exit
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
