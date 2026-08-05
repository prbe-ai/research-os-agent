"""The Claude Code headers helper reports plugin metadata without starting the CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
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


# --- the config read ------------------------------------------------------
#
# NO `probe` ON PATH, deliberately, in every test below. The helper falls back to
# shelling out to the CLI when its own read finds nothing, so a test that leaves a
# real `probe` reachable passes whether the read works or not -- which is exactly
# how a read that never once matched the shape the wizard writes stayed green. PATH
# here holds a python3 and nothing else, so the only thing that can answer is the
# config read under test.


def _hermetic_path(tmp_path: Path) -> str:
    """A PATH with an interpreter and no `probe`."""
    bin_dir = tmp_path / "hermetic-bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    return str(bin_dir)


def _run_helper_against_config(tmp_path: Path, config: dict) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    config_home = tmp_path / "config"
    (config_home / "probe").mkdir(parents=True)
    (config_home / "probe" / "config.json").write_text(json.dumps(config))
    home.mkdir()
    return subprocess.run(
        [str(_HELPER)],
        env={
            "CLAUDE_PLUGIN_ROOT": str(tmp_path / "plugin"),
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(config_home),
            "PATH": _hermetic_path(tmp_path),
        },
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_helper_reads_the_active_context_of_a_v2_config(tmp_path: Path) -> None:
    """The shape `probe wizard` actually writes.

    This is the whole bug: the read only knew v1's top-level key, so on every
    install the wizard has ever produced the fast path returned nothing. A machine
    where the CLI fallback could not resolve a `probe` then sent an unauthenticated
    request, and the edge's `WWW-Authenticate` challenge put Claude Code into an
    OAuth flow -- `/mcp`, re-authenticate, on a device the installer had just
    authorized.
    """
    result = _run_helper_against_config(
        tmp_path,
        {
            "version": 2,
            "current_context": "default",
            "contexts": {
                "default": {
                    "token": "probe_pat_write",
                    "mcp_token": "probe_pat_read",
                }
            },
        },
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["Authorization"] == "Bearer probe_pat_read"


def test_helper_reads_a_non_default_active_context(tmp_path: Path) -> None:
    """`current_context` decides, not the first or the one named "default"."""
    result = _run_helper_against_config(
        tmp_path,
        {
            "version": 2,
            "current_context": "work",
            "contexts": {
                "default": {"mcp_token": "probe_pat_wrong"},
                "work": {"mcp_token": "probe_pat_right"},
            },
        },
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["Authorization"] == "Bearer probe_pat_right"


def test_helper_still_reads_a_v1_flat_config(tmp_path: Path) -> None:
    """v1 is migrated in memory on read and never rewritten, so a config written
    before named contexts can still be sitting on disk untouched."""
    result = _run_helper_against_config(tmp_path, {"mcp_token": "probe_pat_v1"})

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["Authorization"] == "Bearer probe_pat_v1"


def test_helper_serves_no_token_when_the_active_context_is_missing(tmp_path: Path) -> None:
    """An unknown `current_context` must not fall through to a sibling.

    Handing the MCP another context's credential would point it at an endpoint the
    user is not on -- a wrong answer is worse here than no answer, because no answer
    is a diagnosable failure and a wrong one silently reads someone else's lab.
    """
    result = _run_helper_against_config(
        tmp_path,
        {
            "version": 2,
            "current_context": "gone",
            "contexts": {"default": {"mcp_token": "probe_pat_other"}},
        },
    )

    assert result.returncode == 1
    assert "probe_pat_other" not in result.stdout


def test_helper_never_serves_the_write_token(tmp_path: Path) -> None:
    """The MCP surface is read-only. A context holding only a write token has no
    credential for this helper, and must not borrow one."""
    result = _run_helper_against_config(
        tmp_path,
        {
            "version": 2,
            "current_context": "default",
            "contexts": {"default": {"token": "probe_pat_write"}},
        },
    )

    assert result.returncode == 1
    assert "probe_pat_write" not in result.stdout


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
