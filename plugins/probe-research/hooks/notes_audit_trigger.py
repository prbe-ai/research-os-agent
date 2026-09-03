"""SessionStart: say when the team note is due for its periodic audit.

This hook PRINTS A LINE AND NOTHING ELSE. It spawns no process, makes no
network call, and never touches the note — dispatch belongs to the session
(the track-work skill says how), and a hook that burns tokens invisibly is
the wrong default. Registered as its own SessionStart entry in hooks.json so
the existing session-start.sh flow is untouched; Claude Code merges the
additionalContext of sibling hooks.

Two conditions, two owners:
  - TIME: the `<!-- audited YYYY-MM-DD -->` stamp inside the note itself is
    older than the audit interval (or absent — a note never audited is due).
    The stamp lives IN the note so the whole team shares one cadence.
  - SIZE: the last render measured the block near the instruction-file budget.
    That measurement is read from the health file the CLI render writes
    (`<state>/team-note/health.json`) — the pointer-vs-full decision is made
    there against the real budget, and a bash/python mirror of that constant
    would only ever drift. Missing health file = the size half stays quiet.

A 24-hour floor guards both conditions: however over-budget the note is, a
stamp younger than a day never re-fires — otherwise a note that cannot shrink
below the threshold would buy an audit every single session.

FAIL-OPEN AND SILENT, like every hook in this plugin: any surprise degrades to
`{"continue": true}` with no context added. The only bad outcome a bug here
could buy would be a missed or extra audit, never a broken session.

Knobs (environment, invalid values fall back silently):
  PROBE_NOTES_AUDIT_INTERVAL_DAYS  audit cadence, default 7, min 1
  PROBE_NOTES_AUDIT_HORIZON_DAYS   strike-removal age the trigger line
                                   advertises to the auditor, default 7, min 0
                                   (0 = strike-only: the audit removes nothing)
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import sys
from pathlib import Path

#: One canonical stamp shape. The notes-audit skill documents the same literal
#: and tests/test_notes_audit_trigger.py pins the two together — reword the
#: skill and this regex in the same commit or the trigger silently dies.
STAMP_RE = re.compile(r"<!--\s*audited\s+(\d{4}-\d{2}-\d{2})\s*-->", re.IGNORECASE)

#: Fires the size half when the rendered block was measured at or above this
#: fraction of the room its instruction file has left.
SIZE_TRIGGER_PCT = 0.8

DEFAULT_INTERVAL_DAYS = 7
DEFAULT_HORIZON_DAYS = 7


def _int_env(name: str, default: int, floor: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= floor else default


def note_path() -> Path:
    """The synced team-note file for the harness this hook runs under."""
    if os.environ.get("PROBE_AGENT", "") == "codex":
        configured = os.environ.get("CODEX_HOME")
        root = Path(configured).expanduser() if configured else Path.home() / ".codex"
    else:
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        root = Path(configured).expanduser() if configured else Path.home() / ".claude"
    return root / "probe-team-note.md"


def stamp_age_days(body: str, today: _dt.date) -> int | None:
    """Days since the audit stamp, or None when there is no usable stamp.

    None means "never audited" and is DUE: absent, malformed and future-dated
    stamps all land here, so a corrupted stamp buys an audit (which rewrites
    it) rather than suppressing audits until someone notices.
    """
    match = STAMP_RE.search(body)
    if not match:
        return None
    try:
        stamped = _dt.date.fromisoformat(match.group(1))
    except ValueError:
        return None
    age = (today - stamped).days
    return age if age >= 0 else None


def worst_health_pct() -> float | None:
    """The worst measured block/budget fraction from the last render, if any."""
    try:
        import version_policy  # the plugin's synced copy, beside this file

        payload = json.loads(
            (version_policy.state_dir() / "team-note" / "health.json").read_text(encoding="utf-8")
        )
    except Exception:  # noqa: BLE001 - advisory input; any surprise means "no data"
        return None
    sources = payload.get("sources") if isinstance(payload, dict) else None
    if not isinstance(sources, dict):
        return None
    pcts = [
        entry["pct"]
        for entry in sources.values()
        if isinstance(entry, dict) and isinstance(entry.get("pct"), (int, float))
    ]
    return max(pcts) if pcts else None


def decide(
    age: int | None, pct: float | None, interval: int
) -> tuple[bool, str]:
    """(due, reason). The 24h floor binds BOTH conditions when a stamp exists."""
    if age is not None and age < 1:
        return False, ""
    reasons: list[str] = []
    if age is None:
        reasons.append("never audited")
    elif age >= interval:
        reasons.append(f"last audited {age} days ago")
    if pct is not None and pct >= SIZE_TRIGGER_PCT:
        reasons.append(f"at {round(pct * 100)}% of its render budget")
    return (bool(reasons), "; ".join(reasons))


def main() -> str:
    body = note_path().read_text(encoding="utf-8")
    if not body.strip():
        return ""
    interval = _int_env("PROBE_NOTES_AUDIT_INTERVAL_DAYS", DEFAULT_INTERVAL_DAYS, 1)
    horizon = _int_env("PROBE_NOTES_AUDIT_HORIZON_DAYS", DEFAULT_HORIZON_DAYS, 0)
    age = stamp_age_days(body, _dt.date.today())
    due, reason = decide(age, worst_health_pct(), interval)
    if not due:
        return ""
    removal = (
        f"Strikes older than {horizon} days may be removed."
        if horizon > 0
        else "Removal is disabled: strike only, delete nothing."
    )
    # Lane-accurate dispatch, authored HERE so it can never contradict the
    # harness it runs under: Claude Code has background subagents; Codex's
    # sandbox reaps detached children at command teardown (probed live, codex
    # 0.152.1 workspace-write — a setsid/nohup grandchild never outlives the
    # command), so a "background" instruction there is a silent no-op and the
    # honest dispatch is inline-first.
    if os.environ.get("PROBE_AGENT", "") == "codex":
        dispatch = (
            "this sandbox reaps detached processes, so run the `notes-audit` skill "
            "yourself NOW, before taking up the user's work — it audits one small "
            "file and is quick"
        )
    else:
        dispatch = (
            "run the `notes-audit` skill in a BACKGROUND subagent and continue "
            "with the user's work — never inline"
        )
    return (
        f"Probe: the team note is due for its periodic audit ({reason}). "
        f"Per the track-work skill's audit dispatch: {dispatch}. "
        f"{removal} If research tracking is off for this session, skip this."
    )


if __name__ == "__main__":
    try:
        sys.stdin.read()  # drain; SessionStart pipes a JSON payload we do not need
    except Exception:  # noqa: BLE001
        pass
    context = ""
    try:
        context = main()
    except Exception:  # noqa: BLE001 - fail-open by contract; a missed audit beats a broken session
        context = ""
    out: dict[str, object] = {"continue": True}
    if context:
        out["hookSpecificOutput"] = {
            "hookEventName": "SessionStart",
            "additionalContext": context,
        }
    sys.stdout.write(json.dumps(out))
