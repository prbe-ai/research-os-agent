"""Static release gates for the Codex plugin surfaces."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from probe.cli import plugin_cli


ROOT = Path(__file__).resolve().parents[1]


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _base_version(value: str) -> str:
    return value.split("+", 1)[0]


def test_tracking_plugin_uses_native_oauth_mcp() -> None:
    manifest = _json(ROOT / "plugins/probe-research/.codex-plugin/plugin.json")
    server = manifest["mcpServers"]["probe-research"]
    assert server == {
        "url": "https://mcp.research.prbe.ai/mcp",
        "auth": "oauth",
        "required": False,
    }
    assert manifest["skills"] == "./skills/"
    hooks = _json(ROOT / "plugins/probe-research/hooks/hooks.json")["hooks"]
    command = hooks["SessionStart"][0]["hooks"][0]["command"]
    assert "PROBE_AGENT=codex" in command
    assert "PLUGIN_ROOT" in command


def test_repo_marketplace_exposes_tracking_and_capture() -> None:
    marketplace = _json(ROOT / ".agents/plugins/marketplace.json")
    entries = {entry["name"]: entry for entry in marketplace["plugins"]}
    assert set(entries) == {"probe-research", "probe-research-tap"}
    for entry in entries.values():
        assert entry["source"]["source"] == "local"
        assert entry["policy"]["installation"] == "AVAILABLE"
        assert entry["policy"]["authentication"] in {"ON_INSTALL", "ON_USE"}
        assert entry["category"]


def test_capture_plugin_has_codex_manifest_and_hooks() -> None:
    root = ROOT / "plugins/probe-research-tap"
    manifest = _json(root / ".codex-plugin/plugin.json")
    hooks = _json(root / "hooks/hooks.json")["hooks"]
    assert manifest["name"] == "probe-research-tap"
    assert set(hooks) == {"SessionStart", "SessionEnd"}


def test_both_plugins_are_single_source_dual_target_packages() -> None:
    for name in ("probe-research", "probe-research-tap"):
        root = ROOT / "plugins" / name
        claude_manifest = _json(root / ".claude-plugin/plugin.json")
        codex_manifest = _json(root / ".codex-plugin/plugin.json")
        assert claude_manifest["name"] == codex_manifest["name"] == name
        assert _base_version(claude_manifest["version"]) == _base_version(
            codex_manifest["version"]
        )


def test_tracking_skills_are_one_shared_four_skill_tree() -> None:
    root = ROOT / "plugins/probe-research"
    assert {path.parent.name for path in (root / "skills").glob("*/SKILL.md")} == {
        "start-research-work",
        "track-research-work",
        "capture-run-inputs",
        "show-research-timeline",
    }
    assert not (ROOT / "plugins/prbe-codex-tap-plugin").exists()


def test_plugin_cli_contains_the_only_agent_verb_translation(monkeypatch) -> None:
    calls: list[tuple[str, list[str]]] = []

    def fake_run(source: str, args: list[str], *, timeout: float):
        calls.append((source, args))
        return plugin_cli.claude_cli.Result(ok=True)

    monkeypatch.setattr(plugin_cli, "run", fake_run)
    plugin_cli.refresh_marketplace(plugin_cli.CLAUDE, "research-os-agent")
    plugin_cli.refresh_marketplace(plugin_cli.CODEX, "research-os-agent")
    plugin_cli.install(plugin_cli.CLAUDE, "probe-research@research-os-agent")
    plugin_cli.install(plugin_cli.CODEX, "probe-research@research-os-agent")

    assert calls == [
        ("claude_code", ["plugin", "marketplace", "update", "research-os-agent"]),
        ("codex", ["plugin", "marketplace", "upgrade", "research-os-agent"]),
        ("claude_code", ["plugin", "install", "probe-research@research-os-agent"]),
        ("codex", ["plugin", "add", "probe-research@research-os-agent"]),
    ]


def test_codex_plugin_cli_closes_stdin(monkeypatch) -> None:
    observed: dict = {}

    def fake_subprocess_run(command, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setattr(plugin_cli.shutil, "which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(plugin_cli.subprocess, "run", fake_subprocess_run)

    result = plugin_cli.run("codex", ["plugin", "list", "--json"], timeout=1)

    assert result.ok is True
    assert observed["stdin"] is subprocess.DEVNULL


def test_codex_mcp_auth_status_selects_the_named_server(monkeypatch) -> None:
    payload = json.dumps(
        [
            {"name": "PRBE", "auth_status": "o_auth"},
            {"name": "probe-research", "auth_status": "not_logged_in"},
        ]
    )
    monkeypatch.setattr(
        plugin_cli,
        "run",
        lambda *_args, **_kwargs: plugin_cli.claude_cli.Result(ok=True, detail=payload),
    )
    assert plugin_cli.codex_mcp_auth_status("probe-research") == "not_logged_in"
