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
# FAIL-OPEN AND SILENT, like every hook in this plugin. A missing CLI, a dead
# network, or a conflict all leave the file exactly as the agent left it, and the
# next session start reconciles it. Losing the edit would be the only bad outcome
# and nothing here can cause it.
set -u

PROBE_BIN="$(command -v probe 2>/dev/null || true)"
for _c in \
  "$HOME/.local/bin/probe" \
  "$HOME/.local/share/uv/tools/probe-research/bin/probe"
do
  [ -n "$PROBE_BIN" ] && break
  [ -x "$_c" ] && PROBE_BIN="$_c"
done
[ -n "$PROBE_BIN" ] || exit 0

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
if command -v python3 >/dev/null 2>&1; then
  _SID="$(python3 -c '
import json, sys
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
  if python3 -c "
import sys
sys.path.insert(0, '$_HOOKDIR')
try:
    import _session_marker as m
    sys.exit(0 if m.session_state('$_SID') == m.STATE_OFF else 1)
except Exception:
    sys.exit(1)
" 2>/dev/null; then
    exit 0
  fi
fi

# DETACHED, because the two harnesses give this hook wildly different budgets.
# Claude Code fires it on Stop with room to spare; Codex caps SessionEnd at 3
# seconds, which is not a round trip. Spawning and returning means the cap bounds
# the SPAWN and the sync finishes on its own. `setsid` so it survives the
# session's process group going away, which is the whole point of running at
# session end.
#
# CORRECTION. An earlier version of this comment said Codex has no Stop event.
# It does: a live Codex session prints `hook: Stop`, its hooks.json carries a
# Stop group, and config.toml records trust per Stop hook. The observation that
# prompted the claim was real but had a different cause -- Codex skips a hook
# whose hash it has not been asked to trust, and this file's Stop entry is new,
# so it is silently absent until the researcher approves it. Registering on
# SessionEnd as well is still right (it is a second chance on a different
# trust key), but not for the reason first given.
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
  set -- notes sync
else
  set -- notes sync --push-only
fi

if command -v setsid >/dev/null 2>&1; then
  setsid "$PROBE_BIN" "$@" >/dev/null 2>&1 &
else
  "$PROBE_BIN" "$@" >/dev/null 2>&1 &
fi
exit 0
