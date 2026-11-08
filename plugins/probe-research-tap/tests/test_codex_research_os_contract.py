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

    Claude Code reads hooks.json; Codex reads hooks/codex.json, the same entries
    (test below), so every event added here reaches Codex too. UserPromptSubmit
    is the respawn hook (hooks/ensure-daemon.sh): on a
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
    # 20s: Claude Code enforces each hook's own timeout, and a container that
    # raises its exit budget needs the full 15s wait. Codex gets 3s (below).
    assert end["timeout"] == 20
    assert ensure["timeout"] == 5
    # Silent on the common path: this one fires on every prompt.
    assert "statusMessage" not in ensure


#: Codex (codex-rs hooks/src/engine/discovery.rs `normalize_command_hook`, 0.167) caps these
#: events' timeouts at 3s and warns ("clamping SessionEnd hook timeout to 3s") on a larger one.
CODEX_CAPPED_EVENTS = {"SessionEnd": 3, "Interrupt": 3}


def _codex_hooks_file() -> Path:
    """The file Codex reads: the manifest's `hooks` path, else hooks/hooks.json (codex-rs core-plugins loader.rs)."""
    manifest = json.loads((PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text())
    return PLUGIN_ROOT / manifest.get("hooks", "./hooks/hooks.json")


def test_codex_reads_its_own_hooks_file_within_its_caps() -> None:
    """Codex's warning badge: a SessionEnd entry over 3s. Claude Code keeps its 20s in hooks.json."""
    codex = json.loads(_codex_hooks_file().read_text())["hooks"]
    for event, cap in CODEX_CAPPED_EVENTS.items():
        for group in codex.get(event, []):
            for hook in group["hooks"]:
                assert hook.get("timeout", 1) <= cap, f"Codex clamps {event}'s {hook.get('timeout')}s to {cap}s and warns"
    claude = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    assert claude["SessionEnd"][0]["hooks"][0]["timeout"] == 20


def test_codex_runs_the_same_entries_as_claude_code() -> None:
    """codex.json is hooks.json with each capped timeout at Codex's cap: same events, commands, messages."""
    codex = json.loads((PLUGIN_ROOT / "hooks" / "codex.json").read_text())["hooks"]
    claude = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text())["hooks"]
    for event, cap in CODEX_CAPPED_EVENTS.items():
        for group in claude.get(event, []):
            for hook in group["hooks"]:
                hook["timeout"] = min(hook["timeout"], cap)
    assert codex == claude
