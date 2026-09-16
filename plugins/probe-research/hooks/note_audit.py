"""The team note's audit line, delivered when a PERSON submits a prompt.

WHY THIS IS A HOOK. The line used to live inside the rendered team-note block in
`CLAUDE.md` / `AGENTS.md`. That file is read by every session of the harness --
`claude -p`, `codex exec`, a cron job, a subagent -- and an audit dispatched into
one of those asks for a background agent it cannot spawn, for a researcher who is
not there to read the result. Prompt submission is the one event that means a
person is present, so the dispatch moved here.

WHAT IT DOES NOT DECIDE. Whether the note is due, which half to run, and whether
the session is automated are all answered by `probe notes audit-advisory`. This
file finds the CLI, asks once per session, and prints what it said.

STDLIB ONLY, PYTHON 3.9, FAIL-SOFT. It runs under the system python3 on a
prompt-submit budget: every failure path is "say nothing", never an error into
the researcher's turn.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:  # pragma: no cover - the vendored helper is present in a real install
    import _session_marker
except Exception:  # pragma: no cover
    _session_marker = None

#: Asking costs a subprocess. ONCE PER SESSION is the right rate: the note's
#: state cannot meaningfully change between two prompts of the same
#: conversation, and a reminder repeated every turn is one an agent learns to
#: skip.
MARKER_SUFFIX = ".audit-advised"

#: The subprocess budget sits UNDER the hook's own 5s: if the harness kills
#: the hook first there is no chance to fail soft, and the researcher sees a
#: hook error instead of nothing. Measured cold on this box: 0.77s.
#: The CLI is not always on PATH -- a uv tool install puts it somewhere a hook's
#: stripped environment has never heard of. Same search `team-note-sync.sh` does.
CANDIDATES = (
    os.path.expanduser("~/.local/bin/probe"),
    os.path.expanduser("~/.local/share/uv/tools/probe-research/bin/probe"),
)


def _probe_bin():
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory, "probe")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    for candidate in CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _session_id(payload):
    value = payload.get("session_id") if isinstance(payload, dict) else None
    if isinstance(value, str) and value:
        return value
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or ""


def _marker(session_id):
    """Where this session's "already asked" flag lives, or None."""
    if _session_marker is None or not _session_marker.valid_session_id(session_id):
        return None
    try:
        return _session_marker.sessions_dir() / (session_id + MARKER_SUFFIX)
    except Exception:
        return None


def _source() -> str:
    """Which harness's block this session reads, and therefore which budget.

    WHY NOT JUST `PROBE_AGENT`: the hook group this file joined does not export
    it -- only the six that already needed it do -- so reading it alone answered
    `claude_code` inside Codex, which is both the wrong `available_bytes` and
    the Codex session inheriting Claude Code's "spawn a BACKGROUND subagent"
    dispatch. The wrapper now exports it the way `session-start.sh`'s does, and
    `PLUGIN_ROOT` (Codex's own variable; Claude Code sets CLAUDE_PLUGIN_ROOT)
    is the same discriminator read directly, so a wrapper that is ever edited
    out does not silently mislabel every Codex session.
    """
    if os.environ.get("PROBE_AGENT") == "codex":
        return "codex"
    if os.environ.get("PLUGIN_ROOT") and not os.environ.get("CLAUDE_PLUGIN_ROOT"):
        return "codex"
    return "claude_code"


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}

    session_id = _session_id(payload)
    marker = _marker(session_id)
    # NO MARKER, NO ASK. Without somewhere to record that we have spoken this
    # would run on every prompt of every session, which is the behaviour the
    # once-per-session rule exists to prevent.
    if marker is None or marker.exists():
        return 0

    binary = _probe_bin()
    if binary is None:
        return 0

    source = _source()
    try:
        result = subprocess.run(
            [binary, "notes", "audit-advisory", "--source", source],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=4,
        )
    except Exception:
        return 0
    if result.returncode != 0:
        return 0
    advisory = (result.stdout or b"").decode("utf-8", "replace").strip()
    if not advisory:
        # NOT MARKED. Nothing was said, so nothing has been spent -- and the note
        # may cross its threshold later in the same session.
        return 0

    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
    except Exception:
        pass

    event = payload.get("hook_event_name") if isinstance(payload, dict) else None
    sys.stdout.write(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": event if isinstance(event, str) and event else "UserPromptSubmit",
                    "additionalContext": advisory,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # pragma: no cover - a hook may never raise into the turn
        sys.exit(0)
