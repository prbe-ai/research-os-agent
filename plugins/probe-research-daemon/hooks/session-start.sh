#!/usr/bin/env bash
# Probe Research SessionStart hook: nudge when the installed CLI/plugin is out
# of date. SYNCHRONOUS and FAIL-OPEN by contract — the version check must finish
# before this returns its JSON (a SessionStart systemMessage can't come from a
# detached process), and ANY failure degrades to `{"continue": true}` so a
# broken check never blocks a session. Network is throttled by a cache file to
# once per version_policy.TTL (15m; it was 24h once and that bought a whole day
# of blindness across a burst of releases), so most session starts do zero
# network yet the nudge still renders every session from the cached manifest.
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

# Claude Code sends {session_id, transcript_path, cwd, source} on stdin. The
# telemetry observer wants session_id and source; capture it (draining the pipe
# either way so nothing blocks on a full one).
HOOK_INPUT="$(cat 2>/dev/null || true)"

PLUGIN_ROOT="${PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-${KIMI_PLUGIN_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}}}"

# python3 required (same dependency the tap hook and mcp helper already assume);
# degrade silently if absent rather than erroring.
PY="$(command -v python3 2>/dev/null || true)"
if [ -z "$PY" ]; then
  printf '{"continue": true}\n'
  exit 0
fi

