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
# ONE hooks.json ENTRY: this script with `--wait`, under a 20s timeout. Through
# tap 0.9.24 there were two: the original entry (timeout 3, no flag), kept
# byte-identical because Codex trusts each hook by a hash of its whole entry,
# timeout included, and silently skips one whose hash changed until the
# researcher re-approves it, plus a second entry with `--wait`. Every Probe
# plugin now keeps exactly one entry per event, so the flagless one is gone: a
# Codex user who had approved only that one approves this one once in `/hooks`
# (`probe status` says so, and its `--json` `codex_hooks` tells the desktop
# app). Codex clamps the 20s to 3s (it says so in `/hooks` as "1 issue loading
# hooks"; the 20s stays, because Claude Code enforces each hook's own timeout
# and a container that raises its exit budget needs the full wait).
#
# Without the flag the script still stops the daemon and returns at once. A
# Codex session that started on an older release keeps the two entries it
# loaded then, and once its start folder is replaced both run THIS script, in
# either order, so the pid is handed over in a `.stopping` file before the pid
# file is removed.
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
SOURCE="${PROBE_TAP_SOURCE:-claude_code}"

# `--wait`'s deadline, FIRST, so this script's own start-up (cat, python3, ps)
# spends it instead of adding to it. It is a clock: bash 3.2 (/bin/bash on
# macOS) has no sub-second time, and BSD `date` no %N, so a background `sleep`
# is the deadline and the waits below end when it does. Counting 0.2s sleeps
# instead overran on macOS, where one `sleep 0.2` takes ~236ms on a runner (5ms
# to start it, the rest the timer firing late): 75 ticks took 19s, Codex's 13
# took 3.3s against its 3s, and Claude Code's 6 took 1.66s against its 1.5s.
#
# WAIT_MS: at most 15s. Under Claude Code, its exit budget less 400ms for what
# the deadline cannot cover (the hooks.json wrapper's and this bash's start,
# the deadline's own lateness, the exit): ~5ms on Linux, but up to 190ms on a
# macOS runner, whose 1.2s `sleep` fired up to 82ms late. So 1.1s at the
# default 1.5s. Codex (0.159) clamps every SessionEnd hook's timeout to 3s,
# whatever the entry asks: 2.6s, the same 400ms reserve. A registry harness
# (Kimi Code) awaits its SessionEnd hooks up to the entry's own timeout, so it
# gets the full 15s.
WAIT_MS=15000
if [ "$SOURCE" = codex ]; then
    WAIT_MS=2600
elif [ "$SOURCE" = claude_code ]; then
    BUDGET_MS="${CLAUDE_CODE_SESSIONEND_HOOKS_TIMEOUT_MS:-}"
    case "$BUDGET_MS" in '' | *[!0-9]*) BUDGET_MS=1500 ;; esac
    # Strip leading zeros so the length check measures magnitude, and read 0
    # as unset, as Claude Code does.
    BUDGET_MS="${BUDGET_MS#"${BUDGET_MS%%[!0]*}"}"
    [ -n "$BUDGET_MS" ] || BUDGET_MS=1500
    # Past six digits it is over the 15s cap anyway, and a 20-digit value
    # would overflow the arithmetic below.
    [ "${#BUDGET_MS}" -gt 6 ] || WAIT_MS=$((BUDGET_MS - 400))
    if [ "$WAIT_MS" -gt 15000 ]; then WAIT_MS=15000; fi
    if [ "$WAIT_MS" -lt 0 ]; then WAIT_MS=0; fi
fi
printf -v WAIT_S '%d.%03d' "$((WAIT_MS / 1000))" "$((WAIT_MS % 1000))"
DEADLINE=""
POLLER=""
FINISH=""
# Stop one of this script's helpers with signal $2 and reap it. Only while bash
# still holds it unreaped (`jobs -r`): once reaped, its pid is free, and may be
# another process's. Reaped here, not left to an init: a container's PID 1
# that does not reap would keep it a zombie. Quiet, as bash 3.2 reports a
# killed job ("Terminated") on stderr.
stop_helper() {
    local job
    [ -n "$1" ] || return 0
    for job in $(jobs -rp); do
        if [ "$job" = "$1" ]; then kill "-$2" "$1" || true; fi
    done
    wait "$1" || true
} 2>/dev/null
if [ "$WAIT" = 1 ] && [ "$WAIT_MS" -gt 0 ]; then
    # Its own stdio, so the harness never waits on a pipe it holds open.
    sleep "$WAIT_S" </dev/null >/dev/null 2>&1 &
    DEADLINE=$!
    trap 'stop_helper "$POLLER" TERM; stop_helper "$FINISH" KILL; stop_helper "$DEADLINE" TERM' EXIT
fi

