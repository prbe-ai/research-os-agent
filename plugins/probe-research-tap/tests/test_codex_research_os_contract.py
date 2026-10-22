from __future__ import annotations

import json
import os
from pathlib import Path

from tap import config, killswitch

PLUGIN_ROOT = Path(__file__).resolve().parents[1]


def test_hosted_gateway_contract_is_research_os() -> None:
    previous = os.environ.get("PROBE_TAP_SOURCE")
    os.environ["PROBE_TAP_SOURCE"] = "codex"
    try:
        assert config.webhook_path() == "/ingest/v1/sessions/codex"
        assert killswitch.PATH == "/ingest/v1/sessions/status"
    finally:
        if previous is None:
            os.environ.pop("PROBE_TAP_SOURCE", None)
        else:
            os.environ["PROBE_TAP_SOURCE"] = previous


def test_codex_lifecycle_hooks_start_and_stop_capture() -> None:
    """The exact event set, not a subset.

    One hooks.json serves BOTH plugin manifests (neither declares hooks; both
    systems auto-discover this file), so every event added here reaches Codex
    too. UserPromptSubmit is the respawn hook (hooks/ensure-daemon.sh): on a
    harness that never dispatches the event it is inert, and on one that does
    but carries no `session_id` the script exits 0 without spawning. Keeping
    this an equality assertion is what makes the next addition a decision
    rather than an accident.
    """
    hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
    assert set(hooks["hooks"]) == {"SessionStart", "UserPromptSubmit", "SessionEnd", "Stop"}
    # Stop is the daemon's turn-end signal (hooks/turn-end.sh). It reaches Codex
    # too, by decision: it writes only when the session is in `daemon` and the
    # payload names a transcript, prints nothing, and exits 0 on every failure,
    # so where Codex sends no transcript path the worker keeps its quiet timer.
    turn = hooks["hooks"]["Stop"][0]["hooks"][0]
    assert "turn-end.sh" in turn["command"] and turn["timeout"] == 3
    assert "statusMessage" not in turn
    start = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    end = hooks["hooks"]["SessionEnd"][0]["hooks"][0]
    ensure = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert "session-start.sh" in start["command"]
    assert "session-end.sh" in end["command"]
    assert "ensure-daemon.sh" in ensure["command"]
    # ONE SessionEnd entry, the waiting one (D15/D45): every Probe plugin keeps
    # exactly one entry per event. Through 0.9.24 a flagless timeout-3 entry
    # stood beside it, kept only because Codex trusts a hook by a hash of its
    # whole entry; dropping it costs a Codex user who approved only that one a
    # single re-approval, which `probe status` announces (`codex_hooks`).
    # Never change this entry again: that would ask every Codex user again.
    assert [len(group["hooks"]) for groups in hooks["hooks"].values() for group in groups] == [1] * 4
    assert len(hooks["hooks"]["SessionEnd"]) == 1
    assert end["command"].endswith('exec \"$ROOT/hooks/session-end.sh\" --wait\'')
    # 20s, though Codex (0.159) clamps it to 3s and flags it in `/hooks`:
    # Claude Code enforces each hook's own timeout, and a container that raises
    # its exit budget needs the full 15s wait. session-end.sh budgets its Codex
    # wait inside the clamp instead.
    assert end["timeout"] == 20
    assert ensure["timeout"] == 5
    # Silent on the common path: this one fires on every prompt.
    assert "statusMessage" not in ensure
