"""The project's notes: one free-text markdown document per project.

Two rewrites got it here, and both are worth remembering:

  * it started as a structured `probe note` vocabulary (a `kind` enum of eight,
    `--supersedes`, `--authority`). Nothing server-side validated, aggregated or
    grouped by any of it, so eight kinds bought a single list filter;
  * then as a project-anchored ARTIFACT. Artifact identity is
    anchor+name+content_hash, so every edit appended a new row, a project's
    artifact list filled with copies of one file, and reading a paragraph cost
    three round trips.

It is now a COLUMN on the project (research-os 0094), which is what puts it on a
read the caller already makes.
"""

from __future__ import annotations

import io

import pytest

from probe import cli
from probe.mcp.service import ResearchReadService
from probe.mcp.source import ResearchOSSource
from probe.sdk import errors
from tests.conftest import make_client


def _service(client) -> ResearchReadService:
    return ResearchReadService(ResearchOSSource(client))


def test_notes_round_trip_as_plain_text(client):
    project = client.create_project("folding")
    assert client.get_project_notes(project["id"]) is None

    client.set_project_notes(project["id"], "# folding\n\nGKE, not DOKS.\n")

    assert client.get_project_notes(project["id"]) == "# folding\n\nGKE, not DOKS.\n"


def test_the_notes_arrive_on_the_project_row_itself(client, app):
    """The whole reason it is a column: a caller holding the project row already
    has the notes, with no second request. `get_project_notes` is a convenience
    over that read, not a separate fetch."""
    project = client.create_project("row")
    client.set_project_notes(project["id"], "on the row")

    before = len(app.requests)
    row = client.get_project(project["id"])
    assert row["notes"] == "on the row"
    assert len(app.requests) - before == 1, "one GET, no artifact list, no presign"


def test_a_rewrite_replaces_rather_than_accumulating(client, app):
    """The artifact version appended a row per edit. A column is edited in place,
    which is what 'a markdown file you edit' actually means."""
    project = client.create_project("edited")
    client.set_project_notes(project["id"], "first")
    client.set_project_notes(project["id"], "second")

    assert client.get_project_notes(project["id"]) == "second"
    assert app.artifacts.get(f"project:{project['id']}", []) == [], "no blob store at all"


def test_a_backend_without_the_column_is_caught_not_believed(client, app):
    """`ProjectPatch` does not forbid extra fields, so a server predating 0094 takes
    `notes`, drops it, and answers 200. Without the read-back the caller is told a
    write succeeded that vanished -- the same silent-drop shape as unknown args
    being ignored rather than rejected."""
    app.stores_project_notes = False
    project = client.create_project("old-backend")

    with pytest.raises(errors.RosError, match="did not store the notes"):
        client.set_project_notes(project["id"], "lost")

    assert client.get_project_notes(project["id"]) is None


def test_an_async_write_is_not_verified_because_it_has_not_been_sent(app, tmp_path):
    """Journaled writes have not reached the server, so a read-back would report a
    failure for a write that is merely still in the outbox."""
    queued = make_client(app, tmp_spool=tmp_path / "spool", async_writes=True)
    project = make_client(app).create_project("queued")

    assert queued.set_project_notes(project["id"], "later") == "later"


# -- MCP ----------------------------------------------------------------------


def test_the_project_card_carries_the_notes_without_being_asked(client):
    """An agent orients with browse_research and a card; a briefing it has to KNOW
    to ask for is one it does not read."""
    project = client.create_project("surfaced")
    client.set_project_notes(project["id"], "harbor has no generic-k8s backend")

    card = _service(client).get_entity(f"project:{project['id']}")

    assert card["data"]["notes"]["text"] == "harbor has no generic-k8s backend"
    assert "truncated" not in card["data"]["notes"]
    assert card["completeness"]["state"] == "complete"


def test_a_long_document_is_excerpted_on_the_card_and_whole_in_the_view(client):
    """A card is the cheap glance, and `_ViewData(rows=None)` is atomic -- nothing
    downstream would truncate an unbounded document."""
    project = client.create_project("long")
    body = "x" * 5000
    client.set_project_notes(project["id"], body)
    service = _service(client)

    card = service.get_entity(f"project:{project['id']}")
    assert card["data"]["notes"]["truncated"] is True
    assert len(card["data"]["notes"]["text"]) < len(body)
    assert card["data"]["notes"]["read_all"] == 'view="notes"'

    assert service.get_entity(f"project:{project['id']}", view="notes")["data"]["notes"] == body


def test_a_project_with_no_notes_says_nothing_rather_than_claiming_empty(client):
    project = client.create_project("blank")
    card = _service(client).get_entity(f"project:{project['id']}")
    assert "notes" not in card["data"]
    assert _service(client).get_entity(
        f"project:{project['id']}", view="notes"
    )["data"]["notes"] is None


def test_the_card_costs_no_extra_request(client, app):
    """The notes come off the row `source.get` already fetched. The artifact version
    paid three round trips here (list -> presign -> R2 GET) on the cheapest, most-used
    read in the tool; the column pays none."""
    project = client.create_project("free")
    client.set_project_notes(project["id"], "free of charge")

    before = len(app.requests)
    card = _service(client).get_entity(f"project:{project['id']}")
    reads = [
        r for r in app.requests[before:]
        if r.method == "GET" and r.url.path == f"/v1/projects/{project['id']}"
    ]

    assert card["data"]["notes"]["text"] == "free of charge"
    assert len(reads) == 1, "the entity fetch IS the notes fetch"
    # And nothing went near the blob store, which is what the artifact version did.
    assert not [r for r in app.requests[before:] if "artifacts" in r.url.path]


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
    assert "wrote notes on project p" in capsys.readouterr().err

    assert cli.main(["notes", "show"]) == 0
    assert capsys.readouterr().out == "decided on GKE\n"


def test_notes_append_does_not_clobber_the_other_agents_paragraph(wired, capsys, monkeypatch):
    """A plain write is last-one-wins, server-side too. With two agents on one
    project that silently deletes the other's text, so append re-reads first."""
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
    assert "has no notes yet" in out.err


def test_notes_without_a_project_says_what_to_do(app, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        cli, "Client", lambda **_kw: make_client(app, tmp_spool=tmp_path / "spool")
    )
    assert cli.main(["notes", "show"]) != 0
    assert "probe project use" in capsys.readouterr().err
