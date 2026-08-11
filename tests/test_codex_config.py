"""The Codex `config.toml` table the wizard owns.

The stakes here are not "the shortcut did not work". A malformed config.toml
stops Codex from starting at all -- `codex mcp list` answers "failed to load
bootstrap configuration" and every command with it -- so the tests that matter
most are the ones about NOT writing.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tomllib

import pytest

from probe.cli import codex_config


def _config(tmp_path):
    return tmp_path / "config.toml"


def test_writes_a_header_entry_and_never_the_key_that_breaks_codex(tmp_path):
    """`bearer_token` is refused by Codex for streamable HTTP servers, and the
    refusal takes down its whole config load -- so the writer must emit
    `http_headers` and nothing that resembles the trap."""
    path = _config(tmp_path)

    codex_config.write_mcp_bearer(
        "probe-research",
        url="https://mcp.research.prbe.ai/mcp",
        token="probe_pat_example",
        path=path,
    )

    written = path.read_text(encoding="utf-8")
    assert "bearer_token" not in written
    parsed = tomllib.loads(written)
    entry = parsed["mcp_servers"]["probe-research"]
    assert entry["url"] == "https://mcp.research.prbe.ai/mcp"
    assert entry["http_headers"] == {"Authorization": "Bearer probe_pat_example"}


def test_the_file_holding_a_credential_is_not_world_readable(tmp_path):
    path = _config(tmp_path)
    path.write_text('[tui]\ntheme = "dark"\n', encoding="utf-8")
    path.chmod(0o644)

    codex_config.write_mcp_bearer(
        "probe-research", url="https://example.test/mcp", token="probe_pat_x", path=path
    )

    assert path.stat().st_mode & 0o077 == 0


def test_unrelated_configuration_survives_the_write(tmp_path):
    path = _config(tmp_path)
    path.write_text(
        "\n".join(
            [
                'model = "gpt-5.6"',
                "",
                "[tui]",
                'theme = "dark"',
                "",
                "[mcp_servers.something-else]",
                'command = "other-mcp"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    codex_config.write_mcp_bearer(
        "probe-research", url="https://example.test/mcp", token="probe_pat_x", path=path
    )

    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert parsed["model"] == "gpt-5.6"
    assert parsed["tui"]["theme"] == "dark"
    assert parsed["mcp_servers"]["something-else"]["command"] == "other-mcp"
    assert "probe-research" in parsed["mcp_servers"]


def test_rewriting_replaces_the_table_instead_of_stacking_a_second_one(tmp_path):
    """A rotated token must not leave the old one in the file, and an entry
    written in an earlier shape must not keep its `oauth`/`bearer_token_env_var`
    keys -- either would keep Codex asking to log in."""
    path = _config(tmp_path)
    path.write_text(
        "\n".join(
            [
                "[mcp_servers.probe-research]",
                'url = "https://mcp.research.prbe.ai/mcp"',
                'bearer_token_env_var = "PROBE_MCP_TOKEN"',
                'oauth_resource = "https://mcp.research.prbe.ai"',
                "",
                "[tui]",
                'theme = "dark"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    codex_config.write_mcp_bearer(
        "probe-research", url="https://example.test/mcp", token="probe_pat_new", path=path
    )

    written = path.read_text(encoding="utf-8")
    assert written.count("[mcp_servers.probe-research]") == 1
    assert "bearer_token_env_var" not in written
    assert "oauth_resource" not in written
    parsed = tomllib.loads(written)
    assert parsed["mcp_servers"]["probe-research"]["http_headers"] == {
        "Authorization": "Bearer probe_pat_new"
    }
    assert parsed["tui"]["theme"] == "dark"


def test_a_quoted_table_name_is_replaced_not_duplicated(tmp_path):
    """`codex mcp add` writes the bare spelling, a human may quote it. Missing
    the quoted form would append a second table for the same server, which is a
    TOML duplicate-key error and takes Codex down."""
    path = _config(tmp_path)
    path.write_text(
        '[mcp_servers."probe-research"]\nurl = "https://old.test/mcp"\n',
        encoding="utf-8",
    )

    codex_config.write_mcp_bearer(
        "probe-research", url="https://example.test/mcp", token="probe_pat_x", path=path
    )

    written = path.read_text(encoding="utf-8")
    assert '[mcp_servers."probe-research"]' not in written
    parsed = tomllib.loads(written)
    assert parsed["mcp_servers"]["probe-research"]["url"] == "https://example.test/mcp"


def test_an_already_broken_config_is_left_exactly_as_it_was(tmp_path):
    """Someone else's syntax error stays someone else's. Rewriting part of a
    file we cannot parse would make the next failure ours."""
    path = _config(tmp_path)
    broken = "[mcp_servers.probe-research\nurl = nope\n"
    path.write_text(broken, encoding="utf-8")

    with pytest.raises(codex_config.ConfigError):
        codex_config.write_mcp_bearer(
            "probe-research", url="https://example.test/mcp", token="probe_pat_x", path=path
        )

    assert path.read_text(encoding="utf-8") == broken


def test_a_token_with_quotes_cannot_smuggle_itself_out_of_the_string(tmp_path):
    path = _config(tmp_path)

    codex_config.write_mcp_bearer(
        "probe-research",
        url="https://example.test/mcp",
        token='evil"\nmodel = "pwned',
        path=path,
    )

    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "model" not in parsed
    assert parsed["mcp_servers"]["probe-research"]["http_headers"]["Authorization"] == (
        'Bearer evil"\nmodel = "pwned'
    )


def test_removal_takes_the_table_and_leaves_the_rest(tmp_path):
    path = _config(tmp_path)
    codex_config.write_mcp_bearer(
        "probe-research", url="https://example.test/mcp", token="probe_pat_x", path=path
    )
    with path.open("a", encoding="utf-8") as handle:
        handle.write('\n[tui]\ntheme = "dark"\n')

    removed = codex_config.remove_mcp_server("probe-research", path=path)

    assert removed.changed is True
    parsed = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "probe-research" not in parsed.get("mcp_servers", {})
    assert parsed["tui"]["theme"] == "dark"


def test_removal_is_a_no_op_when_there_is_nothing_of_ours_to_remove(tmp_path):
    path = _config(tmp_path)
    path.write_text('[tui]\ntheme = "dark"\n', encoding="utf-8")

    assert codex_config.remove_mcp_server("probe-research", path=path).changed is False
    assert (
        codex_config.remove_mcp_server("probe-research", path=tmp_path / "absent.toml").changed
        is False
    )


def test_the_url_comes_from_the_installed_plugin_not_a_constant(tmp_path, monkeypatch):
    """Registering a guessed URL would point Codex at the wrong server holding a
    valid token, so the manifest is the only acceptable source -- and its
    absence has to mean "no shortcut", not "use production"."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    manifest = (
        tmp_path
        / "plugins"
        / "cache"
        / "research-os-agent"
        / "probe-research"
        / "0.17.0"
        / ".codex-plugin"
        / "plugin.json"
    )
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        '{"mcpServers": {"probe-research": {"url": "https://self-hosted.test/mcp"}}}',
        encoding="utf-8",
    )

    found = codex_config.plugin_mcp_url("probe-research", marketplace="research-os-agent")
    assert found == "https://self-hosted.test/mcp"

    missing = codex_config.plugin_mcp_url("absent-plugin", marketplace="research-os-agent")
    assert missing is None


