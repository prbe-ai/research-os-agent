"""Regressions for the defects the pre-merge review caught.

Each test here failed before its fix. They are grouped in one module because what
they have in common is provenance, not subject: every one is a bug that the type
checker, the parity guard, and the happy-path tests all let through.
"""

from __future__ import annotations

import json

import pytest

from probe import cli
from probe.sdk.client import Anchor
from probe.sdk.config import clear_context, load_context, save_context

from tests.conftest import _WS_MINE, make_client


@pytest.fixture
def wired(app, tmp_path, monkeypatch):
    """A fresh Client per invocation: `_client()` closes its transport on exit, so a
    single shared instance breaks any test that runs two commands."""
    def factory(**_kw):
        return make_client(app, tmp_spool=tmp_path / "spool")

    monkeypatch.setattr(cli, "Client", factory)
    return app


def _out(capsys) -> dict | list:
    return json.loads(capsys.readouterr().out)


# -- the read-only MCP boundary ---------------------------------------------
def test_a_missing_mcp_token_never_falls_back_to_the_write_token(monkeypatch):
    """The whole reason mcp_token is a separate credential.

    `Client(token=None)` resolves through env/file and lands on the WRITE token, so
    building the MCP service that way handed write scope to an MCP client whenever the
    active context had no mcp_token — which `probe context use <new>` creates by
    construction.
    """
    from probe.mcp import server as server_mod

    save_context({"base_url": "http://test", "token": "probe_pat_WRITE"}, name="staging")
    from probe.sdk.config import use_context

    use_context("staging")
    monkeypatch.delenv("PROBE_MCP_TOKEN", raising=False)
    monkeypatch.delenv("PROBE_TOKEN", raising=False)

    captured = {}

    class FakeClient:
        def __init__(self, *, settings, fail_open):
            captured["token"] = settings.token

        def close(self):
            pass

    monkeypatch.setattr(server_mod, "Client", FakeClient)
    server_mod._clients.clear()
    server_mod._sources.clear()
    monkeypatch.setattr(server_mod, "ResearchOSSource", lambda client: object())
    monkeypatch.setattr(server_mod, "ResearchReadService", lambda source: object())

    server_mod._service_from_token()

    assert captured["token"] != "probe_pat_WRITE"
    assert captured["token"] is None


# -- anchor selection --------------------------------------------------------
def test_a_run_positional_plus_an_anchor_flag_is_a_usage_error(wired, client, tmp_path, capsys):
    """`artifact add RUN --project P` used to reinterpret the run id as a file path
    and die with a raw FileNotFoundError, because the positional shift ran BEFORE
    the two-anchor check and left `run` permanently None."""
    blob = tmp_path / "f.bin"
    blob.write_bytes(b"x")

    code = cli.main(["artifact", "add", "SOME-RUN-ID", str(blob), "--project", "p1"])

    assert code == 2
    assert "exactly one thing" in capsys.readouterr().err


def test_uri_on_a_file_anchor_is_a_usage_error_not_a_traceback(wired, client, capsys):
    """A file IS its bytes, so there is no reference-only form — but the SDK's
    ValueError was not in main()'s handler chain and printed a traceback."""
    code = cli.main(["artifact", "add", "--uri", "s3://b/k", "--name", "n", "--workspace", _WS_MINE])

    assert code == 2
    assert "cannot be" in capsys.readouterr().err


def test_a_missing_local_file_reports_cleanly(wired, client, capsys):
    """The anchored upload path is strict — no fail-open spool absorbs a typo."""

    code = cli.main(["artifact", "add", "/nonexistent/typo.bin", "--project", "p1"])

    assert code == 1
    assert "error:" in capsys.readouterr().err


# -- slug resolution ---------------------------------------------------------
def test_project_commands_accept_a_slug(wired, client, capsys):
    """Every /v1/projects/{id} route types the param as a UUID, so a slug reached the
    server as a 422 about UUID parsing even though the help said 'id or slug'."""
    proj = client.create_project("by-slug", workspace_id=_WS_MINE)

    assert cli.main(["project", "get", "by-slug"]) == 0

    assert _out(capsys)["id"] == proj["id"]


def test_an_unknown_slug_is_a_usage_error(wired, client, capsys):
    code = cli.main(["project", "get", "does-not-exist"])

    assert code == 2
    assert "no project with id or slug" in capsys.readouterr().err


def test_restore_finds_an_archived_project_by_slug(wired, client, capsys):
    """Archived rows are filtered out of the default listing, so slug resolution has
    to look again before claiming the project does not exist."""
    proj = client.create_project("archived-one", workspace_id=_WS_MINE)
    client.archive_project(proj["id"])

    assert cli.main(["project", "restore", "archived-one"]) == 0

    assert _out(capsys)["archived_at"] is None