# SessionStart's stdin carries `source` (startup|resume|clear|compact) and
# `session_id`. version_check.py turns source=compact into a reconcile-Probe
# nudge on the additionalContext channel -- the one surface that reaches the
# model right after a compaction -- and needs the session id alongside it: a
# session the researcher untracked (`probe session untrack`) gets the off
# contract restated there instead of a nudge to do Probe work. The initial cwd
# seeds the folder-aware tracking default. All three fields come from ONE JSON
# parse and travel over NUL-delimited records: unlike tabs/newlines, NUL cannot
# occur in a filesystem path, and no shell text is ever evaluated. Skipped
# under PreCompact (its stdin has no `source` and the event can carry no context
# anyway). Fail-open: unparseable payload = empty fields, and an absent id reads
# as tracking on -- today's behaviour.
PROBE_SESSION_SOURCE=""
PROBE_SESSION_ID=""
PROBE_SESSION_CWD=""
if [ "${PROBE_HOOK_EVENT:-}" != "precompact" ]; then
  # This runs in the session's folder, outside the agent's sandbox, and its child scripts keep that
  # folder. `-c` puts it first on Python's path (as an empty entry), where the agent can plant a
  # `json.py`: dropped before any import but the builtin sys (dispatch.sh's rule).
  exec 3< <(printf '%s' "$HOOK_INPUT" | "$PY" -c 'import sys
sys.path[:] = [p for p in sys.path if p]
import json
try:
    d = json.load(sys.stdin)
except Exception:
    d = {}
def field(value):
    return value if isinstance(value, str) else ""
fields = (d.get("source"), d.get("session_id"), d.get("cwd")) if isinstance(d, dict) else ("", "", "")
for value in fields:
    sys.stdout.buffer.write(field(value).encode("utf-8", "surrogatepass") + b"\0")' 2>/dev/null)
  IFS= read -r -d '' PROBE_SESSION_SOURCE <&3 || PROBE_SESSION_SOURCE=""
  IFS= read -r -d '' PROBE_SESSION_ID <&3 || PROBE_SESSION_ID=""
  IFS= read -r -d '' PROBE_SESSION_CWD <&3 || PROBE_SESSION_CWD=""
  exec 3<&-
fi
export PROBE_SESSION_SOURCE PROBE_SESSION_ID PROBE_SESSION_CWD

# Resolve the `probe` binary without trusting PATH — a dock-launched Claude Code
# sources no profile, so ~/.local/bin may be absent. Mirrors bin/probe-mcp-headers.
PROBE_BIN="$(command -v probe 2>/dev/null || true)"
for _c in \
  "$HOME/.local/bin/probe" \
  "$HOME/.local/share/uv/tools/probe-research/bin/probe"
do
  [ -n "$PROBE_BIN" ] && break
  [ -x "$_c" ] && PROBE_BIN="$_c"
done

export PROBE_BIN="${PROBE_BIN:-probe}"
# The agent's own shell needs `probe` too: a Claude Code started from a terminal opened before
# the install has no ~/.local/bin on PATH, so its `probe session state on` failed "command not
# found" (live auto-mode test, 2026-11-09). Claude Code sources $CLAUDE_ENV_FILE before each Bash
# call: add a folder holding `probe` there, once, only when PATH lacks it. Codex runs a login shell.
# Only a CLI found above (absolute): the bare `probe` fallback names the session's folder, which
# the agent can write. Not the CLI's own folder either: uv's tool folder holds Probe's python and
# uvicorn, which would fill in for an agent that has none. A folder in Probe's state holding only a
# `probe` link goes on PATH, last.
case "$PROBE_BIN" in /*) _probe_found=1 ;; *) _probe_found="" ;; esac
_probe_link_dir="${XDG_STATE_HOME:-$HOME/.local/state}/probe/agent-bin"
# A relative folder would follow cwd; a `:` would split it into PATH entries, the part after it relative.
case "$_probe_link_dir" in *:* | [!/]*) _probe_found="" ;; esac
_probe_link="$_probe_link_dir/probe"
if [ -n "${CLAUDE_ENV_FILE:-}" ] && [ -n "$_probe_found" ] && [ -x "$PROBE_BIN" ] \
  && ! command -v probe >/dev/null 2>&1; then
  # Relinked only when it names another CLI: BSD `ln -sf` unlinks first, so a rewrite on every start
  # would leave other chats without `probe` for a moment, and two starts at once could fail it.
  # Read back after `ln` whatever it said: a start racing this one writes the same link.
  [ "$(readlink "$_probe_link" 2>/dev/null)" = "$PROBE_BIN" ] \
    || { mkdir -p "$_probe_link_dir" && ln -sfn "$PROBE_BIN" "$_probe_link"; } 2>/dev/null
  if [ "$(readlink "$_probe_link" 2>/dev/null)" = "$PROBE_BIN" ]; then
    _probe_path_line="$(printf 'export PATH="${PATH:+$PATH:}"%q' "$_probe_link_dir")"
    grep -qxF "$_probe_path_line" "$CLAUDE_ENV_FILE" 2>/dev/null \
      || printf '%s\n' "$_probe_path_line" >> "$CLAUDE_ENV_FILE" 2>/dev/null || true
  fi
fi
# The running harness's own manifest (.claude-plugin, .codex-plugin, .kimi-plugin),
# from the harness registry beside this script.
MANIFEST_DIR="$("$PY" "$PLUGIN_ROOT/hooks/_hook_harness.py" manifest-dir 2>/dev/null || true)"
export PROBE_PLUGIN_JSON="$PLUGIN_ROOT/${MANIFEST_DIR:-.claude-plugin}/plugin.json"
# PROBE_BASE_URL (self-host) is honored by version_check.py if exported; otherwise
# it reads the CLI config, then falls back to the hosted API.

# Funnel telemetry (observability only, PROBE_TELEMETRY=off to disable). The
# observer parses stdin and hands the event to a detached sender, so this adds
# one interpreter start and no network to the hook path. After the exports
# above on purpose: the detached sender inherits PROBE_BIN/PROBE_PLUGIN_JSON.
printf '%s' "$HOOK_INPUT" | "$PY" "$PLUGIN_ROOT/hooks/telemetry.py" session-start 2>/dev/null || true

# Probe's skills live in the agent's OWN skills folder, so they carry no plugin
# prefix; the plugin stays their source and this copies it out when it differs.
# Silent and fail-open, and a no-op on every harness but Claude Code.
"$PY" "$PLUGIN_ROOT/hooks/root_skills_sync.py" >/dev/null 2>&1 || true

out="$("$PY" "$PLUGIN_ROOT/hooks/version_check.py" 2>/dev/null)" || out=""
if [ -n "$out" ]; then
  printf '%s\n' "$out"
else
  printf '{"continue": true}\n'
fi
exit 0
