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
command -v python3 >/dev/null 2>&1 || exit 0
# Imported, not run as a script, so python caches its bytecode. The hooks
# folder replaces the empty first sys.path entry (the session's working
# directory) `-c` puts there BEFORE anything but the builtin sys is imported:
# a project's own os.py must never run in place of the stdlib's.
# A dispatch.py that will not import (unreadable, half-written mid-update) exits
# 0: python's own exit for a script it cannot open is 2, a veto.
exec python3 -S -c '
import sys
sys.path[0] = sys.argv[1]
try:
    import dispatch
except BaseException:
    sys.exit(0)
dispatch.run(sys.argv[2:])
' "$HOOKS" "${1:-}"
