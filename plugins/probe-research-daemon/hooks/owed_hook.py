#!/usr/bin/env python3
"""End of turn, inline sessions only (notes parity N3): send the agent back
when the session still owes what its own writes left behind -- a summary for a
project or experiment it created or concluded, a note on a run it tagged
invalid -- in the words the daemon's writer is sent back with.

The CLI keeps the ledger and decides (`probe session owed --send-back`: each
item at most twice a session, summaries only in recorder mode); this hook only
looks first whether the ledger holds any item of the agent's own
(`session_marker.agent_owes`, one small file read), so a turn that owes nothing
starts no CLI. It prints `{"decision": "block", "reason": ...}` (Claude Code
and Codex continue the turn with the reason; Kimi's adapter turns it into its
own block).

No CLI version floor: only a CLI that has `probe session owed` writes the
ledger this reads first, and one rolled back under it answers `No such command`
(exit 2), which reads as nothing owed.

STDLIB ONLY, PYTHON 3.9, FAIL-OPEN: any failure prints nothing and exits 0.
"""

from __future__ import annotations

import json
import subprocess
import sys

# Sibling imports, resolved because sys.path[0] is this script's directory (run
# as a script, or in-process by dispatch.py beside it), as tracking_guard's.
import _hook_harness
import _session_marker

#: The CLI gets this long (its start is most of it); the Stop lane's own
#: timeout bounds the whole route.
CLI_TIMEOUT_S = 8.0


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return
    session_id = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(session_id, str) or not session_id:
        return
    if not _session_marker.is_inline(session_id) or not _session_marker.agent_owes(session_id):
        return
    probe = _hook_harness.load_sibling("version_check")._probe_bin()
    out = subprocess.run(
        [probe, "session", "owed", "--send-back", "--session", session_id],
        capture_output=True, text=True, timeout=CLI_TIMEOUT_S, stdin=subprocess.DEVNULL,
    )
    reason = (out.stdout or "").strip() if out.returncode == 0 else ""
    if reason:
        sys.stdout.write(json.dumps({"decision": "block", "reason": reason}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # a reminder layer must never become a broken session
    sys.exit(0)
