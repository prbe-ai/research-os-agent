#!/usr/bin/env bash
# Session-stop reconcile for the team note.
#
# The agent has been editing `probe-team-note.md` all session; this sends it.
# SessionStart already pushed whatever an earlier session left behind, so by the
# time this runs the only unsynced text is this session's own work.
#
# --push-only, deliberately: pulling here would refresh a file nobody is about to
# read, and would race the next session's own pull. Stop is for getting work OUT.
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

# Bounded: a stop hook that hangs holds up the session's exit. The reconcile is
# one HTTP call; anything slower than this is a network problem, and the file
# survives to be sent next time.
timeout 12 "$PROBE_BIN" notes sync --push-only >/dev/null 2>&1 || true
exit 0
