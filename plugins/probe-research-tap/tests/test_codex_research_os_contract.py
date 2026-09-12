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
    assert set(hooks["hooks"]) == {"SessionStart", "UserPromptSubmit", "SessionEnd"}
    start = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    end = hooks["hooks"]["SessionEnd"][0]["hooks"][0]
    ensure = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert "session-start.sh" in start["command"]
    assert "session-end.sh" in end["command"]
    assert "ensure-daemon.sh" in ensure["command"]
    assert end["timeout"] == 3
    assert ensure["timeout"] == 5
    # Silent on the common path: this one fires on every prompt.
    assert "statusMessage" not in ensure