HOOK_INPUT="$(cat)"
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
# Windows: python3 ends each line with CR LF, and only the last one's goes with
# the command substitution: the session id kept its CR, and every file named
# after it (the sentinel the service's recorder watches) was another file.
SESSION_ID="${SESSION_ID%$'\r'}"
REASON="${REASON%$'\r'}"

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

# A watcher file of this user's: a regular file, not a link, owned by us. /tmp
# is shared, so a file anyone else put there names nothing of ours, and a link
# to a tty or a FIFO would block the read below.
own_file() { [ -f "$1" ] && [ ! -L "$1" ] && [ -O "$1" ]; }
# The pid in watcher file $1, or nothing.
pid_in() {
    local pid=""
    own_file "$1" || return 0
    pid=$(head -c 32 "$1" 2>/dev/null | tr -d '[:space:]' || true)
    case "$pid" in '' | *[!0-9]*) return 0 ;; esac
    [ "$pid" -gt 1 ] 2>/dev/null || return 0
    printf '%s' "$pid"
}
# The command line of pid $1: `ps`, else Linux's /proc (a slim container has no
# `ps`); empty when neither answers.
cmdline_of() {
    local cmd=""
    cmd=$(ps -ww -o command= -p "$1" 2>/dev/null || true)
    if [ -z "$cmd" ] && [ -r "/proc/$1/cmdline" ]; then
        cmd=$(tr '\0' ' ' <"/proc/$1/cmdline" 2>/dev/null || true)
    fi
    printf '%s' "$cmd"
}
# Does pid $1 run this session's `tap watch`? Its command line names both, as
# session-start.sh proves it before replacing a wrapper.
runs_this_session() {
    case "$(cmdline_of "$1")" in *"tap watch"*"$SESSION_ID"*) return 0 ;; esac
    return 1
}
# Does the process group pid $1 led still run this session's `tap watch`? The
# leader itself, or (its wrapper gone, as after a hand-over: the wrapper exits
# once its TERM trap forwarded) a member of the group: from `ps`, else (no
# `ps`, or a busybox one that knows no `pgid`/`command`) Linux's /proc, each
# process's group from its `stat` and its command line from `cmdline`.
group_runs_this_session() {
    local group="$1" listing="" d stat rest
    runs_this_session "$group" && return 0
    if command -v ps >/dev/null 2>&1; then
        listing=$(ps -A -ww -o pgid= -o command= 2>/dev/null || true)
    fi
    if [ -n "$listing" ]; then
        printf '%s\n' "$listing" \
            | awk -v g="$group" -v s="$SESSION_ID" '$1 == g && index($0, "tap watch") && index($0, s) { f = 1 } END { exit !f }'
        return
    fi
    [ -r /proc/self/stat ] || return 1
    for d in /proc/[0-9]*; do
        IFS= read -r stat <"$d/stat" 2>/dev/null || continue
        # `pid (comm) state ppid pgrp ...`: comm may hold spaces, so after the last `) `.
        rest="${stat##*) }"
        set -f
        # shellcheck disable=SC2086
        set -- $rest
        set +f
        [ "${3:-}" = "$group" ] || continue
        case "$(tr '\0' ' ' <"$d/cmdline" 2>/dev/null || true)" in
            *"tap watch"*"$SESSION_ID"*) return 0 ;;
        esac
    done
    return 1
}

# The pid file, or the hand-over a sibling entry left before removing it. Only
# a pid that is provably THIS session's tap wrapper is ever signalled: /tmp is
# world-writable and pids are reused, so a pid file the wrapper left when it
# died (or one a session the service now records kept) can name any process by
# now, and its group with it. No proof, no signal: the stale pid file is only
# removed. A hand-over (`.stopping`) is NEVER signalled: the entry that wrote it
# signalled already. It is only waited on, and only while its group still runs
# this session's `tap watch`.
PID=$(pid_in "$PID_FILE")
HANDED_OVER=0
if [ -z "$PID" ]; then
    PID=$(pid_in "$STOPPING_FILE")
    [ -z "$PID" ] || HANDED_OVER=1
fi
LEADER=0

if [ -n "$PID" ] && [ "$HANDED_OVER" = 1 ]; then
    if kill -0 -- "-$PID" 2>/dev/null && group_runs_this_session "$PID"; then
        LEADER=1
    else
        PID=""
    fi
elif [ -n "$PID" ] && runs_this_session "$PID"; then
    # Written only over nothing or a file of this user's: never through a link.
    if { [ ! -e "$STOPPING_FILE" ] && [ ! -L "$STOPPING_FILE" ]; } || own_file "$STOPPING_FILE"; then
        printf '%s' "$PID" >"$STOPPING_FILE" 2>/dev/null || true
    fi
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
else
    PID=""
fi
rm -f "$PID_FILE" 2>/dev/null || true

