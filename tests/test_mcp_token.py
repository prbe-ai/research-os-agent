"""`probe mcp` — the read-only MCP credential.

This lived in a markdown slash command that shelled out to
``grep -q PROBE_MCP_TOKEN "$PROFILE" || echo export ... >> "$PROFILE"``. The guard
stopped duplicates but also made the write a no-op whenever the variable already
existed, so rotating a token silently left the revoked one in place. Prose cannot be
tested; these are the cases that regression guards.
"""

from __future__ import annotations

import importlib
import json

import pytest

from probe import cli
from probe.sdk.config import config_path, load_file
from tests.conftest import make_client

# `probe.cli.main` the function shadows `probe.cli.main` the module on the package.
impl = importlib.import_module("probe.cli.main")


@pytest.fixture
def wired(app, tmp_path, monkeypatch):
    """CLI against the fake API, with config in a scratch XDG dir (never the real one)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.delenv("PROBE_MCP_TOKEN", raising=False)

    def factory(**_kw):
        return make_client(app, tmp_spool=tmp_path / "spool")

    monkeypatch.setattr(cli, "Client", factory)

    def _no_device(*_a, **_kw):
        # Reaching the browser flow means a test would poll until timeout; fail loudly.
        raise AssertionError("device_login must not run in tests")

    monkeypatch.setattr(impl, "device_login", _no_device)
    app.me_scopes = ["read"]
    return app


def _stored() -> str | None:
    return load_file().get("mcp_token")


# -- rotation: the reported bug ---------------------------------------------
def test_rotating_replaces_the_token_and_leaves_no_stale_copy(wired, capsys):
    assert cli.main(["mcp", "token", "set", "--token", "probe_pat_AAAA"]) == 0
    assert _stored() == "probe_pat_AAAA"

    assert cli.main(["mcp", "token", "set", "--token", "probe_pat_BBBB"]) == 0
    assert _stored() == "probe_pat_BBBB"

    raw = config_path().read_text()
    assert "probe_pat_AAAA" not in raw  # the stale token is gone, not shadowed
    assert raw.count("probe_pat_BBBB") == 1  # and the new one is not duplicated


def test_rotation_does_not_disturb_the_write_token(wired):
    from probe.sdk.config import save_file

    save_file({"base_url": "http://test", "token": "probe_pat_WRITE"})
    cli.main(["mcp", "token", "set", "--token", "probe_pat_READ"])
    data = load_file()
    assert data["token"] == "probe_pat_WRITE"  # separate credential, untouched
    assert data["mcp_token"] == "probe_pat_READ"


def test_set_reports_whether_it_verified(wired, capsys):
    cli.main(["mcp", "token", "set", "--token", "probe_pat_AAAA"])
    out = capsys.readouterr().out
    assert "verified: yes" in out
    assert "Restart" in out  # MCP clients do not hot-load a credential
    assert "probe_pat_AAAA" not in out  # never echo the secret back


# -- refusing to persist a known-bad token ----------------------------------
def test_set_refuses_a_token_the_api_rejects(wired, capsys):
    wired.me_status = 401
    assert cli.main(["mcp", "token", "set", "--token", "probe_pat_REVOKED"]) == 1
    assert _stored() is None  # nothing saved
    assert "rejected" in capsys.readouterr().err


def test_set_keeps_the_existing_token_when_the_new_one_is_rejected(wired):
    cli.main(["mcp", "token", "set", "--token", "probe_pat_GOOD"])
    wired.me_status = 401
    assert cli.main(["mcp", "token", "set", "--token", "probe_pat_BAD"]) == 1
    assert _stored() == "probe_pat_GOOD"


def test_no_verify_persists_but_says_it_did_not_check(wired, capsys):
    assert cli.main(["mcp", "token", "set", "--token", "probe_pat_X", "--no-verify"]) == 0
    assert _stored() == "probe_pat_X"
    assert "verified: no" in capsys.readouterr().out


# -- the read-only guard -----------------------------------------------------
def test_set_refuses_a_write_scoped_token(wired, capsys):
    wired.me_scopes = ["read", "write"]
    assert cli.main(["mcp", "token", "set", "--token", "probe_pat_WRITER"]) == 1
    assert _stored() is None
    assert "read-only" in capsys.readouterr().err


def test_allow_write_overrides_the_guard(wired, capsys):
    wired.me_scopes = ["read", "write"]
    assert cli.main(["mcp", "token", "set", "--token", "probe_pat_W", "--allow-write"]) == 0
    assert _stored() == "probe_pat_W"
    assert "can write" in capsys.readouterr().out


# -- how tokens actually arrive ---------------------------------------------
@pytest.mark.parametrize(
    "pasted",
    ["  probe_pat_X  ", "Bearer probe_pat_X", "bearer probe_pat_X", '"probe_pat_X"',
     "'probe_pat_X'", '"Bearer probe_pat_X"', "probe_pat_X\n"],
)
def test_pasted_tokens_are_normalized(wired, pasted):
    assert cli.main(["mcp", "token", "set", "--token", pasted]) == 0
    assert _stored() == "probe_pat_X"


@pytest.mark.parametrize("token", ["ros_pat_legacy", "probe_pat_current"])
def test_both_prefixes_are_accepted(wired, token):
    """The prefix only discriminates; auth is a sha256 lookup. Legacy tokens must live."""
    assert cli.main(["mcp", "token", "set", "--token", token]) == 0
    assert _stored() == token


@pytest.mark.parametrize("bad", ["", "   ", "probe_pat_a b", "probe_pat_a\tb"])
def test_malformed_tokens_are_rejected(wired, bad):
    assert cli.main(["mcp", "token", "set", "--token", bad]) != 0
    assert _stored() is None


# -- headers: what the MCP client calls -------------------------------------
def test_headers_emits_the_authorization_header(wired, capsys):
    cli.main(["mcp", "token", "set", "--token", "probe_pat_R"])
    capsys.readouterr()
    assert cli.main(["mcp", "headers"]) == 0
    assert json.loads(capsys.readouterr().out) == {"Authorization": "Bearer probe_pat_R"}


def test_headers_prefers_the_environment(wired, capsys, monkeypatch):
    """A shell that already exports the token keeps working unchanged."""
    cli.main(["mcp", "token", "set", "--token", "probe_pat_FILE"])
    capsys.readouterr()
    monkeypatch.setenv("PROBE_MCP_TOKEN", "probe_pat_ENV")
    cli.main(["mcp", "headers"])
    assert json.loads(capsys.readouterr().out)["Authorization"] == "Bearer probe_pat_ENV"


def test_headers_never_falls_back_to_the_write_token(wired, capsys):
    from probe.sdk.config import save_file

    save_file({"base_url": "http://test", "token": "probe_pat_WRITE"})
    assert cli.main(["mcp", "headers"]) == 1  # no mcp_token: fail, do not hand over a writer
    assert "probe_pat_WRITE" not in capsys.readouterr().out


def test_unset_removes_the_token(wired, capsys):
    cli.main(["mcp", "token", "set", "--token", "probe_pat_R"])
    assert cli.main(["mcp", "token", "unset"]) == 0
    assert _stored() is None
    assert cli.main(["mcp", "token", "unset"]) == 0  # idempotent


def test_env_prints_a_shell_safe_export(wired, capsys):
    cli.main(["mcp", "token", "set", "--token", "probe_pat_R"])
    capsys.readouterr()
    cli.main(["mcp", "env"])
    assert capsys.readouterr().out.strip() == "export PROBE_MCP_TOKEN=probe_pat_R"


# -- status: the diagnostic that did not exist ------------------------------
def test_status_flags_a_rejected_token(wired, capsys):
    cli.main(["mcp", "token", "set", "--token", "probe_pat_R"])
    wired.me_status = 401
    assert cli.main(["mcp", "status"]) == 1
    assert "REJECTED" in capsys.readouterr().out


def test_status_flags_env_shadowing_a_rotated_config_token(wired, capsys, monkeypatch):
    """The failure that looks like a successful rotation: config is new, the shell is old."""
    cli.main(["mcp", "token", "set", "--token", "probe_pat_NEW"])
    monkeypatch.setenv("PROBE_MCP_TOKEN", "probe_pat_OLD")
    capsys.readouterr()
    cli.main(["mcp", "status"])
    out = capsys.readouterr().out
    assert "DIFFERENT tokens" in out
    assert "environment wins" in out


def test_status_without_a_token_says_what_to_run(wired, capsys):
    assert cli.main(["mcp", "status"]) == 1
    assert "probe mcp token set" in capsys.readouterr().out


def test_status_does_not_print_the_secret(wired, capsys):
    cli.main(["mcp", "token", "set", "--token", "probe_pat_SECRET"])
    capsys.readouterr()
    cli.main(["mcp", "status"])
    assert "probe_pat_SECRET" not in capsys.readouterr().out