# -- ambient anchors actually applied ---------------------------------------
def test_run_start_uses_the_active_project(wired, client, capsys):
    """`project use` stored and displayed the anchor but no write path applied it.

    The context holds the project ID (stable across renames), while ``Client.run``
    get-or-creates by SLUG — so this must store an id, exactly like `project use`
    does. An earlier version of this test stored a slug and passed while the live
    CLI silently created a second project named after the UUID.
    """
    proj = client.create_project("ambient", workspace_id=_WS_MINE)
    save_context({"workspace": {"id": _WS_MINE, "project": proj["id"]}})

    assert cli.main(["run", "start", "--experiment", "e", "--hypothesis", "h", "--name", "r"]) == 0

    run_id = capsys.readouterr().out.strip()
    exp = client.get_experiment(client.get_run(run_id)["experiment_id"])
    assert exp["project_id"] == proj["id"]
    # And no junk project was conjured from the id.
    assert [p["slug"] for p in client.list_projects().items] == ["ambient"]


def test_an_explicit_project_still_beats_the_context(wired, client, capsys):
    """Ambient context is a convenience, never a requirement."""
    ambient = client.create_project("ambient", workspace_id=_WS_MINE)
    explicit = client.create_project("explicit", workspace_id=_WS_MINE)
    save_context({"workspace": {"id": _WS_MINE, "project": ambient["slug"]}})

    cli.main([
        "run", "start", "--experiment", "e", "--hypothesis", "h", "--name", "r",
        "--project", "explicit",
    ])

    run_id = capsys.readouterr().out.strip()
    exp = client.get_experiment(client.get_run(run_id)["experiment_id"])
    assert exp["project_id"] == explicit["id"]


# -- logout really logs out --------------------------------------------------
def test_logout_removes_credentials_it_does_not_recognize():
    """clear_context subtracted a known-key list, but _migrate deliberately carries
    unknown keys forward — so a credential from a newer client survived logout."""
    save_context({"token": "probe_pat_X", "future_credential": "leftover"})

    clear_context()

    assert load_context() == {}


# -- redaction ---------------------------------------------------------------
def test_context_list_never_prints_a_usable_token(wired, client, capsys):
    save_context({"token": "probe_pat_SUPERSECRETVALUE", "base_url": "http://test"})

    assert cli.main(["context", "list"]) == 0

    printed = capsys.readouterr().out
    assert "probe_pat_SUPERSECRETVALUE" not in printed
    assert "SUPERSECRET" not in printed


# -- shared anchor (previously untested end to end) --------------------------
def test_upload_straight_into_the_shared_folder(client, app, tmp_path):
    """The SHARED anchor is the one with no path id — reached by the fallthrough
    branch of _presign_anchored and permitted a None anchor_id."""
    blob = tmp_path / "s.bin"
    blob.write_bytes(b"S")

    out = client.upload_file(Anchor.SHARED, None, "s.bin", str(blob))

    assert out["status"] == "complete"
    assert [f["id"] for f in client.list_anchored(Anchor.SHARED)] == [out["id"]]


def test_cli_shared_add_and_delete(wired, client, tmp_path, capsys):
    blob = tmp_path / "s.bin"
    blob.write_bytes(b"S")

    assert cli.main(["shared", "add", str(blob)]) == 0
    artifact_id = _out(capsys)["id"]

    assert cli.main(["shared", "delete", artifact_id]) == 0
    assert client.list_anchored(Anchor.SHARED) == []


# -- the scoped-upload wire shape -------------------------------------------
def test_a_scoped_presign_sends_only_the_four_permitted_keys(client, app, tmp_path):
    """ScopedUploadRequest is additionalProperties:false. Asserting the client refuses
    run-only fields proves the guard; this proves the body it actually sends."""
    blob = tmp_path / "f.bin"
    blob.write_bytes(b"x")

    client.upload_file(Anchor.PROJECT, "p1", "f.bin", str(blob))

    presign = next(r for r in app.requests if r.url.path.endswith("/artifacts/uploads"))
    assert set(json.loads(presign.content)) <= {
        "name", "content_hash", "size_bytes", "content_type",
    }


# -- context CLI -------------------------------------------------------------
def test_context_show_surfaces_the_env_override(wired, client, monkeypatch, capsys):
    """The command exists precisely to explain why the effective value differs from
    the stored one."""
    save_context({"base_url": "http://from-context"})
    monkeypatch.setenv("PROBE_BASE_URL", "http://from-env")

    assert cli.main(["context", "show"]) == 0

    row = _out(capsys)
    assert row["base_url"] == "http://from-context"
    assert row["resolved"]["base_url"] == "http://from-env"


def test_context_delete_of_an_unknown_name_exits_nonzero(wired, client, capsys):

    assert cli.main(["context", "delete", "nope"]) == 1
    assert "no such context" in capsys.readouterr().err
