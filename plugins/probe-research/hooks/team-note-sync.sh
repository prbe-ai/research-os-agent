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
if command -v setsid >/dev/null 2>&1; then
  setsid "$PROBE_BIN" notes sync --push-only >/dev/null 2>&1 &
else
  "$PROBE_BIN" notes sync --push-only >/dev/null 2>&1 &
fi
exit 0
