#!/bin/bash
# The one script every hooks.json entry runs: `dispatch.sh <lane>`.
#
# hooks.json's entries are frozen (Codex trusts each by a hash of the whole
# entry and silently skips a changed one until the researcher re-approves it in
# /hooks), so everything that may change lives behind them: this file,
# dispatch.py, and routes.json (which hooks run, per event and tool). The
# entries carry no tool matcher on purpose: which tools are guarded is
# routes.json's to decide, so a new tool can be guarded in any release.
#
# Fail-open: nothing here exits non-zero (a veto on PreToolUse and
# UserPromptSubmit), not even with dispatch.py missing mid-install. dispatch.py
# runs under `python3 -S` (stdlib only, ~10ms less start-up on every event) and
# decides from the payload's bytes, before parsing it, whether any route can
# apply to this tool call.

HOOKS="${BASH_SOURCE[0]%/*}"
[ "$HOOKS" != "${BASH_SOURCE[0]}" ] || HOOKS="$PWD"
[ -f "$HOOKS/dispatch.py" ] || exit 0
# Keep only the absolute entries of PATH and PYTHONPATH: an empty or relative one names the folder
# this runs in, the session's, which the agent can write (a planted `python3`, or a `json.py` Python
# imports at start-up). Builtins only on Linux and macOS (hooks/session-folder.sh's rule, the tap's).
windows=""
is_windows() {  # real Windows: OSTYPE (which can be inherited) only says when to ask `uname`
    if [ -z "$windows" ]; then
        windows=no
        case "${OSTYPE:-}" in
            msys* | cygwin*) case "$(/usr/bin/uname -s 2>/dev/null)" in MINGW* | MSYS* | CYGWIN*) windows=yes ;; esac ;;
        esac
    fi
    [ "$windows" = yes ]
}
drop_relative_entries() {
    local separator=":" rest entry kept=""
    rest="${!1-}"
    if [ "$1" = PYTHONPATH ] && [ -n "$rest" ] && is_windows; then separator=";"; fi
    [ -z "$rest" ] || rest="$rest$separator"
    while [ -n "$rest" ]; do
        entry="${rest%%"$separator"*}"
        rest="${rest#*"$separator"}"
        case "$entry" in
            /*) kept="${kept:+$kept$separator}$entry" ;;
            [A-Za-z]:[\\/]* | \\\\*) ! is_windows || kept="${kept:+$kept$separator}$entry" ;;
        esac
    done
    if [ -n "$kept" ]; then
        export "$1=$kept"
    elif [ "$1" = PATH ]; then
        export PATH=/usr/bin:/bin  # never empty or unset: bash then searches the current folder
    else
        unset "$1"
    fi
}
drop_relative_entries PATH
drop_relative_entries PYTHONPATH
command -v python3 >/dev/null 2>&1 || exit 0
# Imported, not run as a script, so python caches its bytecode. The hooks
# folder replaces the empty first sys.path entry (the session's working
# directory) `-c` puts there BEFORE anything but the builtin sys is imported:
# a project's own os.py must never run in place of the stdlib's.
# A dispatch.py that will not import (unreadable, half-written mid-update) exits
# 0: python's own exit for a script it cannot open is 2, a veto.
# Every argument goes on: hooks.json passes the lane alone; the Probe service
# adds `--record-keeping` when it runs a queued event's routes (dispatch.py, D36).
exec python3 -S -c '
import sys
sys.path[0] = sys.argv[1]
try:
    import dispatch
except BaseException:
    sys.exit(0)
dispatch.run(sys.argv[2:])
' "$HOOKS" "$@"
