"""The project's NOTES.md: one markdown file per project, read and written as text.

This replaced a structured `probe note` command group — a kind vocabulary
(intent/decision/observation/...), supersession, an authority field. None of it was
validated, aggregated or grouped by anything server-side, so eight kinds bought a
single list filter, and the durable claims it was meant to hold were already being
written as markdown in the repo. What survived is the part that was actually load
bearing: a fixed filename, and getting it in front of an agent without being asked.
"""

from __future__ import annotations

import io

import pytest

from probe import cli
from probe.mcp.service import ResearchReadService
from probe.mcp.source import ResearchOSSource
from probe.sdk.client import PROJECT_NOTES_FILE
from tests.conftest import make_client


def _service(client) -> ResearchReadService:
    return ResearchReadService(ResearchOSSource(client))


def test_notes_round_trip_as_plain_text(client):
    project = client.create_project("folding")
    assert client.get_project_notes(project["id"]) is None

    client.set_project_notes(project["id"], "# folding\n\nGKE, not DOKS.\n")

    assert client.get_project_notes(project["id"]) == "# folding\n\nGKE, not DOKS.\n"


def test_a_rewrite_supersedes_by_being_the_newer_row(client):
    """No supersession machinery: a project is an artifact anchor, so each edit is a
    new row under the same name and the newest one IS the current text. The old
    version stays retrievable without anything having to model 'replaced'."""
    project = client.create_project("edited")
    client.set_project_notes(project["id"], "first")
    client.set_project_notes(project["id"], "second")

    assert client.get_project_notes(project["id"]) == "second"
    rows = [
        r for r in client.list_project_artifacts(project["id"])
        if r["name"] == PROJECT_NOTES_FILE
    ]
    assert len(rows) == 2, "history is kept, not overwritten"


def test_the_project_card_carries_the_notes_without_being_asked(client):
    """The whole point. An agent orients with browse_research and a card; a briefing
    it has to KNOW to ask for is one it does not read."""
    project = client.create_project("surfaced")
    client.set_project_notes(project["id"], "harbor has no generic-k8s backend")

    card = _service(client).get_entity(f"project:{project['id']}")

    assert card["data"]["notes"]["text"] == "harbor has no generic-k8s backend"
    assert "truncated" not in card["data"]["notes"]
    assert card["completeness"]["state"] == "complete"


def test_a_long_file_is_excerpted_on_the_card_and_whole_in_the_view(client):
    """A card is the cheap glance. An unbounded file on it would blow the token
    budget of every project read, and `_ViewData(rows=None)` is atomic — nothing
    downstream would truncate it."""
    project = client.create_project("long")
    body = "x" * 5000
    client.set_project_notes(project["id"], body)
    service = _service(client)

    card = service.get_entity(f"project:{project['id']}")
    assert card["data"]["notes"]["truncated"] is True
    assert len(card["data"]["notes"]["text"]) < len(body)
    assert card["data"]["notes"]["read_all"] == 'view="notes"'

    full = service.get_entity(f"project:{project['id']}", view="notes")
    assert full["data"]["notes"] == body
    assert full["data"]["file"] == PROJECT_NOTES_FILE


def test_a_project_with_no_notes_says_nothing_rather_than_claiming_empty(client):
    project = client.create_project("blank")
    card = _service(client).get_entity(f"project:{project['id']}")
    assert "notes" not in card["data"]
    assert _service(client).get_entity(
        f"project:{project['id']}", view="notes"
    )["data"]["notes"] is None


def test_the_card_survives_an_unreadable_notes_file(client, app):
    """A card that starts FAILING because a notes read hiccuped is worse than a card
    without notes: it takes out the cheapest, most-used read in the tool."""
    project = client.create_project("broken")
    client.set_project_notes(project["id"], "text")
    app.fail_paths = {f"/v1/projects/{project['id']}/artifacts"}

    card = _service(client).get_entity(f"project:{project['id']}")
    assert card["data"]["notes"] == {"unavailable": True}


# -- CLI ----------------------------------------------------------------------


@pytest.fixture
def wired(app, tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli, "Client", lambda **_kw: make_client(app, tmp_spool=tmp_path / "spool")
    )
    cli.main(["project", "create", "p"])
    cli.main(["project", "use", "p"])
    return app


def test_notes_write_reads_stdin_and_show_prints_it(wired, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("decided on GKE\n"))
    assert cli.main(["notes", "write"]) == 0
    assert "wrote NOTES.md on project p" in capsys.readouterr().err

    assert cli.main(["notes", "show"]) == 0
    assert capsys.readouterr().out == "decided on GKE\n"


def test_notes_append_does_not_clobber_the_other_agents_paragraph(wired, capsys, monkeypatch):
    """A plain write is last-one-wins. With two agents on one project that silently
    deletes the other's text, which is the failure mode a shared file invites."""
    monkeypatch.setattr("sys.stdin", io.StringIO("first agent\n"))
    cli.main(["notes", "write"])
    monkeypatch.setattr("sys.stdin", io.StringIO("second agent\n"))
    cli.main(["notes", "write", "--append"])
    capsys.readouterr()

    cli.main(["notes", "show"])
    assert capsys.readouterr().out == "first agent\n\nsecond agent\n"


def test_notes_show_on_an_empty_project_says_so_on_stderr(wired, capsys):
    assert cli.main(["notes", "show"]) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert "has no NOTES.md yet" in out.err


def test_notes_without_a_project_says_what_to_do(app, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "Client", lambda **_kw: make_client(app, tmp_spool=tmp_path / "spool")
    )
    assert cli.main(["notes", "show"]) != 0
    assert "probe project use" in capsys.readouterr().err
