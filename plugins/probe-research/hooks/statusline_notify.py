#!/usr/bin/env python3
"""Tell the reader when this session's tracking state CHANGES.

The status-line segment is the good answer, and Codex cannot render it: its
status line is a picker over built-in items (`/statusline` — "Select which items
to display"; `tui.status_line` is a sequence and an unrecognised entry is ignored
rather than executed). There is no command hook to render into, so the same
information is delivered the only other way an agent offers — a message — and the
whole design problem is making that not be noise.

ON CHANGE, NOT ON A CADENCE. A line every turn saying the same thing is something
a reader learns to skip in about four turns, at which point it is worse than
absent: it costs attention and delivers nothing. So this fires when the state
key actually moves — untracked -> tracked, a different project, a run starting or
finishing — which in a normal session is three or four lines total.

    Probe: tracked → session-tracking-indicator
    Probe: tracked → session-tracking-indicator · running
    Probe: this session is not tracked yet.

OPT-IN, exactly like the segment. `probe statusline install` under Codex sets the
flag this reads; nothing here runs for anyone who did not ask for it.

CONTRACT:
  * THIS HOOK PRINTS, unlike its sibling `statusline_refresh.py`, and printing is
    its entire purpose — but only the one JSON object, and only when something
    changed. Any failure prints NOTHING and exits 0.
  * STDLIB ONLY, PYTHON 3.9: it runs under the system python3 with no `probe`
    package importable.
  * NO NETWORK. It reads the marker `statusline_refresh.py` keeps warm. A hook
    that fetched before speaking would put a network round trip between the user
    pressing enter and the agent answering.
"""

from __future__ import annotations

import json
import os
import sys


def _load(name: str):
    """A vendored sibling module by explicit path — see statusline.py's `_load`."""
    import importlib.util  # noqa: PLC0415

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def disabled() -> bool:
    return (os.environ.get("PROBE_STATUSLINE") or "").strip().lower() in {
        "off",
        "0",
        "false",
        "no",
        "disabled",
    }


def notice(marker, session_id: str) -> str | None:
    """The line to emit, or None when nothing changed.

    The FIRST observation of a session is deliberately not silent-by-default: a
    session that opens already tracked (a resume) has a state worth stating once.
    What it must not do is repeat it.
    """
    state = marker.read(session_id)
    key = marker.state_key(state, live=marker.is_live(state))
    if key == marker.read_notified(session_id):
        return None
    text = marker.message(state, live=key.endswith("|running"))
    marker.write_notified(session_id, key)
    return text


def main(argv: list[str] | None = None) -> int:
    if disabled():
        return 0
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0

    try:
        marker = _load("_session_marker")
        if not marker.notify_enabled() or not marker.valid_session_id(session_id):
            return 0
        signal = marker.tracking_signal(session_id)
        if signal is not None:
            tracking = marker.is_tracking(signal)
        else:
            cwd = payload.get("cwd")
            cwd = cwd if isinstance(cwd, str) and cwd else None
            tracking, _source = marker.resolve_tracking_default(cwd)
        if not tracking:
            return 0  # not tracking here: nothing to announce
        text = notice(marker, session_id)
    except BaseException:  # noqa: BLE001 - a hook that cannot speak stays silent
        return 0

    if text:
        # `systemMessage` is the one field both agents render from a hook; the
        # wire shape is shared (HookUniversalOutputWire on the Codex side).
        sys.stdout.write(json.dumps({"systemMessage": text}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
