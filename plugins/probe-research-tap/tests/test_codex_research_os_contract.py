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
    hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())
    assert set(hooks["hooks"]) == {"SessionStart", "SessionEnd"}
    start = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    end = hooks["hooks"]["SessionEnd"][0]["hooks"][0]
    assert "session-start.sh" in start["command"]
    assert "session-end.sh" in end["command"]
    assert end["timeout"] == 3