# `--wait`: stay until the wrapper's group (the daemon) has exited, so the agent
# cannot exit, and take a container down, before the FINALIZE leaves. Not on
# `clear` or `resume`: the agent keeps running (and so does the delivery), and a
# wait there would freeze the researcher's /clear.
WAITS=0
[ -n "$DEADLINE" ] && [ "$REASON" != clear ] && [ "$REASON" != resume ] && WAITS=1

# Return once "$@" stops holding, or at the deadline set at the top. This shell
# waits on the deadline itself, so a wait ends when it does, not at the end of a
# poll (one `sleep 0.05` takes 88ms on a macOS runner). A background poller
# checks "$@" every 50ms and, once it fails, ends the wait early with SIGUSR1:
# a trapped signal returns `wait`, and the deadline runs on for the next wait.
# The poller sleeps before its first check, by when this shell is in `wait`
# (were it not, the wait would run to the deadline: late, never over budget),
# and stops with the deadline, so a killed hook leaves none behind. Stopped,
# it ends and reaps its own sleep, then exits 0.
wait_while() {
    "$@" 2>/dev/null || return 0
    trap ':' USR1
    (
        nap=""
        trap 'if [ -n "$nap" ]; then kill "$nap" 2>/dev/null || true; fi; wait 2>/dev/null || true; exit 0' TERM
        held=1
        polls=$((WAIT_MS / 50))
        while [ "$polls" -gt 0 ]; do
            sleep 0.05 &
            nap=$!
            wait "$nap" || true
            nap=""
            kill -0 "$DEADLINE" 2>/dev/null || exit 0
            # Once "$@" fails, signal every poll until stopped: bash 3.2's
            # `wait` missed ~1 in 9 single signals in a loop of these tests.
            if [ "$held" = 0 ] || ! "$@" 2>/dev/null; then
                held=0
                kill -USR1 "$$" 2>/dev/null || true
            fi
            polls=$((polls - 1))
        done
    ) </dev/null >/dev/null 2>&1 &
    POLLER=$!
    wait "$DEADLINE" 2>/dev/null || true
    # Ignored from here, so a late one cannot cut short the reaping wait.
    trap '' USR1
    stop_helper "$POLLER" TERM
    POLLER=""
}

# The Probe service records it (plan A2): the sentinel above ends its recorder
# as it ends a `tap watch`, and `tap finish` waits for the last pass the same way.
# The service finishes the session whether or not this client is still waiting,
# so the deadline ends the client too: its start-up and the call's own timeout
# never push the hook past its budget.
HOSTED_FILE="/tmp/${WATCHER_PREFIX}-watcher-${SESSION_ID}.hosted"
if [ -s "$HOSTED_FILE" ]; then
    if [ "$WAITS" = 1 ]; then
        TAP_ROOT="${TAP_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"
        # On the interpreter session-start.sh ran the tap on (hooks/tap-python.sh):
        # the one it remembered in the tap's state folder, a file read and no
        # python start inside the exit budget. None remembered (an older tap
        # started this session): `python3`, as before.
        PY=""
        TAP_HOOKS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        if [ -f "$TAP_HOOKS/tap-python.sh" ]; then
            # shellcheck source=tap-python.sh
            . "$TAP_HOOKS/tap-python.sh"
            if [ -n "$REGISTRY_ENV" ]; then
                PY=$(tap_python_cached "${TAP_PLUGIN_DIR:-}" || true)
            else
                PY=$(tap_python_cached "$(tap_state_dir "$SOURCE")" || true)
            fi
        fi
        [ -n "$PY" ] || PY=python3
        PROBE_TAP_SOURCE="$SOURCE" PYTHONPATH="$TAP_ROOT" "$PY" -m tap finish --session-id "$SESSION_ID" \
            --wait "$WAIT_S" </dev/null >/dev/null 2>&1 &
        FINISH=$!
        wait_while kill -0 "$FINISH"
        stop_helper "$FINISH" KILL
        FINISH=""
    fi
    rm -f "$HOSTED_FILE" 2>/dev/null || true
fi

if [ "$WAIT" = 1 ]; then
    if [ "$LEADER" = 1 ] && [ "$WAITS" = 1 ]; then
        wait_while kill -0 -- "-$PID"
    fi
    rm -f "$STOPPING_FILE" 2>/dev/null || true
fi

# Deliberately do NOT rm the shutdown sentinel here. If the wrapper missed the
# forwarded TERM (raced mid-respawn) or a daemon child outlived it, the sentinel
# is the only remaining stop signal — the wrapper's respawn loop and the daemon's
# per-tick _shutdown_observed() both watch it. Deleting it would strand that
# orphan running. The next session-start for this session_id clears the stale
# sentinel before spawning a fresh wrapper.

exit 0
