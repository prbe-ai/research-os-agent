#!/usr/bin/env bash
# SessionEnd hook — terminate the tap daemon for this session.
#
# Touches the shutdown sentinel before SIGTERMing the wrapper so the
# crash-recovery loop exits instead of respawning the daemon one more time.
#
# With `--wait`, it then WAITS, bounded, for the daemon to finish. SIGTERM does
# not kill the daemon: it makes one last pass that delivers the session's
# FINALIZE, the only signal that the session is over. Returning at once let the
# agent exit first, and wherever the agent's exit takes the machine with it (a
# container, a CI job, `claude -p` in a sandbox) the daemon died mid-delivery:
# 46 of 51 claude_code sessions the server had to end by itself came from one
# containerized device.
#
# WHY TWO hooks.json ENTRIES, not one with a longer timeout. Codex trusts each
# hook by a hash of its whole entry, timeout included, and silently skips one
# whose hash changed until the researcher re-approves it. So the original
# entry (timeout 3, no flag) stays byte-identical, and a second entry runs this
# script with `--wait` under a 20s timeout. Claude Code runs both in parallel
# and waits for the longer, up to its exit budget (below); Codex keeps running
# the old one until the new one is approved, and clamps the new one to 3s (it
# says so in `/hooks` as "1 issue loading hooks"; the 20s stays, because Claude
# Code enforces each hook's own timeout and a container that raises its exit
# budget needs the full wait). Either may run first, so the pid is handed over
# in a `.stopping` file before the pid file is removed.
#
# CLAUDE CODE'S EXIT BUDGET IS NOT THE ENTRY'S TIMEOUT. At exit Claude Code
# bounds the whole SessionEnd phase at CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS,
# else at the longest SessionEnd `timeout` in the user's own settings (a
# plugin's hooks.json is not counted), else at 1.5s (the variable arrived in
# 2.1.74), and cancels whatever is still running: "SessionEnd hook [...]
# failed: Hook cancelled" on every exit whose last pass took longer (seen on
# 2.1.283). So under Claude Code the wait ends inside that budget, and the hook
# exits 0 instead of being killed. Only the variable is read, not settings
# files, so a longer budget from a settings hook is left unused: short, never
# cancelled. A container or CI job that needs the daemon's last pass (often
# 1.3-5s) sets the variable to 16000 for the full 15s wait.

set -euo pipefail

WAIT=0
[ "${1:-}" = "--wait" ] && WAIT=1

HOOK_INPUT="$(cat)"
SOURCE="${PROBE_TAP_SOURCE:-claude_code}"
# Any harness but Claude Code and Codex: its values, and its session id without
# the harness's own prefix, from its registry row (tap/hook_env.py).
REGISTRY_ENV=""
case "$SOURCE" in
    claude_code | codex) ;;
    *)
        TAP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
        REGISTRY_ENV=$(printf '%s' "$HOOK_INPUT" | PROBE_TAP_SOURCE="$SOURCE" PYTHONPATH="$TAP_ROOT" \
            python3 -m tap hook-env --source "$SOURCE" --payload 2>/dev/null || true)
        ;;
esac
if [ -n "$REGISTRY_ENV" ]; then
    eval "$REGISTRY_ENV"
    HOOK_INPUT="$TAP_HOOK_INPUT"
fi
# One python3 start for both fields, and `-S` skips site-packages: start-up
# spends the --wait budget below.
PARSED=$(printf '%s' "$HOOK_INPUT" | python3 -S -c 'import json,sys
d=json.load(sys.stdin); v=d.get("reason")
print(d.get("session_id","")); print(v if isinstance(v,str) else "")' 2>/dev/null || echo "")
SESSION_ID=""
REASON=""
{ IFS= read -r SESSION_ID || true; IFS= read -r REASON || true; } <<EOF
$PARSED
EOF

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

WATCHER_PREFIX="probe-research-tap"
[ "$SOURCE" = "codex" ] && WATCHER_PREFIX="prbe-codex-tap"
[ -n "$REGISTRY_ENV" ] && WATCHER_PREFIX="$TAP_WATCHER_PREFIX"
PID_FILE="/tmp/${WATCHER_PREFIX}-watcher-${SESSION_ID}.pid"
SHUTDOWN_FILE="/tmp/${WATCHER_PREFIX}-watcher-${SESSION_ID}.shutdown"
STOPPING_FILE="/tmp/${WATCHER_PREFIX}-watcher-${SESSION_ID}.stopping"

touch "$SHUTDOWN_FILE"

# The pid file, or the hand-over a sibling entry left before removing it.
PID=$(cat "$PID_FILE" 2>/dev/null || true)
[ -n "$PID" ] || PID=$(cat "$STOPPING_FILE" 2>/dev/null || true)
case "$PID" in '' | *[!0-9]*) PID="" ;; esac
LEADER=0