@pytest.mark.skipif(shutil.which("codex") is None, reason="codex not installed")
def test_codex_itself_accepts_what_we_write(tmp_path):
    """The only check that can catch the failure that matters.

    Every other test here asserts the output parses as TOML, and `bearer_token`
    is the standing proof that parsing is not acceptance: it is valid TOML that
    Codex rejects, and the rejection takes down its whole config load rather
    than one server. A suite that never asks Codex can therefore go green over
    a file that stops the CLI from starting.

    So this one shells out. It writes the entry the wizard writes, next to
    config Codex already had, and requires `codex mcp list --json` to still
    parse AND to report our server as authenticated by the header.
    """
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        "\n".join(
            [
                'model = "gpt-5.6"',
                "",
                "[tui]",
                'theme = "dark"',
                "",
                "[mcp_servers.unrelated]",
                'command = "some-other-mcp"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    codex_config.write_mcp_bearer(
        "probe-research",
        url="https://mcp.research.prbe.ai/mcp",
        token="probe_pat_placeholder",
        path=home / "config.toml",
    )

    completed = subprocess.run(
        [shutil.which("codex"), "mcp", "list", "--json"],
        env={**os.environ, "CODEX_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    # A non-zero exit here is the disaster case: "failed to load bootstrap
    # configuration" means every codex command is down, not just this server.
    assert completed.returncode == 0, (
        f"codex refused the config we wrote: {completed.stderr.strip()}"
    )
    servers = json.loads(completed.stdout)
    ours = [s for s in servers if isinstance(s, dict) and s.get("name") == "probe-research"]
    assert len(ours) == 1, f"expected exactly one probe-research row, got {ours}"
    assert ours[0].get("auth_status") == codex_config.BEARER_STATUS
    # The rest of the user's config survived the edit.
    assert any(s.get("name") == "unrelated" for s in servers if isinstance(s, dict))
