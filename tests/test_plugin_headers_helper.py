"""The Claude Code headers helper reports plugin metadata without starting the CLI."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest


_HELPER = (
    Path(__file__).resolve().parent.parent
    / "plugins"
    / "probe-research"
    / "bin"
    / "probe-mcp-headers"
)


def _run_helper(
    plugin_root: Path,
    *,
    include_env_token: bool = True,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    env = {
        **os.environ,
        "CLAUDE_PLUGIN_ROOT": str(plugin_root),
    }
    if include_env_token:
        env["PROBE_MCP_TOKEN"] = "probe_pat_test"
    else:
        env.pop("PROBE_MCP_TOKEN", None)
    env.update(extra_env or {})
    result = subprocess.run(
        [str(_HELPER)],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    )
    return json.loads(result.stdout)


@pytest.mark.parametrize("version", ["0.7.0", "1.2.3+linux-01"])
def test_helper_emits_plugin_kind_and_metadata_version(
    tmp_path: Path,
    version: str,
) -> None:
    metadata = tmp_path / ".claude-plugin" / "plugin.json"
    metadata.parent.mkdir()
    metadata.write_text(
        json.dumps({"name": "probe-research", "version": version}) + "\n"
    )

    assert _run_helper(tmp_path) == {
        "Authorization": "Bearer probe_pat_test",
        "X-Probe-Client": "plugin",
        "X-Probe-Client-Version": version,
    }


def test_helper_keeps_authorization_when_metadata_is_missing(tmp_path: Path) -> None:
    assert _run_helper(tmp_path) == {
        "Authorization": "Bearer probe_pat_test",
    }


@pytest.mark.parametrize("version", ["latest", "0.0.0.dev0", "1.2.3-01"])
def test_helper_drops_malformed_metadata_without_breaking_auth(
    tmp_path: Path,
    version: str,
) -> None:
    metadata = tmp_path / ".claude-plugin" / "plugin.json"
    metadata.parent.mkdir()
    metadata.write_text(
        json.dumps({"name": "probe-research", "version": version}) + "\n"
    )

    assert _run_helper(tmp_path) == {
        "Authorization": "Bearer probe_pat_test",
    }


def test_helper_adds_plugin_metadata_to_cli_fallback_token(tmp_path: Path) -> None:
    metadata = tmp_path / "plugin" / ".claude-plugin" / "plugin.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text('{"name":"probe-research","version":"0.7.0"}\n')

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_probe = fake_bin / "probe"
    fake_probe.write_text(
        "#!/bin/sh\n"
        '[ "$1 $2" = "mcp headers" ] || exit 1\n'
        """printf '%s\\n' '{"Authorization": "Bearer probe_pat_fallback"}'\n"""
    )
    fake_probe.chmod(0o755)

    assert _run_helper(
        tmp_path / "plugin",
        include_env_token=False,
        extra_env={
            "HOME": str(tmp_path / "home"),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        },
    ) == {
        "Authorization": "Bearer probe_pat_fallback",
        "X-Probe-Client": "plugin",
        "X-Probe-Client-Version": "0.7.0",
    }
