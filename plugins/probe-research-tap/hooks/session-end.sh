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
        else
            # Not a group leader: signal the wrapper alone. Its TERM trap
            # forwards to the python child, so both still stop.
            kill -TERM "$PID" 2>/dev/null || true
        fi
    fi
    rm -f "$PID_FILE"
fi

# Deliberately do NOT rm the shutdown sentinel here. If the wrapper missed the
# forwarded TERM (raced mid-respawn) or a daemon child outlived it, the sentinel
# is the only remaining stop signal — the wrapper's respawn loop and the daemon's
# per-tick _shutdown_observed() both watch it. Deleting it would strand that
# orphan running. The next session-start for this session_id clears the stale
# sentinel before spawning a fresh wrapper.

exit 0
