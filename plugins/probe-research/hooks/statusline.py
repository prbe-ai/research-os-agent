#!/usr/bin/env python3
"""Probe Research status-line segment: is this session's work landing in Probe?

Reads the status-line payload on stdin, prints ONE bounded segment, exits 0.

    (nothing)                           Probe is not configured on this machine
      ● untracked                       configured; this session has recorded nothing
      ● tracked → bird-sql-sft          its work is filed under that project
      ● tracked → bird-sql-sft · running  ...and a run it opened is executing now
      ● tracking off                    the researcher ended tracking here

CONTRACT — this runs on a RENDER PATH, once per status-line update:

  * NO NETWORK, and no `probe` subprocess. The answer comes from a marker file
    that `statusline_refresh.py` keeps warm in the background. `probe --version`
    alone costs ~300ms of interpreter and imports; this renders in ~26ms measured
    against the system python3, which is the entire reason it imports nothing of
    ours but one vendored stdlib module loaded by explicit path. Adding an import
    here is not free -- pulling in `urllib.request` alone doubled it.
  * SILENT ON EVERY FAILURE. A traceback here is a broken prompt, and stderr
    from a status-line command is logged on every render. Exit 0 with empty
    stdout instead.
  * ONE LINE, NEVER A NEWLINE. Claude Code splits this command's stdout on
    newlines and renders each as its own status row.

STDIN MAY BE EMPTY, and that is a supported case rather than a bug. The
status line is a single global slot, so this command is typically CHAINED
after somebody else's — and a predecessor that does `input=$(cat)` has already
drained the pipe. `probe statusline install` builds a chain that tees stdin to
both sides, but a hand-written chain will not, so fall back to the session id
in the environment before giving up.
"""

from __future__ import annotations

import json
import os
import sys


def _load(name: str):
    """A vendored sibling module, by EXPLICIT path only.

    Never a bare import: sys.path can carry the user's project directory, and
    executing a same-named stranger inside a status-line command would be
    arbitrary code execution on every render. Mirrors telemetry.py's `_load_core`.
    """
    import importlib.util  # noqa: PLC0415

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def resolve_session_id(payload: dict) -> str:
    """The session this status line belongs to.

    `session_id` off the payload first — it is the only value that is correct
    when several sessions run at once. The environment fallback exists solely
    for a drained-stdin chain (see the module docstring); it is right in the
    common case of one session per terminal and is the best available guess
    otherwise, which beats rendering nothing.
    """
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        return session_id
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        # `~/.claude/projects/<slug>/<session-id>.jsonl` — the basename IS the id.
        stem = os.path.splitext(os.path.basename(transcript))[0]
        if stem:
            return stem
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CODEX_THREAD_ID") or ""


def segment(payload: dict) -> str:
    marker = _load("_session_marker")

    if not marker.configured():
        return ""

    session_id = resolve_session_id(payload)
    if not session_id:
        return marker.render(None, configured=True, tracking=False, color=_color())

    state = marker.read(session_id)
    tracking = marker.is_tracking(state, marker.tracking_signal(session_id))
    return marker.render(
        state,
        configured=True,
        tracking=tracking,
        live=tracking and marker.is_live(state),
        color=_color(),
    )


def _color() -> bool:
    """Honour NO_COLOR; Claude Code dims the status line itself either way."""
    return not os.environ.get("NO_COLOR")


def main() -> int:
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except (OSError, ValueError):
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
    except ValueError:
        payload = {}

    try:
        out = segment(payload)
    except BaseException:  # noqa: BLE001 - a render path may never traceback
        out = ""
    if out:
        # No newline: the caller joins segments, and a newline would become a row.
        sys.stdout.write(out.replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
