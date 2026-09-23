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
printf '%s' "$HOOK_INPUT" | python3 -c '
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
exit 0
