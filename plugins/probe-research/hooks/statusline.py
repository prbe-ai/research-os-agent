#!/usr/bin/env python3
"""Probe Research status-line segment: is this session's work landing in Probe?

Reads the status-line payload on stdin, prints ONE bounded segment, exits 0.

    (nothing)                           Probe is not configured on this machine
      ● tracking                        configured; this session has recorded nothing
      ● tracking → bird-sql-sft         its work is filed under that project
      ● tracking → bird-sql-sft · running  ...and a run it opened is executing now
      ◐ tracking → bird-sql-sft · not capturing session transcript: halted
                                        ...work lands, but no transcript daemon is watching
      ● read-only                       searching prior work, recording nothing
      ● off                             not calling Probe at all (RED, not yellow)

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

KIMI CODE (`$KIMI_CODE_HOME/tui.toml` `[status_line] command`) runs this too,
with `KIMI_CODE_STATUS_LINE=1` set. Its command REPLACES Kimi's footer line 1
and only the first stdout line counts, so there the output is the whole line:
Kimi's own `model  ~/cwd  branch` (or the first line of a researcher's own
command, handed over in `PROBE_SL_PREFIX` by the chain) followed by the
segment. Kimi kills the command after 300 ms; this stays at the same ~26 ms.

STDIN MAY BE EMPTY, and that is a supported case rather than a bug. The
status line is a single global slot, so this command is typically CHAINED
after somebody else's — and a predecessor that does `input=$(cat)` has already
drained the pipe. The wizard's status-line step builds a chain that tees stdin to
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


def _kimi() -> bool:
    """Kimi Code sets this on its status-line command's environment."""
    return os.environ.get("KIMI_CODE_STATUS_LINE") == "1"


def _uuid_tail(value: str) -> str:
    """The UUID a harness id ends with (Kimi's `session_<uuid>`): Probe keys
    every session by the bare UUID."""
    tail = value[-36:]
    if len(tail) == 36 and tail.count("-") == 4 and all(c in "0123456789abcdefABCDEF-" for c in tail):
        return tail.lower()
    return value


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
    if "sessionId" in payload:
        # Kimi Code's snapshot. Empty until the first message (Kimi creates the
        # session then): no session yet, so the line shows what a new one starts
        # as. Never the environment fallback: the footer's copy of this renderer
        # carries only RENDERER_FILES, and `_hook_harness` is not one of them.
        kimi_session = payload.get("sessionId")
        return _uuid_tail(kimi_session) if isinstance(kimi_session, str) and kimi_session else ""
    transcript = payload.get("transcript_path")
    if isinstance(transcript, str) and transcript:
        # `~/.claude/projects/<slug>/<session-id>.jsonl` — the basename IS the id.
        stem = os.path.splitext(os.path.basename(transcript))[0]
        if stem:
            return stem
    try:
        return _load("_hook_harness").session_id()
    except Exception:  # noqa: BLE001 - a copy without the module renders the default
        return ""


#: The status line is Claude Code's (`statusLine` in its settings.json): who
#: records for THIS coding agent decides a session not yet marked.
AGENT = "claude_code"  # harness-literal-ok: the status line is a Claude Code feature
#: ...or Kimi Code's (`[status_line] command` in its tui.toml).
KIMI_AGENT = "kimi_code"  # harness-literal-ok: Kimi Code's footer runs this renderer


def segment(payload: dict) -> str:
    agent = KIMI_AGENT if _kimi() else AGENT
    marker = _load("_session_marker")

    # ONE parse of the config file, two answers off it: whether Probe is set up
    # at all, and this machine's tracking default. Both are needed on every
    # render and the file is the only I/O here worth counting.
    config = marker._read_config()
    if not marker.configured(config):
        return ""

    session_id = resolve_session_id(payload)
    cwd = payload.get("cwd")
    cwd = cwd if isinstance(cwd, str) and cwd else None
    if not session_id:
        switch, _source = marker.resolve_state_default(cwd, config)
        switch = marker.seed_state("", switch, agent)
        return marker.render(
            None,
            configured=True,
            tracking=marker.state_allows_writes(switch),
            color=_color(),
            session_state=switch,
            daemon=marker.daemon_session("", agent),
        )

    state = marker.read(session_id)
    switch = marker.session_state(session_id)
    if switch is None:
        # No decision on disk YET, most often: Claude Code draws this line once
        # while SessionStart is still running, and does not draw it again until
        # the conversation moves. So resolve exactly as that hook is about to
        # seed it (`seed_state`: `on` by who records), or a new daemon session
        # shows the agent's words until its first prompt (Richard 2026-09-29).
        switch, _source = marker.resolve_state_default(cwd, config)
        switch = marker.seed_state(session_id, switch, agent)
    tracking = marker.state_allows_writes(switch)
    daemon = marker.daemon_status(session_id, switch)
    return marker.render(
        state,
        configured=True,
        tracking=tracking,
        live=tracking and marker.is_live(state),
        color=_color(),
        session_state=switch,
        # A worker that has not taken its first lease yet is STARTING, not down,
        # when it can start at all (the daemon holds its key) -- the grace the
        # prompt hook gives (`tracking_guard._daemon_notice`). This line is drawn
        # right at session start and not again until the conversation moves.
        daemon_live=daemon is not None and (
            daemon[0] == marker.DAEMON_LIVE
            or (daemon[1] == marker.DAEMON_NOT_STARTED and marker.companion_key_held(config))
        ),
        # `read only (daemon)` in a session the daemon reads for; a session the
        # lean plugin has not marked yet goes by who records for Claude Code.
        daemon=marker.daemon_session(session_id, agent),
        # `on (inline)`: the researcher set Probe inline (`/probe inline`).
        inline=marker.is_inline(session_id, switch),
        # `· paused: not ML`: the daemon paused itself (`auto_paused`).
        paused=marker.auto_paused(session_id, switch),
    )


def kimi_line(payload: dict, ours: str) -> str:
    """Kimi Code's footer line 1, which this command replaces: the researcher's
    own first line when the chain hands one over, else Kimi's `model  ~/cwd
    branch`, then our segment."""
    prefix = (os.environ.get("PROBE_SL_PREFIX") or "").strip()
    if not prefix:
        parts = []
        model = payload.get("model")
        if isinstance(model, str) and model:
            parts.append(model)
        cwd = payload.get("cwd")
        if isinstance(cwd, str) and cwd:
            home = os.path.expanduser("~")
            parts.append("~" + cwd[len(home):] if home and (cwd == home or cwd.startswith(home + os.sep)) else cwd)
        branch = payload.get("gitBranch")
        if isinstance(branch, str) and branch:
            parts.append(branch)
        prefix = "  ".join(parts)
    return "  ".join(part for part in (prefix, ours) if part)


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
    if _kimi():
        try:
            out = kimi_line(payload, out)
        except BaseException:  # noqa: BLE001 - a render path may never traceback
            pass
    if out:
        # No newline: the caller joins segments, and a newline would become a row.
        sys.stdout.write(out.replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
