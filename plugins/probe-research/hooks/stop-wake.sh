#!/bin/bash
# Claude Code's Stop wake for one kind of message ($1: alert, answer or message): claude-reads.json
# runs it once per kind, each with its own chat label, and reads_hook.py waits for that kind only.
# Exits before Python unless that kind can come for this session.
[ "${CLAUDE_CODE_SESSION_ATTENDED:-}" = 1 ] || exit 0
SID="${CLAUDE_CODE_SESSION_ID:-${CODEX_THREAD_ID:-}}"; SID="${SID//[^A-Za-z0-9_-]/}"; [ -n "$SID" ] || exit 0
shopt -s nullglob
R="${XDG_STATE_HOME:-$HOME/.local/state}/probe/reads"
case "${1:-}" in
  # A session the daemon serves: its worker's run alerts.
  alert) [ -f "$R/status/$SID.json" ] || exit 0 ;;
  # An open ask or writer request (taken by the worker or not): its end.
  answer) W=("$R/asks/$SID"/a*.json "$R/asks/$SID"/.taken/a*.json "$R/requests/$SID"/w*.json "$R/requests/$SID"/.taken/w*.json)
          [ ${#W[@]} -gt 0 ] || exit 0 ;;
  # A waiting server message, a served session, or an open ask or writer request (a message landing
  # during that wait wakes the idle agent): its inbox messages.
  message) W=("$R/messages/$SID"/00000000000000000000-n*.json)
           A=("$R/asks/$SID"/a*.json "$R/asks/$SID"/.taken/a*.json "$R/requests/$SID"/w*.json "$R/requests/$SID"/.taken/w*.json)
           [ ${#W[@]} -gt 0 ] || [ ${#A[@]} -gt 0 ] || [ -f "$R/status/$SID.json" ] || exit 0 ;;
  *) exit 0 ;;
esac
export PROBE_READS_GROUP="$1"
ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-}}"; ROOT="${ROOT%/}"
if [ ! -f "$ROOT/hooks/reads_hook.py" ] && [ -n "${PLUGIN_ROOT:-}" ]; then
  ROOT="$(ls -dt "${ROOT%/*}"/[0-9]*.[0-9]*/.codex-plugin/plugin.json 2>/dev/null | head -1)"
  ROOT="${ROOT%/.codex-plugin/plugin.json}"; [ -n "$ROOT" ] && export PLUGIN_ROOT="$ROOT"
fi
[ -f "$ROOT/hooks/reads_hook.py" ] || exit 0
[ -n "${PLUGIN_ROOT:-}" ] && export PROBE_AGENT=codex
PY="$(command -v python3)" || exit 0
exec "$PY" "$ROOT/hooks/reads_hook.py"
