#!/usr/bin/env bash
# SessionStart hook for probe-research-tap.
#
# Reads {session_id, transcript_path, cwd} from stdin and spawns the tap
# daemon detached, wrapped in a crash-recovery loop. Wrapper PID is recorded
# in /tmp/probe-research-tap-watcher-<sid>.pid for SessionEnd cleanup.
#
# Claude Code's and Codex's values are spelled below. Every other hook-plugin
# harness (Kimi Code) gets its values, and its payload in Claude Code's shape,
# from its registry row through `python3 -m tap hook-env` (tap/hook_env.py).

set -euo pipefail

PLUGIN_ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}}"
SOURCE="${PROBE_TAP_SOURCE:-claude_code}"
# Git Bash on Windows: the Windows path of this script's `/tmp`, for the Python
# side and the Probe service's recorder of this session (`tap/config.py`
# WATCHER_DIR_ENV): the temp folder Python finds there need not be it.
case "${OSTYPE:-}" in
msys* | cygwin*)
    PROBE_TAP_WATCHER_DIR=$(cygpath -w /tmp 2>/dev/null || true)
    [ -n "$PROBE_TAP_WATCHER_DIR" ] && export PROBE_TAP_WATCHER_DIR
    ;;
esac
HOOK_INPUT="$(cat)"
REGISTRY_ENV=""
case "$SOURCE" in
    claude_code | codex) ;;
    *)
        TAP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
        REGISTRY_ENV=$(printf '%s' "$HOOK_INPUT" | PROBE_TAP_SOURCE="$SOURCE" PYTHONPATH="$TAP_ROOT" \
            python3 -m tap hook-env --source "$SOURCE" --payload --transcript 2>/dev/null || true)
        ;;
esac
TAP_MANIFEST_DIR=""
TAP_HAS_TOKEN=""
IS_CODEX=""
if [ "$SOURCE" = "codex" ]; then
    IS_CODEX=1
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
elif [ -n "$REGISTRY_ENV" ]; then
    eval "$REGISTRY_ENV"
    # This script runs from that harness's own copy of the plugin.
    PLUGIN_ROOT="$TAP_ROOT"
    PLUGIN_DIR="$TAP_PLUGIN_DIR"
    WATCHER_PREFIX="$TAP_WATCHER_PREFIX"
    HOOK_INPUT="$TAP_HOOK_INPUT"
else
    PLUGIN_DIR="${PROBE_RESEARCH_TAP_PLUGIN_DIR:-$HOME/.claude/plugins/probe-research-tap}"
    WATCHER_PREFIX="probe-research-tap"
fi
LOG_DIR="$PLUGIN_DIR/logs"
mkdir -p "$LOG_DIR"
# Git Bash: the watcher folder (its `/tmp`), kept in the tap's state folder for
# what this hook does not start: `probe session status`, `probe doctor` and the
# status line run from other shells, without PROBE_TAP_WATCHER_DIR. Rewritten
# only when it changed, whole (a reader sees the old folder or the new one).
if [ -n "${PROBE_TAP_WATCHER_DIR:-}" ] && case "${OSTYPE:-}" in msys* | cygwin*) true ;; *) false ;; esac; then
    WATCHER_DIR_FILE="$PLUGIN_DIR/.watcher_dir"
    if [ "$(cat "$WATCHER_DIR_FILE" 2>/dev/null || true)" != "$PROBE_TAP_WATCHER_DIR" ]; then
        printf '%s' "$PROBE_TAP_WATCHER_DIR" >"$WATCHER_DIR_FILE.$$" 2>/dev/null \
            && mv -f "$WATCHER_DIR_FILE.$$" "$WATCHER_DIR_FILE" 2>/dev/null \
            || rm -f "$WATCHER_DIR_FILE.$$" 2>/dev/null || true
    fi
fi

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
# Owner records (tap/owner.py) and session-end's pid hand-overs go the same way:
# a running daemon refreshes its owner record every tick, so only a finished
# session's files are ever 2 days old.
find /tmp/ -maxdepth 1 \( -name "${WATCHER_PREFIX}-watcher-*.shutdown" \
    -o -name "${WATCHER_PREFIX}-watcher-*.owner" -o -name "${WATCHER_PREFIX}-watcher-*.stopping" \) \
    -mtime +2 -delete 2>/dev/null || true

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