if [ -n "$PID" ]; then
    printf '%s' "$PID" >"$STOPPING_FILE" 2>/dev/null || true
    # Kill the wrapper's process group so the Python child dies too — but
    # ONLY when the wrapper really leads that group.
    #
    # `kill -TERM -<PID>` names the process group whose PGID equals PID. A
    # setsid'd wrapper (0.1.3+) leads its own group, so that is exactly the
    # wrapper + its child. A pre-0.1.3 wrapper does NOT — it inherited the
    # hook's PGID.
    #
    # The risk is narrower than "a non-leader pid hits some other group": a
    # PGID exists only because a process with that id led the group, so
    # while our wrapper holds pid P, no other group can have pgid P. What
    # CAN happen is an orphaned group — the leader exits, surviving members
    # keep the group alive, and that pgid is now a free pid. A stale pid
    # file naming it would then signal strangers. Verify leadership before
    # using the negated form.
    # `|| true` is load-bearing under `set -euo pipefail`: ps exits 1 for a
    # pid that is already gone (the common case — the wrapper may have died
    # on its own), and without it the assignment aborts this script before
    # the `rm -f "$PID_FILE"` below, leaking the stale pid file that this
    # very check exists to defuse.
    PGID=$(ps -o pgid= -p "$PID" 2>/dev/null | tr -d ' ' || true)
    if [ -n "$PGID" ] && [ "$PGID" = "$PID" ]; then
        kill -TERM "-$PID" 2>/dev/null || true
        LEADER=1
    elif ! command -v ps >/dev/null 2>&1 && kill -0 -- "-$PID" 2>/dev/null; then
        # No `ps` (a slim container image) but a group this pid leads
        # exists: the 0.1.3+ wrapper always leads its own. Signal the
        # wrapper alone as before, and wait on the group below.
        kill -TERM "$PID" 2>/dev/null || true
        LEADER=1
    else
        # Not a group leader: signal the wrapper alone. Its TERM trap
        # forwards to the python child, so both still stop.
        kill -TERM "$PID" 2>/dev/null || true
    fi
fi
rm -f "$PID_FILE"

# `--wait`: stay until the wrapper's group (the daemon) has exited, so the agent
# cannot exit, and take a container down, before the FINALIZE leaves. Not on
# `clear` or `resume`: the agent keeps running (and so does the delivery), and a
# wait there would freeze the researcher's /clear. At most 75 x 0.2s = 15s;
# under Claude Code, at most its exit budget less 300ms for this script's
# start-up (python3, ps: ~60ms on Linux) and the hooks.json wrapper, which spend
# the same budget: 6 ticks at the default 1.5s. Codex (0.159) clamps every
# SessionEnd hook's timeout to 3s, whatever the entry asks, so its wait ends at
# 2.7s: 13 ticks, the same 300ms margin. A registry harness (Kimi Code) awaits
# its SessionEnd hooks up to the entry's own timeout, so it gets the full wait.
WAIT_TICKS=75
if [ "$SOURCE" = codex ] && [ -z "$REGISTRY_ENV" ]; then
    WAIT_TICKS=13
elif [ -z "$REGISTRY_ENV" ]; then
    BUDGET_MS="${CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS:-}"
    case "$BUDGET_MS" in '' | *[!0-9]*) BUDGET_MS=1500 ;; esac
    # Strip leading zeros so the length check measures magnitude, and read 0
    # as unset, as Claude Code does.
    BUDGET_MS="${BUDGET_MS#"${BUDGET_MS%%[!0]*}"}"
    [ -n "$BUDGET_MS" ] || BUDGET_MS=1500
    # Past six digits it is over the 75-tick cap anyway, and a 20-digit value
    # would overflow the arithmetic below.
    if [ "${#BUDGET_MS}" -gt 6 ]; then
        WAIT_TICKS=75
    else
        WAIT_TICKS=$(((BUDGET_MS - 300) / 200))
    fi
    if [ "$WAIT_TICKS" -gt 75 ]; then WAIT_TICKS=75; fi
    if [ "$WAIT_TICKS" -lt 0 ]; then WAIT_TICKS=0; fi
fi
if [ "$WAIT" = 1 ]; then
    if [ "$LEADER" = 1 ] && [ "$REASON" != clear ] && [ "$REASON" != resume ]; then
        i=0
        while [ "$i" -lt "$WAIT_TICKS" ] && kill -0 -- "-$PID" 2>/dev/null; do
            sleep 0.2
            i=$((i + 1))
        done
    fi
    rm -f "$STOPPING_FILE"
fi

# Deliberately do NOT rm the shutdown sentinel here. If the wrapper missed the
# forwarded TERM (raced mid-respawn) or a daemon child outlived it, the sentinel
# is the only remaining stop signal — the wrapper's respawn loop and the daemon's
# per-tick _shutdown_observed() both watch it. Deleting it would strand that
# orphan running. The next session-start for this session_id clears the stale
# sentinel before spawning a fresh wrapper.

exit 0
