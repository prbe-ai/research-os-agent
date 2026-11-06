# Sourced FIRST by every tap hook script: leave the session's folder before anything else runs.
#
# Claude Code, Codex and Kimi Code run these hooks outside the agent's sandbox, in the session's
# folder, which the agent can write. Whatever a hook starts there can load the agent's files:
# `python3 -c`, `python3 -` and `python3 -m tap` put that folder first on Python's path, and an
# empty or relative PATH or PYTHONPATH entry names it too. So, before any program starts:
#
#   - PATH and PYTHONPATH keep their absolute entries only;
#   - each variable of TAP_PATH_VARIABLES (one path each, read by these hooks or the tap) that
#     names its path relatively is made absolute against the session's folder, so it means what
#     it meant there; no other variable is touched;
#   - the hook moves to the tap's own folder (`cd`: a builtin, so no process and no time added).
#
# The session's folder stays data: SESSION_DIR, which a payload naming its folder or transcript
# relatively is read against. A script this one starts (the installed copy, ensure-daemon.sh's
# session-start.sh) gets it as TAP_SESSION_DIR, read and cleared here. Bash 3.2 (macOS /bin/bash).

#: The variables naming one path that these hooks or the tap read (agent/tests/test_service_hooks_actions.py
#: holds the service's copy, `capture.TAP_PATH_VARIABLES`, equal). Not PROBE_TAP_WATCHER_DIR: a
#: relative one is ignored by design (/tmp).
TAP_PATH_VARIABLES="HOME TMPDIR XDG_CONFIG_HOME XDG_STATE_HOME XDG_DATA_HOME XDG_CACHE_HOME XDG_RUNTIME_DIR
PROBE_CONFIG_PATH PROBE_SERVICE_ROOT PROBE_BOX_DIR PROBE_TRANSCRIPT_STATE_DIR PROBE_RESEARCH_TAP_PLUGIN_DIR
PROBE_RESEARCH_TAP_PROJECTS_DIR PRBE_CODEX_TAP_PLUGIN_DIR PRBE_CODEX_SESSIONS_DIR PROBE_KIMI_TAP_PLUGIN_DIR
PROBE_KIMI_SESSIONS_DIR PROBE_PI_TAP_PLUGIN_DIR PROBE_CURSOR_TAP_PLUGIN_DIR PROBE_CURSOR_TRANSCRIPTS_DIR
PROBE_CURSOR_HOME CLAUDE_CONFIG_DIR CLAUDE_CODE_PLUGIN_CACHE_DIR CLAUDE_PLUGIN_ROOT CODEX_HOME PLUGIN_ROOT
KIMI_CODE_HOME KIMI_PLUGIN_ROOT CURSOR_PLUGIN_ROOT PI_CODING_AGENT_DIR SSL_CERT_FILE SSL_CERT_DIR
UV_CACHE_DIR UV_TOOL_DIR UV_TOOL_BIN_DIR PIPX_HOME APPDATA LOCALAPPDATA"
#: `tap_is_windows`'s answer, asked once ("" until then; never an inherited value).
TAP_WINDOWS=""

# 0 on real Windows (Git Bash, MSYS2, Cygwin). OSTYPE can be inherited from another system's
# shell, so it only says when to ask `uname`: never on Linux or macOS.
tap_is_windows() {
    if [ -z "$TAP_WINDOWS" ]; then
        TAP_WINDOWS=no
        case "${OSTYPE:-}" in
            msys* | cygwin*)
                case "$(/usr/bin/uname -s 2>/dev/null)" in MINGW* | MSYS* | CYGWIN*) TAP_WINDOWS=yes ;; esac ;;
        esac
    fi
    [ "$TAP_WINDOWS" = yes ]
}

# 0 when "$1" is an absolute path: `/...`, and only on real Windows `C:\...`, `C:/...`, `\\host\...`.
tap_is_absolute() {
    case "$1" in
        /*) return 0 ;;
        [A-Za-z]:[\\/]* | \\\\*) tap_is_windows ;;
        *) return 1 ;;
    esac
}

# Keep only the absolute entries of the list variable named $1 (PATH or PYTHONPATH).
tap_drop_relative_entries() {
    local separator=":" rest entry kept=""
    rest="${!1-}"
    # Windows' own python reads PYTHONPATH as `C:\a;D:\b`.
    if [ "$1" = PYTHONPATH ] && [ -n "$rest" ] && tap_is_windows; then separator=";"; fi
    [ -z "$rest" ] || rest="$rest$separator"
    while [ -n "$rest" ]; do
        entry="${rest%%"$separator"*}"
        rest="${rest#*"$separator"}"
        if tap_is_absolute "$entry"; then kept="${kept:+$kept$separator}$entry"; fi
    done
    if [ -n "$kept" ]; then
        export "$1=$kept"
    elif [ "$1" = PATH ]; then
        export PATH=/usr/bin:/bin  # never empty or unset: bash then searches the current folder
    else
        unset "$1"
    fi
}

# Make each set TAP_PATH_VARIABLES path that is relative absolute against SESSION_DIR: the whole
# value is one path (a name may hold `:` or `;`); a `~` path is its reader's to expand.
tap_make_relative_paths_absolute() {
    local name value
    for name in $TAP_PATH_VARIABLES; do
        value="${!name-}"
        case "$value" in '' | \~*) continue ;; esac
        tap_is_absolute "$value" && continue
        { printf -v "$name" '%s/%s' "$SESSION_DIR" "$value"; } 2>/dev/null || true
    done
}

# $1: the hook script's own path. Sets SESSION_DIR and TAP_HOOKS_DIR, then leaves for the tap's folder.
tap_leave_session_folder() {
    # A relative or empty TAP_SESSION_DIR is ignored: never joined to anything.
    case "${TAP_SESSION_DIR:-}" in /*) SESSION_DIR="$TAP_SESSION_DIR" ;; *) SESSION_DIR="$PWD" ;; esac
    unset TAP_SESSION_DIR
    tap_drop_relative_entries PATH
    tap_drop_relative_entries PYTHONPATH
    tap_make_relative_paths_absolute
    case "$1" in
        /*) TAP_HOOKS_DIR="${1%/*}" ;;
        *) TAP_HOOKS_DIR="$PWD/${1%/*}" ;;
    esac
    cd "$TAP_HOOKS_DIR/.." 2>/dev/null || cd / || return 1
}
:
