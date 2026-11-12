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
absent: it costs attention and delivers nothing. So this fires when what the
notice shows moves (`state_key`: the state words, the title, a capture problem),
never for a run starting or finishing, which in a normal session is a few lines.

    on (inline) | BIRD SFT Fine Tuning
    on (daemon) | nothing filed yet
    on (inline) | BIRD SFT Fine Tuning | session transcript not recorded: halted; fix: see probe doctor

OPT-IN, exactly like the segment. The wizard's status-line step under Codex sets the
flag this reads; nothing here runs for anyone who did not ask for it.

ONE LINE IS ALWAYS ON (fixes-1107b PR B): what the agent changed from this chat that
records less (or moved this chat's switch at all), told to the person once, where the
harness shows a person-only line (Claude Code, Codex, pi; `lowered_lines`).

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
    switch = marker.session_state(session_id)
    is_manual = not marker.is_auto_chat(session_id)  # the status line's tag (only manual is named)
    key = marker.state_key(state, session_state=switch, is_manual=is_manual)  # a run starting changes nothing
    if key == marker.read_notified(session_id):
        return None
    text = marker.message(state, session_state=switch, is_manual=is_manual)
    marker.write_notified(session_id, key)
    return text


def lowered_lines(marker, session_id: str) -> "list[str]":
    """What the agent changed from this chat that the person must hear (fixes-1107b PR B), each
    once: its move of this chat's switch, and a machine setting it turned down from here. ALWAYS
    ON, unlike the opt-in notice below: it is how the person learns their agent recorded less."""
    lines = []
    move = marker.load_agent_move(session_id)
    if move is not None and not move["told"]:
        text = marker.get_agent_move_notice(move["state"])
        if text:
            lines.append(text)
        marker.mark_agent_move_told(session_id, at=move["at"])
    for entry in marker.take_untold(marker.TOLD_IN_CHAT, session=session_id):
        if entry.get("notice"):
            lines.append(entry["notice"])
    return lines


#: `server_notices.ATTENDED_ENV`: a `claude -p` run says "0" (its `systemMessage` goes to a script).
ATTENDED_ENV = "CLAUDE_CODE_SESSION_ATTENDED"


def is_person_watching() -> bool:
    """Would a person see this hook's `systemMessage` (`server_notices.is_person_watching`: not
    Kimi Code, the Cursor app or a `claude -p` run)? Where not, nothing is spent unseen."""
    if os.environ.get(ATTENDED_ENV) == "0":
        return False
    try:
        return _load("_hook_harness").current().shows_person_line()
    except Exception:  # noqa: BLE001 - unknown reads as watched, as there
        return True


def main(argv: list[str] | None = None) -> int:
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0

    lines: list = []
    try:
        marker = _load("_session_marker")
        if not marker.valid_session_id(session_id):
            return 0
        if is_person_watching():
            lines = lowered_lines(marker, session_id)
        if not disabled() and marker.notify_enabled():
            signal = marker.tracking_signal(session_id)
            if signal is not None:
                tracking = marker.is_tracking(signal)
            else:
                cwd = payload.get("cwd")
                cwd = cwd if isinstance(cwd, str) and cwd else None
                tracking, _source = marker.resolve_tracking_default(cwd)
            text = notice(marker, session_id) if tracking else None  # not tracking: nothing to announce
            if text:
                lines.append(text)
    except BaseException:  # noqa: BLE001 - a hook that cannot speak stays silent
        pass

    if lines:
        # `systemMessage` is the one field both agents render from a hook; the
        # wire shape is shared (HookUniversalOutputWire on the Codex side).
        sys.stdout.write(json.dumps({"systemMessage": "\n".join(lines)}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
