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

# First of all, before any program starts: leave the session's folder, which the agent can write
# (hooks/session-folder.sh). Sets SESSION_DIR and TAP_HOOKS_DIR; this runs in the tap's own folder.
[ -f "${BASH_SOURCE[0]%/*}/session-folder.sh" ] && . "${BASH_SOURCE[0]%/*}/session-folder.sh" \
    && tap_leave_session_folder "${BASH_SOURCE[0]}" || exit 0

# An open session runs the INSTALLED tap's copy of this script when that is
# another one: a tap fix reaches it at its next event (hooks/installed-tap.sh).
# Then, before stdin is read. That copy starts here too, so it is told the session's folder.
if [ -f "$TAP_HOOKS_DIR/installed-tap.sh" ] && . "$TAP_HOOKS_DIR/installed-tap.sh"; then
    export TAP_SESSION_DIR="$SESSION_DIR"
    tap_run_installed "${BASH_SOURCE[0]}" "$@"
    unset TAP_SESSION_DIR
    tap_yields_to_shim "${BASH_SOURCE[0]}" && exit 0
fi

set -euo pipefail

PLUGIN_ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-$(cd "$TAP_HOOKS_DIR/.." && pwd)}}"
SOURCE="${PROBE_TAP_SOURCE:-claude_code}"
# The watcher folder (`tap/config.py` `_watcher_dir`): PROBE_TAP_WATCHER_DIR when
# it names an absolute folder, else /tmp.
WATCHER_DIR=/tmp
case "${PROBE_TAP_WATCHER_DIR:-}" in /*) WATCHER_DIR="$PROBE_TAP_WATCHER_DIR" ;; esac
HOOK_INPUT="$(cat)"
REGISTRY_ENV=""
case "$SOURCE" in
    claude_code | codex) ;;
    *)
        TAP_ROOT="$(cd "$TAP_HOOKS_DIR/.." && pwd)"
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
find "$WATCHER_DIR/" -maxdepth 1 \( -name "${WATCHER_PREFIX}-watcher-*.shutdown" \
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
    local pid
    # Not a regular file (a link to a tty, a FIFO): never read, never ours.
    { [ -f "$1" ] && [ ! -L "$1" ]; } || return 1
    [ -O "$1" ] || return 0
    pid=$(head -c 32 "$1" 2>/dev/null | tr -d '[:space:]' || true)
    case "$pid" in '' | *[!0-9]*) return 1 ;; esac
    [ "$pid" -gt 1 ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1
    case "$(cmdline_of "$pid")" in *probe.service*) return 0 ;; esac
    return 1
}
# Kept ones are touched, so each is looked at again in 2 days, not every start.
find "$WATCHER_DIR/" -maxdepth 1 -name "${WATCHER_PREFIX}-watcher-*.hosted" -mtime +2 2>/dev/null \
    | while IFS= read -r _hf; do
        if keep_hosted "$_hf"; then touch -c "$_hf" 2>/dev/null || true; else rm -f "$_hf" 2>/dev/null || true; fi
    done || true
# A wrapper's version record is written once and never refreshed, and a live
# session can be days old: of the 2-day-old ones, only those whose pid file is
# gone are pruned.
find "$WATCHER_DIR/" -maxdepth 1 -name "${WATCHER_PREFIX}-watcher-*.version" -mtime +2 2>/dev/null \
    | while IFS= read -r _vf; do
        [ -e "${_vf%.version}.pid" ] || rm -f "$_vf"
    done || true

SESSION_ID=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("session_id",""))' 2>/dev/null || echo "")
# The payload's transcript and folder, a relative one read against the session's folder (an argument:
# this hook runs in the tap's folder); a payload naming no folder: the session's.
TRANSCRIPT_PATH=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,os,sys; v=json.load(sys.stdin).get("transcript_path"); print(os.path.normpath(os.path.join(sys.argv[1],v)) if isinstance(v,str) and v else "")' "$SESSION_DIR" 2>/dev/null || echo "")
CWD=$(printf '%s' "$HOOK_INPUT" | python3 -c 'import json,os,sys; v=json.load(sys.stdin).get("cwd"); print(os.path.normpath(os.path.join(sys.argv[1],v)) if isinstance(v,str) and v else sys.argv[1])' "$SESSION_DIR" 2>/dev/null || echo "")

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
        echo "[$(date -u +%FT%TZ)] probe-research-tap: no token configured; run 'python3 -m tap pair <token>' (or "'run `npx -y probe-research sign-in --json`'"); skipping" >>"$LOG_FILE"
        printf '{"continue": true}\n'
        exit 0
    fi
fi

# The interpreter the tap runs on: 3.11 or newer, which the first `python3` on
# PATH need not be (Apple's is 3.9). hooks/tap-python.sh says where it looks.
TAP_HOOKS="$(cd "$TAP_HOOKS_DIR" && pwd)"
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
# Run by the Probe service for a frozen hook shim (`hooks.run {routes: "service"}`),
# this hook sits below the service, not the agent: the walk starts at the shim,
# the caller the service names (PROBE_HOOK_PARENT_PID, set by the service only).
OWNER_FROM="$$"
case "${PROBE_HOOK_PARENT_PID:-}" in '' | *[!0-9]*) ;; *) OWNER_FROM="$PROBE_HOOK_PARENT_PID" ;; esac
PROBE_TAP_SOURCE="$SOURCE" PYTHONPATH="$PLUGIN_ROOT" \
    "$PY" -m tap owner --session-id "$SESSION_ID" --from-pid "$OWNER_FROM" </dev/null >/dev/null 2>&1 || true

# A session's stale shutdown sentinel and hand-over (an older tap's wrapper left them, or the
# last SessionEnd): its recorder starts below, and must not read them as a stop.
rm -f "${WATCHER_DIR}/${WATCHER_PREFIX}-watcher-${SESSION_ID}.shutdown" \
    "${WATCHER_DIR}/${WATCHER_PREFIX}-watcher-${SESSION_ID}.stopping" 2>/dev/null || true

if [ -n "$TRANSCRIPT_PATH" ]; then
    TRANSCRIPT_ARGS=(--transcript "$TRANSCRIPT_PATH")
else
    TRANSCRIPT_ARGS=(--transcript-dir "${PRBE_CODEX_SESSIONS_DIR:-$HOME/.codex/sessions}")
fi

# THE PROBE SERVICE RECORDS THIS SESSION, one thread of it (plan A2): `tap record`
# starts the service when none answers and writes its pid to the session's `.hosted`
# marker (ensure-daemon.sh's fast path and session-end.sh read it). Bounded at 6s,
# inside this hook's 10s; the prompt's heal (ensure-daemon.sh, a 5s hook) asks for
# less (TAP_RECORD_WITHIN_S). A service that does not take it leaves the session for
# the next prompt's heal: nothing records here (service-only task 15).
if PROBE_TAP_SOURCE="$SOURCE" PYTHONPATH="$PLUGIN_ROOT" \
    "$PY" -m tap record --session-id "$SESSION_ID" --cwd "$CWD" --plugin-root "$PLUGIN_ROOT" --within "${TAP_RECORD_WITHIN_S:-6}" \
    "${TRANSCRIPT_ARGS[@]}" </dev/null >>"$LOG_FILE" 2>&1; then
    echo "[$(date -u +%FT%TZ)] tap: the Probe service records this session (tap ${RUNNING_VER:-unknown})" >>"$LOG_FILE"
else
    rm -f "${WATCHER_DIR}/${WATCHER_PREFIX}-watcher-${SESSION_ID}.hosted" 2>/dev/null || true
    echo "[$(date -u +%FT%TZ)] tap: the Probe service did not take this session; the next prompt asks again" >>"$LOG_FILE"
fi

# Tell the researcher what was redacted from their LAST session. The daemon
# has no terminal of its own — it runs in the Probe service and everything it
# logs goes to a file nobody reads — so this hook's `systemMessage` is the only
# channel that actually reaches a human. A credential we quietly removed and
# never mentioned leaves the key live and the researcher unaware, which is the
# whole failure this lane exists to close.
#
# Bounded and fail-open: a 5s cap, stderr discarded, and any failure just means
# no message. Never a reason a session fails to start.
NOTICE=""
# Not from the prompt's heal (TAP_HEAL): its 5s hook has no room, and the notice is the start's.
if [ -z "${TAP_HEAL:-}" ] && [ -n "${PY:-}" ] && [ -x "$PY" ]; then
    NOTICE=$(cd "$PLUGIN_ROOT" 2>/dev/null && timeout 5 "$PY" -m tap redaction-notice 2>/dev/null || true)
fi
if [ -n "$NOTICE" ]; then
    printf '%s' "$NOTICE" | "$PY" -c 'import json,sys; print(json.dumps({"continue": True, "systemMessage": sys.stdin.read().strip()}))' 2>/dev/null \
        || printf '{"continue": true}\n'
else
    printf '{"continue": true}\n'
fi
