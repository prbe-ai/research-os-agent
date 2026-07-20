"""Contract tests for the v2 named-context config.

Two of these guard things that fail *silently* in production if they regress:

- the v1 -> v2 migration, because a user's existing ``~/.config/probe/config.json`` is
  the only copy of their token;
- ``PROBE_BASE_URL`` outranking a context's ``base_url``, because the hosted MCP pods set
  that env var to the in-cluster service. A context that could outrank it would point
  production at the wrong API while ``/healthz`` still returned 200 — the failure would
  surface as wrong data, not as an outage.
"""

from __future__ import annotations

import json

import pytest

from probe.sdk.config import (
    CONFIG_VERSION,
    DEFAULT_BASE_URL,
    DEFAULT_CONTEXT,
    clear_context,
    config_path,
    delete_context,
    load_context,
    load_file,
    resolve,
    save_context,
    save_file,
    use_context,
)


def _write_raw(data: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# -- migration ---------------------------------------------------------------
def test_flat_v1_config_still_resolves_after_upgrade():
    """The hard requirement: an existing flat file keeps working, untouched."""
    _write_raw({"base_url": "http://legacy", "token": "probe_pat_LEGACY"})

    settings = resolve()

    assert settings.base_url == "http://legacy"
    assert settings.token == "probe_pat_LEGACY"


def test_migration_does_not_rewrite_the_file_on_read():
    """A read-only command must not rewrite a config symlinked into a dotfiles repo."""
    flat = {"base_url": "http://legacy", "token": "probe_pat_LEGACY"}
    _write_raw(flat)

    resolve()
    load_file()
    load_context()

    assert json.loads(config_path().read_text()) == flat


def test_migration_preserves_unrecognized_keys():
    """A key we do not know about is more likely a newer client's than junk."""
    _write_raw({"token": "probe_pat_X", "future_key": "keep-me"})

    assert load_context()["future_key"] == "keep-me"


def test_missing_file_is_empty_dict():
    # mcp/server.py calls .get() on this unguarded — it must never be None.
    assert load_file() == {}
    assert load_context() == {}
    assert resolve().base_url == DEFAULT_BASE_URL


def test_unreadable_file_is_empty_dict():
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")

    assert load_file() == {}


# -- the prod-MCP guard ------------------------------------------------------
def test_env_base_url_outranks_a_context(monkeypatch: pytest.MonkeyPatch):
    """PROBE_BASE_URL beats the file. The hosted MCP depends on this."""
    save_context({"base_url": "http://from-context"})
    monkeypatch.setenv("PROBE_BASE_URL", "http://from-env")

    assert resolve().base_url == "http://from-env"


def test_explicit_arg_outranks_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PROBE_BASE_URL", "http://from-env")

    assert resolve(base_url="http://explicit").base_url == "http://explicit"


def test_stdio_mcp_token_resolves_from_a_v2_file():
    """The live self-host path: `probe mcp token set` writes v2, the server reads it."""
    save_context({"mcp_token": "probe_pat_READ"})

    assert load_context().get("mcp_token") == "probe_pat_READ"
    assert resolve().mcp_token == "probe_pat_READ"


def test_mcp_token_never_falls_back_to_the_write_token():
    save_context({"token": "probe_pat_WRITE"})

    assert resolve().mcp_token is None


# -- context mechanics -------------------------------------------------------
def test_saving_a_context_leaves_its_neighbours_alone():
    save_context({"token": "probe_pat_PROD"}, name="prod")
    save_context({"token": "probe_pat_STAGING"}, name="staging")

    assert load_context("prod")["token"] == "probe_pat_PROD"
    assert load_context("staging")["token"] == "probe_pat_STAGING"


def test_use_context_switches_what_resolve_reads():
    save_context({"base_url": "http://prod"}, name="prod")
    save_context({"base_url": "http://staging"}, name="staging")

    use_context("prod")
    assert resolve().base_url == "http://prod"

    use_context("staging")
    assert resolve().base_url == "http://staging"


def test_logout_clears_only_the_active_context():
    """Logging out of staging must not sign the user out of prod."""
    save_context({"token": "probe_pat_PROD"}, name="prod")
    save_context({"token": "probe_pat_STAGING"}, name="staging")
    use_context("staging")

    clear_context()

    assert load_context("staging").get("token") is None
    assert load_context("prod")["token"] == "probe_pat_PROD"


def test_deleting_the_active_context_leaves_a_coherent_file():
    save_context({"token": "probe_pat_PROD"}, name="prod")
    use_context("staging")

    delete_context("staging")

    data = load_file()
    assert data["current_context"] in data["contexts"]


def test_first_save_writes_v2_shape():
    save_context({"token": "probe_pat_X"})

    data = json.loads(config_path().read_text())
    assert data["version"] == CONFIG_VERSION
    assert data["current_context"] == DEFAULT_CONTEXT
    assert data["contexts"][DEFAULT_CONTEXT]["token"] == "probe_pat_X"


def test_a_flat_file_is_rewritten_as_v2_on_next_save():
    save_file({"base_url": "http://legacy", "token": "probe_pat_LEGACY"})

    save_context({"mcp_token": "probe_pat_READ"})

    data = json.loads(config_path().read_text())
    assert data["version"] == CONFIG_VERSION
    ctx = data["contexts"][DEFAULT_CONTEXT]
    assert ctx["token"] == "probe_pat_LEGACY"  # carried across, not dropped
    assert ctx["mcp_token"] == "probe_pat_READ"


# -- anchors -----------------------------------------------------------------
def test_project_nests_under_workspace():
    save_context({"workspace": {"id": "ws-1", "project": "proj-1"}})

    settings = resolve()
    assert settings.workspace == "ws-1"
    assert settings.project == "proj-1"


def test_env_anchors_outrank_the_context(monkeypatch: pytest.MonkeyPatch):
    """Scripts and CI must never depend on a developer's ambient context."""
    save_context({"workspace": {"id": "ws-1", "project": "proj-1"}})
    monkeypatch.setenv("PROBE_WORKSPACE", "ws-env")
    monkeypatch.setenv("PROBE_PROJECT", "proj-env")

    settings = resolve()
    assert settings.workspace == "ws-env"
    assert settings.project == "proj-env"


def test_explicit_anchor_outranks_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PROBE_PROJECT", "proj-env")

    assert resolve(project="proj-explicit").project == "proj-explicit"
