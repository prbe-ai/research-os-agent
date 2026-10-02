"""The team note's audit line, delivered when a PERSON submits a prompt.

WHY THIS IS A HOOK. The line used to live inside the rendered team-note block in
`CLAUDE.md` / `AGENTS.md`. That file is read by every session of the harness --
`claude -p`, `codex exec`, a cron job, a subagent -- and an audit dispatched into
one of those asks for a background agent it cannot spawn, for a researcher who is
not there to read the result. Prompt submission is the one event that means a
person is present, so the dispatch moved here.

WHAT IT DOES NOT DECIDE. Whether the note is due, which half to run, whether
the session is automated, and which ONE session on the machine gets the audit
are all answered by `probe notes audit-advisory`. This file finds the CLI, asks
once per session, and prints what it said. The one thing it decides is the one
only it can see: whether THIS session's research tracking is on.

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



def _hook_harness():
    """The harness resolver beside this file, loaded by explicit path."""
    import importlib.util  # noqa: PLC0415

    key = "_probe_hooks._hook_harness"
    if key in sys.modules:
        return sys.modules[key]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_hook_harness.py")
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module

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
    return _hook_harness().session_id()


def _marker(session_id):
    """Where this session's "already asked" flag lives, or None."""
    if _session_marker is None or not _session_marker.valid_session_id(session_id):
        return None
    try:
        return _session_marker.sessions_dir() / (session_id + MARKER_SUFFIX)
    except Exception:
        return None


def _recording(session_id, cwd) -> bool:
    """Will this session run the audit it is handed?

    The line says to skip it when tracking is off, and asking is what claims the
    machine's audit lease: a read-only session that asked would hold the audit
    for every other session on the box and then do nothing with it. Same
    resolution as the tracking guard -- this session's decision, else the folder
    and machine default. Anything unreadable asks, as before this existed.
    """
    try:
        state = _session_marker.session_state(session_id)
        if state is None:
            state, _source = _session_marker.resolve_state_default(cwd)
        return _session_marker.state_allows_writes(state)
    except Exception:
        return True


def _is_switch_prompt(payload) -> bool:
    """Is this prompt itself a `/probe` switch command?

    The UserPromptSubmit hooks run IN PARALLEL, so on `/probe off` this file can
    read the state `tracking_guard.py` is about to replace: the session would
    claim the machine's audit as `on` and then be told by its own line to skip
    it. Asking on the NEXT prompt reads the settled state. The guard's own
    parser decides, so the two cannot disagree about what a switch is.
    """
    try:
        import tracking_guard

        direction, _shape, _slug = tracking_guard.prompt_direction(payload.get("prompt"))
    except Exception:
        return False
    return direction is not None


def _source() -> str:
    """Which harness's block this session reads, and therefore which budget.

    The hook group this file joined does not export `PROBE_AGENT`, so the
    resolver also reads each harness's own plugin-root variable (Codex's
    `PLUGIN_ROOT`, Claude Code's `CLAUDE_PLUGIN_ROOT`): a wrapper that is ever
    edited out does not silently mislabel every Codex session.
    """
    return _hook_harness().current().id


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
    # NOT MARKED either: the switch can be turned on later in this session.
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if _is_switch_prompt(payload) or not _recording(
        session_id, cwd if isinstance(cwd, str) and cwd else None
    ):
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
            # Names the holder of the machine's audit lease, so this session is
            # told again if this telling never reaches the model. An env var,
            # not a flag: an older CLI would refuse an unknown option.
            env=dict(os.environ, PROBE_NOTE_AUDIT_SESSION=session_id),
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

    event = payload.get("hook_event_name") if isinstance(payload, dict) else None
    try:
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
        sys.stdout.flush()
    except Exception:
        # NOT MARKED: nothing reached the model. This session now holds the
        # machine's audit lease, so its next prompt is told again.
        return 0

    # MARKED ONLY AFTER THE WRITE. Marking first meant a failed write left the
    # session "told" with nothing said, and the lease it holds silenced everyone.
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # pragma: no cover - a hook may never raise into the turn
        sys.exit(0)
