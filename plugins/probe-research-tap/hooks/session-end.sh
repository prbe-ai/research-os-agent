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
# and waits for the longer; Codex keeps running the old one until the new one
# is approved. Either may run first, so the pid is handed over in a
# `.stopping` file before the pid file is removed.

set -euo pipefail

WAIT=0
[ "${1:-}" = "--wait" ] && WAIT=1

HOOK_INPUT="$(cat)"
SESSION_ID=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null || echo "")
REASON=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys; v=json.load(sys.stdin).get("reason"); print(v if isinstance(v,str) else "")' 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ]; then
    exit 0
fi

SOURCE="${PROBE_TAP_SOURCE:-claude_code}"
WATCHER_PREFIX="probe-research-tap"
[ "$SOURCE" = "codex" ] && WATCHER_PREFIX="prbe-codex-tap"
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
# wait there would freeze the researcher's /clear. 75 x 0.2s = 15s, inside the
# entry's 20s timeout.
if [ "$WAIT" = 1 ]; then
    if [ "$LEADER" = 1 ] && [ "$REASON" != clear ] && [ "$REASON" != resume ]; then
        i=0
        while [ "$i" -lt 75 ] && kill -0 -- "-$PID" 2>/dev/null; do
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
