#!/usr/bin/env bash
# Stop hook: the agent finished a turn. Tell the Probe daemon where it ended.
#
# Writes `<state>/probe/sessions/<sid>.turn` = {"offset": <transcript size>,
# "at": <epoch>} when the session is in the `daemon` state. The worker reads it
# (`companion_worker.TURN_SUFFIX`): it drains the transcript up to that offset at
# once instead of waiting for quiet, then runs the conclusions pass over the whole
# session, so what the agent concluded in its last message is recorded while the
# researcher is still reading it.
#
# Silent and harmless on every failure: no output (a Stop hook's stdout can block
# the agent from stopping), no write outside the sessions directory, nothing when
# the session is not in `daemon` or the harness gives no transcript path (Codex's
# payload may not carry one: the worker falls back to its quiet timer).

set -uo pipefail

HOOK_INPUT="$(cat)"
SOURCE="${PROBE_TAP_SOURCE:-claude_code}"
# Any harness but Claude Code and Codex: its payload in Claude Code's shape, its
# session id without the harness's own prefix and its transcript found when the
# payload names none (Kimi Code), from its registry row (tap/hook_env.py).
case "$SOURCE" in
    claude_code | codex) ;;
    *)
        TAP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
        REGISTRY_ENV=$(printf '%s' "$HOOK_INPUT" | PROBE_TAP_SOURCE="$SOURCE" PYTHONPATH="$TAP_ROOT" \
            python3 -m tap hook-env --source "$SOURCE" --payload --transcript 2>/dev/null || true)
        if [ -n "$REGISTRY_ENV" ]; then
            eval "$REGISTRY_ENV"
            HOOK_INPUT="$TAP_HOOK_INPUT"
        fi
        ;;
esac
# Only a session the daemon records gets a turn mark, so its switch is read
# here first: every other Stop costs no python start (it was ~20 ms of this
# hook's ~32). The payload's one plain "session_id" (as ensure-daemon.sh reads
# it, up to 256 KB), its `<sid>.state` as the session marker writes it;
# anything unexpected goes to the python below, which decides as it always did.
WRITE_TURN=1
if [ "${#HOOK_INPUT}" -le 262144 ] && [[ "$HOOK_INPUT" =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([A-Za-z0-9._-]{1,128})\" ]]; then
    TURN_SID="${BASH_REMATCH[1]}"
    # Linear, as ensure-daemon.sh's: never `${x#*pat}` on a payload.
    case "$HOOK_INPUT" in
        *\"session_id\"*\"session_id\"*) ;;
        *)
            TURN_STATE=""
            read -r TURN_STATE 2>/dev/null <"${XDG_STATE_HOME:-$HOME/.local/state}/probe/sessions/$TURN_SID.state" || true
            TURN_STATE="${TURN_STATE//[[:space:]]/}"  # a CR too: a file written on Windows
            case "$TURN_STATE" in [Dd][Aa][Ee][Mm][Oo][Nn]) ;; *) WRITE_TURN="" ;; esac
            ;;
    esac
fi
[ -z "$WRITE_TURN" ] || printf '%s' "$HOOK_INPUT" | python3 -c '
import json, os, re, sys, tempfile, time
from pathlib import Path

try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(0)
sid = data.get("session_id")
path = data.get("transcript_path")
if not (isinstance(sid, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", sid) and isinstance(path, str) and path):
    sys.exit(0)
base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "probe" / "sessions"
try:
    if (base / (sid + ".state")).read_text(encoding="utf-8").strip().lower() != "daemon":
        sys.exit(0)
    size = os.path.getsize(os.path.expanduser(path))
    fd, tmp = tempfile.mkstemp(dir=base, prefix="." + sid + ".", suffix=".turn.tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"offset": size, "at": time.time()}, fh)
    os.replace(tmp, base / (sid + ".turn"))
except OSError:
    sys.exit(0)
' >/dev/null 2>&1 || true

# The Probe service records this session (plan A2, its `.hosted` marker) and
# is gone (a crash, a kill): start it again now, in the background, rather
# than at the next prompt (a long autonomous turn has none). A service
# started again resumes every session it recorded (and rewrites each one's
# marker). Free otherwise: a pattern match, a file read, `kill -0`.
PREFIX="probe-research-tap"
[ "$SOURCE" = codex ] && PREFIX="prbe-codex-tap"
[ -n "${TAP_WATCHER_PREFIX:-}" ] && PREFIX="$TAP_WATCHER_PREFIX"
if [[ "$HOOK_INPUT" =~ \"session_id\"[[:space:]]*:[[:space:]]*\"([A-Za-z0-9._-]{1,128})\" ]]; then
    HOSTF="/tmp/${PREFIX}-watcher-${BASH_REMATCH[1]}.hosted"
    if [ -s "$HOSTF" ]; then
        PID=$(cat "$HOSTF" 2>/dev/null || echo "")
        case "$PID" in '' | *[!0-9]*) PID="" ;; esac
        # (Git Bash on Windows: `kill` knows MSYS pids only; the marker is trusted.)
        case "${OSTYPE:-}" in msys* | cygwin* | win*) PID="" ;; esac
        if [ -n "$PID" ] && ! kill -0 "$PID" 2>/dev/null; then
            # Only the service, which resumes every session it recorded: never
            # SessionStart in the background (its owner lookup walks up from
            # itself, and a detached copy would find no agent and drop the record).
            TAP_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
            PROBE_TAP_SOURCE="$SOURCE" PYTHONPATH="$TAP_ROOT" nohup python3 -m tap service ensure \
                </dev/null >/dev/null 2>&1 &
        fi
    fi
fi
exit 0
