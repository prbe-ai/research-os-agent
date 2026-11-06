#!/usr/bin/env bash
# Session-stop reconcile for the team note.
#
# The agent has been editing `probe-team-note.md` all session; this sends it.
# ONE file per machine (`<state>/team-note/probe-team-note.md`), shared by every
# harness on it -- PROBE_AGENT below picks the instruction FILE to re-render, not
# the document.
# SessionStart already pushed whatever an earlier session left behind, so by the
# time this runs the only unsynced text is this session's own work.
#
# TWO MODES, because the two events mean different things.
#
# Stop fires EVERY TURN, so it stays --push-only: get this turn's work out, cheap,
# no pull. SessionEnd fires once, and there it does a full reconcile -- which also
# RE-RENDERS the managed block in CLAUDE.md / AGENTS.md.
#
# That reversed an earlier judgement, and the premise is what changed rather than
# the reasoning. This comment used to say "pulling here would refresh a file
# nobody is about to read". True while the note only reached a session through
# the hook. It stopped being true when the note moved INTO the instruction file:
# that file is read at the next session's launch, before any hook of ours runs,
# so refreshing it at session end is refreshing exactly the bytes somebody is
# about to read. Without this, an edit made in session N first reached the block
# that session N+2 read -- push at N, render at N+1's start, visible at N+2.
#
# The "races the next session's pull" worry is handled: the render takes a lock
# keyed on the instruction FILE, and reconcile is idempotent.
#
# FAIL-OPEN AND SILENT, like every hook in this plugin. A service that does not
# take the sync, a dead network, or a conflict all leave the file exactly as the
# agent left it, and the next session start reconciles it. Losing the edit would
# be the only bad outcome and nothing here can cause it.
set -u

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

# PROBE OFF MEANS NO NETWORK, and this is the loudest thing the plugin does
# without being asked: a detached push, every turn. A researcher who set `off`
# and then watched packets leave would be right to say it was not off.
#
# Resolved through the SAME module every other surface reads, never by
# re-implementing the file format here -- two answers to "is this session off"
# is how one surface ends up honouring a declaration the other ignores. Fail
# OPEN: an unreadable state syncs, because a note that silently stopped sending
# is the one failure this hook must never cause.
# THE HOOK PAYLOAD IS THE AUTHORITY, the environment only a fallback. Stop and
# SessionEnd both deliver `session_id` on stdin, and a harness that does not ALSO
# export it into the hook's environment would skip this check entirely and sync a
# session the researcher had switched off. Reading stdin is safe here: nothing
# below consumes it, and a payload that is missing, empty or unparseable just
# leaves the environment fallback in place.
_SID=""
# This runs in the session's folder, outside the agent's sandbox. `python3 -c` puts that folder first
# on Python's path (as an empty entry), where the agent can plant a `json.py`: each program below
# drops it before any import but the builtin sys (dispatch.sh's rule).
if command -v python3 >/dev/null 2>&1; then
  _SID="$(python3 -c '
import sys
sys.path[:] = [p for p in sys.path if p]
import json
try:
    payload = json.load(sys.stdin)
    value = payload.get("session_id") if isinstance(payload, dict) else None
    sys.stdout.write(value if isinstance(value, str) else "")
except Exception:
    pass
' 2>/dev/null <&0 || true)"
fi
[ -n "$_SID" ] || _SID="${CLAUDE_CODE_SESSION_ID:-${CODEX_THREAD_ID:-}}"
if [ -n "$_SID" ] && command -v python3 >/dev/null 2>&1; then
  _HOOKDIR="$(cd "$(dirname "$0")" && pwd)"
  # The hooks folder and the session id go in as arguments, never into the program's text: the id is
  # payload data, and a quote in it must stay data.
  if python3 -c '
import sys
sys.path[:] = [sys.argv[1]] + [p for p in sys.path if p]
try:
    import _session_marker as m
    sys.exit(0 if m.session_state(sys.argv[2]) == m.STATE_OFF else 1)
except Exception:
    sys.exit(1)
' "$_HOOKDIR" "$_SID" 2>/dev/null; then
    exit 0
  fi
fi

# THE PROBE SERVICE SYNCS, once per machine (`notes.sync`, service-only plan
# task 14): debounced, so every session's Stop in a burst is one push, never a
# `probe` per session per turn. This route runs in the service; it only asks.
# SessionEnd reconciles (and therefore renders); every other event pushes only.
#
# BOTH registrations state their mode explicitly, because this variable is
# INHERITED. Marking only SessionEnd would leave Stop reading whatever the
# environment happened to carry, and an ambient `sessionend` -- exported by a
# wrapper, a parent agent, or a future hook that forgot to scope it -- would put
# a network pull and an instruction-file rewrite on EVERY TURN. Defaulting to
# --push-only makes the cheap path the fallback as well, so the failure mode of
# a missing or unrecognised value is "did less", never "did more".
if [ "${PROBE_HOOK_EVENT:-}" = "sessionend" ]; then
  _MODE=reconcile
else
  _MODE=push
fi

_HOOKDIR="${_HOOKDIR:-$(cd "$(dirname "$0")" && pwd)}"
if [ -f "$_HOOKDIR/_service_call.py" ] && command -v python3 >/dev/null 2>&1; then
  python3 -I "$_HOOKDIR/_service_call.py" notes-sync "$_MODE" </dev/null >/dev/null 2>&1
fi
exit 0