# Whether the `.hosted` marker $1 must be kept: it is not this user's (not ours
# to judge), or the pid in it is this user's live Probe service. Nothing
# rewrites the marker while the service records the session, and a session can
# live for days: pruning it by age alone left a live session reading "not
# started" in `probe session status` and the status line.
keep_hosted() {
    local pid lock base
    # Not a regular file (a link to a tty, a FIFO): never read, never ours.
    { [ -f "$1" ] && [ ! -L "$1" ]; } || return 1
    [ -O "$1" ] || return 0
    pid=$(head -c 32 "$1" 2>/dev/null | tr -d '[:space:]' || true)
    case "$pid" in '' | *[!0-9]*) return 1 ;; esac
    [ "$pid" -gt 1 ] || return 1
    case "${OSTYPE:-}" in
    msys* | cygwin*)
        # A Windows pid, which Git Bash's `kill` and `ps` do not know: the
        # service names itself in its lock file (`server._name_holder`).
        base=$(cygpath -u "${LOCALAPPDATA:-}" 2>/dev/null || true)
        [ -n "$base" ] || return 1
        for lock in "$base"/probe/service/*/service.lock; do
            [ -f "$lock" ] || continue
            [ "$(head -c 64 "$lock" 2>/dev/null | tr -s ' \r\n' ' ' | cut -d' ' -f1)" = "$pid" ] && return 0
        done
        return 1
        ;;
    esac
    kill -0 "$pid" 2>/dev/null || return 1
    case "$(cmdline_of "$pid")" in *probe.service*) return 0 ;; esac
    return 1
}
# Kept ones are touched, so each is looked at again in 2 days, not every start.
find /tmp/ -maxdepth 1 -name "${WATCHER_PREFIX}-watcher-*.hosted" -mtime +2 2>/dev/null \
    | while IFS= read -r _hf; do
        if keep_hosted "$_hf"; then touch -c "$_hf" 2>/dev/null || true; else rm -f "$_hf" 2>/dev/null || true; fi
    done || true
# A wrapper's version record is written once and never refreshed, and a live
# session can be days old: of the 2-day-old ones, only those whose pid file is
# gone are pruned.
find /tmp/ -maxdepth 1 -name "${WATCHER_PREFIX}-watcher-*.version" -mtime +2 2>/dev/null \
    | while IFS= read -r _vf; do
        [ -e "${_vf%.version}.pid" ] || rm -f "$_vf"
    done || true

SESSION_ID=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null || echo "")
TRANSCRIPT_PATH=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys; v=json.load(sys.stdin).get("transcript_path"); print(v if isinstance(v,str) else "")' 2>/dev/null || echo "")
CWD=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys,os; print(json.load(sys.stdin).get("cwd") or os.getcwd())' 2>/dev/null || echo "")

if [ -z "$SESSION_ID" ] || { [ "$SOURCE" != "codex" ] && [ -z "$TRANSCRIPT_PATH" ]; }; then
    printf '{"continue": true}\n'
    exit 0
fi

# Codex: record once whether this session should expect Probe's own hooks to
# run (hooks/probe-hooks-trust.sh; ensure-daemon.sh tells the researcher when
# they do not). Before every exit below: it needs no token and no capture.
if [ -n "$IS_CODEX" ] && [ -z "$REGISTRY_ENV" ] && [ -f "$PLUGIN_ROOT/hooks/probe-hooks-trust.sh" ]; then
    HOOK_EVENT=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys; v=json.load(sys.stdin).get("hook_event_name"); print(v if isinstance(v,str) else "")' 2>/dev/null || echo "")
    # shellcheck source=probe-hooks-trust.sh
    . "$PLUGIN_ROOT/hooks/probe-hooks-trust.sh" && probe_hooks_record "$SESSION_ID" "$PLUGIN_ROOT" "$HOOK_EVENT" || true
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
[ -n "$TAP_MANIFEST_DIR" ] && MANIFEST="$PLUGIN_ROOT/$TAP_MANIFEST_DIR/plugin.json"
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
# ~/.config/probe/config.json, written by the Probe wizard's sign-in; PROBE_CONFIG_PATH
# overrides for tests/dev). Surface once and no-op when none is present.
TOKEN_ENV="${PROBE_INGEST_TOKEN:-}"
[ "$SOURCE" = "codex" ] && TOKEN_ENV="${PRBE_CODEX_TAP_TOKEN:-}"
# A registry harness: hook-env already asked tap/config.py's load_token(), which
# never takes the probe CLI's token for a harness that token was not minted for.
[ -n "$REGISTRY_ENV" ] && TOKEN_ENV="$TAP_HAS_TOKEN"
if [ ! -f "$PLUGIN_DIR/.token" ] && [ -z "$TOKEN_ENV" ]; then
    if [ "$SOURCE" = "codex" ] || [ -n "$REGISTRY_ENV" ]; then
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
        echo "[$(date -u +%FT%TZ)] probe-research-tap: no token configured; run 'python3 -m tap pair <token>' (or run the Probe wizard: probe wizard or npx probe-research); skipping" >>"$LOG_FILE"
        printf '{"continue": true}\n'
        exit 0
    fi
fi

# The interpreter the tap runs on: 3.11 or newer, which the first `python3` on
# PATH need not be (Apple's is 3.9). hooks/tap-python.sh says where it looks.
TAP_HOOKS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY=""
if [ -f "$TAP_HOOKS/tap-python.sh" ]; then
    # shellcheck source=tap-python.sh
    . "$TAP_HOOKS/tap-python.sh"
    PY=$(tap_python "$PLUGIN_ROOT") || PY=""
    MISSING_FILE="$PLUGIN_DIR/$TAP_PYTHON_MISSING"
    if [ -z "$PY" ]; then
        # Said to the researcher once a session (this hook runs again from
        # ensure-daemon.sh while the session has no recorder), and left for
        # `probe doctor`. Nothing else here can say it: the tap is the part
        # that talks to the server, and it cannot start.
        SEEN=$(tap_python_seen)
        TOLD=""
        grep -q "no Python 3.11 or newer" "$LOG_FILE" 2>/dev/null && TOLD=1
        echo "[$(date -u +%FT%TZ)] tap: no Python 3.11 or newer found ($SEEN); not recording this session" >>"$LOG_FILE"
        printf '%s\n' "$SEEN" >"$MISSING_FILE" 2>/dev/null || true
        if [ -z "$TOLD" ]; then
            # SEEN is one of tap_python_seen's fixed phrases (digits and dots at
            # most), so it needs no JSON escaping.
            printf '{"continue": true, "systemMessage": "Probe is not recording this session: session capture needs Python 3.11 or newer, and this machine has %s. Install Python 3.11 or newer, then start a new session (probe doctor shows what it found)."}\n' "$SEEN"
        else
            printf '{"continue": true}\n'
        fi
        exit 0
    fi
    rm -f "$MISSING_FILE" 2>/dev/null || true
    # For session-end.sh's `tap finish` (tap_python_cached): replaced whole, so
    # a SessionEnd reading it at that moment sees the old answer or the new one.
    CACHED_PY=$(tap_python_cached "$PLUGIN_DIR" || true)
    if [ "$CACHED_PY" != "$PY" ]; then
        printf '%s\n' "$PY" >"$PLUGIN_DIR/$TAP_PYTHON_CACHE.$$" 2>/dev/null \
            && mv -f "$PLUGIN_DIR/$TAP_PYTHON_CACHE.$$" "$PLUGIN_DIR/$TAP_PYTHON_CACHE" 2>/dev/null \
            || rm -f "$PLUGIN_DIR/$TAP_PYTHON_CACHE.$$" 2>/dev/null || true
    fi
else
    # No tap-python.sh beside this script (a plugin folder caught mid-update):
    # the choice this hook made before it.
    PY="$PLUGIN_ROOT/.venv/bin/python3"
    [ -x "$PY" ] || PY="$(command -v python3 || true)"
fi
if [ -z "$PY" ] || [ ! -x "$PY" ]; then
    echo "[$(date -u +%FT%TZ)] no python3 found, daemon disabled" >>"$LOG_FILE"
    printf '{"continue": true}\n'
    exit 0
fi

# Record which agent process owns this session: this hook runs below it, so
# tap/owner.py walks up from our own pid to the first `claude`/`codex`. The
# daemon finalizes when that exact process is gone, and never on silence.
# BEFORE the already-running check on purpose: a session resumed in a new agent
# process must hand the surviving daemon its new owner, or the old process's
# exit would end a live session. Fail-open: no record means "owner unknown".
PROBE_TAP_SOURCE="$SOURCE" PYTHONPATH="$PLUGIN_ROOT" \
    "$PY" -m tap owner --session-id "$SESSION_ID" --from-pid "$$" </dev/null >/dev/null 2>&1 || true

PID_FILE="/tmp/${WATCHER_PREFIX}-watcher-${SESSION_ID}.pid"
# The tap version the live wrapper was started from, written beside its pid
# file by whatever spawned it (this hook below, `tap start`). Taps before 0.9.16
# wrote none; their wrapper's command line names its versioned plugin folder.
VERSION_FILE="/tmp/${WATCHER_PREFIX}-watcher-${SESSION_ID}.version"

#: The first tap on protocol 2. A protocol-1 daemon's session cannot be taken
#: over: the new daemon would refuse it every tick ("legacy transcript coverage
#: is unverified"), so one older than this is left to finish on its own.
PROTOCOL2_TAP="0.4.5"

# Exit 0 when tap version $1 is older than $2 (x.y.z). Never "older" when
# either is unknown or not plain digits, so nothing is replaced on a guess.
tap_older() {
    local live="$1" ours="$2" a b i
    [ -n "$ours" ] || return 1
    [ -n "$live" ] || return 1
    for i in 1 2 3; do
        a="${live%%.*}"
        b="${ours%%.*}"
        case "$a" in '' | *[!0-9]*) return 1 ;; esac
        case "$b" in '' | *[!0-9]*) return 1 ;; esac
        [ "$a" -lt "$b" ] && return 0
        [ "$a" -gt "$b" ] && return 1
        if [ "$live" = "${live#*.}" ]; then live=0; else live="${live#*.}"; fi
        if [ "$ours" = "${ours#*.}" ]; then ours=0; else ours="${ours#*.}"; fi
    done
    return 1
}

# A watcher file of this user's: a regular file, not a link, owned by us. /tmp
# is shared, so a file anyone else put there names nothing of ours, and a link
# to a tty or a FIFO would block a read.
own_file() { [ -f "$1" ] && [ ! -L "$1" ] && [ -O "$1" ]; }

# Remove the session's pid file when it names no wrapper of this session: its
# wrapper is gone (it gave up after 5 restarts, was killed, or the machine slept
# through it), or its pid now runs something else (pids are reused). Left, it
# says "a wrapper runs this session" to everything that reads it after this hook
# hosts the session or starts a new wrapper, and session-end.sh would act on it.
# Kept: an empty one (a wrapper writing its pid: it truncates, then writes), and
# a live pid whose command line cannot be read (no proof either way). A link or
# a file that is not a regular one is never read, and goes.
drop_dead_pid_file() {
    local old
    if [ -L "$PID_FILE" ] || { [ -e "$PID_FILE" ] && [ ! -f "$PID_FILE" ]; }; then
        rm -f "$PID_FILE" 2>/dev/null || true
        return 0
    fi
    own_file "$PID_FILE" || return 0
    old=$(head -c 32 "$PID_FILE" 2>/dev/null | tr -d '[:space:]' || true)
    case "$old" in
        '') return 0 ;;
        *[!0-9]*) rm -f "$PID_FILE" 2>/dev/null || true; return 0 ;;
    esac
    if [ "$old" -le 1 ] 2>/dev/null || ! kill -0 "$old" 2>/dev/null; then
        rm -f "$PID_FILE" 2>/dev/null || true
        return 0
    fi
    case "$(cmdline_of "$old")" in
        '' | *"tap watch"*"$SESSION_ID"*) ;;
        *) rm -f "$PID_FILE" 2>/dev/null || true ;;
    esac
}
drop_dead_pid_file

# If a daemon is already running for this session_id (e.g. resumed session),
# don't spawn another -- unless an OLDER tap started it.
#
# A resumed session hands its surviving daemon to the new agent process (the
# owner hand-over above), and the daemon is the code its own tap version
# loaded. So after an update, the restart that is supposed to apply it kept the
# old daemon instead: anthrogen's dev pod ran tap 0.9.3 for a session born on
# 10-04 and resumed under a newer tap on 10-06, three days after its tap had
# been updated to 0.9.14 (2026-10-07). The machine then reports that old tap,
# and nothing local says so: `.installed_version` above already reads the new
# one. So a daemon an older tap started is stopped -- its last pass and
# finalize, exactly as at SessionEnd, after which the stream continues as on
# any resume -- and this version's daemon takes the session over. Never the
# other way: an older tap resuming a session never replaces a newer daemon.
if own_file "$PID_FILE" && kill -0 "$(head -c 32 "$PID_FILE" | tr -d '[:space:]')" 2>/dev/null; then
    LIVE_VER=""
    own_file "$VERSION_FILE" && LIVE_VER=$(head -c 64 "$VERSION_FILE" 2>/dev/null | tr -d '[:space:]' || true)
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
    case "$OLD_PID" in '' | *[!0-9]*) OLD_PID="" ;; esac
    # Only a pid that is provably THIS session's tap wrapper is stopped: /tmp is
    # world-writable and pids are reused, and the check above only proves that
    # SOME process holds this pid. Its command line from `ps`, else Linux's
    # /proc (a slim container has no `ps`); neither means no proof, and it is
    # left alone.
    OLD_CMD=""
    if [ -n "$OLD_PID" ]; then
        OLD_CMD=$(cmdline_of "$OLD_PID")
    fi
    case "$OLD_CMD" in
        *"tap watch"*"$SESSION_ID"*) OURS=1 ;;
        *) OURS="" ;;
    esac
    # A wrapper from a tap before 0.9.16 recorded no version: the versioned
    # folder (Claude Code's and Codex's plugin caches) in its command line says.
    if [ -z "$LIVE_VER" ] && [[ "$OLD_CMD" =~ /(probe-research-tap|prbe-cc-tap-plugin|prbe-codex-tap-plugin)/([0-9]+\.[0-9]+\.[0-9]+)(/|[[:space:]]|$) ]]; then
        LIVE_VER="${BASH_REMATCH[2]}"
    fi
    if [ -z "$OURS" ] || ! tap_older "$LIVE_VER" "$RUNNING_VER" \
        || tap_older "$LIVE_VER" "$PROTOCOL2_TAP"; then
        printf '{"continue": true}\n'
        exit 0
    fi
    echo "[$(date -u +%FT%TZ)] tap $LIVE_VER daemon (wrapper $OLD_PID) still runs this session; replacing it with $RUNNING_VER" >>"$LOG_FILE"
    # session-end.sh's stop, without its sentinel (the new wrapper's first check
    # would exit on it): the group when the wrapper leads one (pgid from `ps`,
    # or, with no `ps`, a live group this pid leads), else the wrapper, whose
    # TERM trap forwards to the daemon. Waits up to 3s for the last pass, inside
    # this hook's 10s. A daemon still finishing after that holds the session's
    # journal lock: the new daemon cannot take it and exits 0, its wrapper
    # retries at most 5 times a minute and then gives up, and ensure-daemon.sh
    # restarts capture on the next prompt. Nothing is lost: the transcript is
    # read from the journal's offset, whichever daemon reads it.
    OLD_PGID=$(ps -o pgid= -p "$OLD_PID" 2>/dev/null | tr -d ' ' || true)
    if [ -n "$OLD_PGID" ] && [ "$OLD_PGID" = "$OLD_PID" ]; then
        kill -TERM "-$OLD_PID" 2>/dev/null || true
        for _ in $(seq 1 15); do kill -0 -- "-$OLD_PID" 2>/dev/null || break; sleep 0.2; done
    elif ! command -v ps >/dev/null 2>&1 && kill -0 -- "-$OLD_PID" 2>/dev/null; then
        kill -TERM "$OLD_PID" 2>/dev/null || true
        for _ in $(seq 1 15); do kill -0 -- "-$OLD_PID" 2>/dev/null || break; sleep 0.2; done
    else
        kill -TERM "$OLD_PID" 2>/dev/null || true
        for _ in $(seq 1 15); do kill -0 "$OLD_PID" 2>/dev/null || break; sleep 0.2; done
    fi
    rm -f "$PID_FILE"
fi

# No live wrapper for this session_id, so any leftover shutdown sentinel is
# stale (SessionEnd no longer deletes it — see session-end.sh). Clear it before
# spawning, or the wrapper's first `[ -f "$SHUTDOWN" ] && exit 0` check would
# immediately kill the fresh daemon on a resumed session.
SHUTDOWN_FILE="/tmp/${WATCHER_PREFIX}-watcher-${SESSION_ID}.shutdown"
rm -f "$SHUTDOWN_FILE"
# The last SessionEnd's hand-over goes with it: no wrapper of this session runs
# now, and session-end.sh would otherwise wait on whatever pid it names.
rm -f "/tmp/${WATCHER_PREFIX}-watcher-${SESSION_ID}.stopping" 2>/dev/null || true

if [ -n "$TRANSCRIPT_PATH" ]; then
    TRANSCRIPT_ARGS=(--transcript "$TRANSCRIPT_PATH")
else
    TRANSCRIPT_ARGS=(--transcript-dir "${PRBE_CODEX_SESSIONS_DIR:-$HOME/.codex/sessions}")
fi

# The Probe service records this session itself when it can (plan A2): one
# thread of the service instead of a wrapper and a `tap watch` per session.
# `tap record` writes the service's pid to the session's `.hosted` marker
# (ensure-daemon.sh's fast path and session-end.sh read it). Bounded at 6s,
# inside this hook's 10s; any other answer (no service, an older one, no
# owner, PROBE_TAP_HOSTED=0) starts the wrapper below, as before.
HOSTED=""
if [ "${PROBE_TAP_HOSTED:-1}" != 0 ] && PROBE_TAP_SOURCE="$SOURCE" PYTHONPATH="$PLUGIN_ROOT" \
    "$PY" -m tap record --session-id "$SESSION_ID" --cwd "$CWD" --plugin-root "$PLUGIN_ROOT" \
    "${TRANSCRIPT_ARGS[@]}" </dev/null >>"$LOG_FILE" 2>&1; then
    HOSTED=1
    rm -f "$VERSION_FILE" 2>/dev/null || true
    # A wrapper's pid file that outlived it: the service records the session
    # now, and `probe session status` and session-end.sh must not read it. One
    # a wrapper wrote since the check above (a concurrent start) is live: kept.
    drop_dead_pid_file
    echo "[$(date -u +%FT%TZ)] tap: the Probe service records this session (tap ${RUNNING_VER:-unknown})" >>"$LOG_FILE"
fi

if [ -z "$HOSTED" ]; then
rm -f "/tmp/${WATCHER_PREFIX}-watcher-${SESSION_ID}.hosted"

# Which tap the wrapper below runs, for the next resume's check above. Written
# before the spawn, so there is no moment when a live wrapper carries an older
# one's version; unknown (no manifest) leaves none, which never replaces.
# Written only over nothing or a file of this user's: never through a link.
if [ -n "$RUNNING_VER" ] && { { [ ! -e "$VERSION_FILE" ] && [ ! -L "$VERSION_FILE" ]; } || own_file "$VERSION_FILE"; }; then
    printf '%s' "$RUNNING_VER" >"$VERSION_FILE" 2>/dev/null || true
else
    rm -f "$VERSION_FILE" 2>/dev/null || true
fi

# Crash-recovery wrapper: respawn up to 5 times per minute.
# Self-terminates when shutdown sentinel exists (SessionEnd touches it), and
# when `tap watch` exits 12 (tap/handback.py EXIT_HANDED_OVER): the Probe
# service took the session over, so its pid file goes with it.
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
    CODE=0
    wait "$CHILD_PID" 2>/dev/null || CODE=$?
    CHILD_PID=""
    [ -f "$SHUTDOWN" ] && exit 0
    # 12: the Probe service records the session now (tap/handback.py): done.
    if [ "$CODE" = 12 ]; then
        [ "$(cat "$PIDF" 2>/dev/null)" = "$$" ] && rm -f "$PIDF"
        exit 0
    fi
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
if os.name == "nt":
    # No setsid there, and execv does not quote its arguments (Git Bash hands
    # `/bin/bash` over as `C:/Program Files/Git/usr/bin/bash`): a child of its
    # own process group, which the wrapper rewrites the pid file for.
    import subprocess
    subprocess.Popen(sys.argv[1:], creationflags=0x00000200)  # CREATE_NEW_PROCESS_GROUP
    os._exit(0)
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
fi  # no hosted recorder

# Tell the researcher what was redacted from their LAST session. The daemon
# has no terminal of its own — it is spawned detached below and everything it
# logs goes to a file nobody reads — so this hook's `systemMessage` is the only
# channel that actually reaches a human. A credential we quietly removed and
# never mentioned leaves the key live and the researcher unaware, which is the
# whole failure this lane exists to close.
#
# Bounded and fail-open: a 5s cap, stderr discarded, and any failure just means
# no message. Never a reason a session fails to start.
NOTICE=""
if [ -n "${PY:-}" ] && [ -x "$PY" ]; then
    NOTICE=$(cd "$PLUGIN_ROOT" 2>/dev/null && timeout 5 "$PY" -m tap redaction-notice 2>/dev/null || true)
fi
if [ -n "$NOTICE" ]; then
    printf '%s' "$NOTICE" | "$PY" -c 'import json,sys; print(json.dumps({"continue": True, "systemMessage": sys.stdin.read().strip()}))' 2>/dev/null \
        || printf '{"continue": true}\n'
else
    printf '{"continue": true}\n'
fi
