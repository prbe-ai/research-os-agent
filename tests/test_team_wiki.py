"""The team wiki's CLIENT surfaces: generated models, MCP reads, SDK, CLI.

The backend half of this feature (research-os 0098) is a document nobody can
reach. This file guards the half that decides whether the feature works at all,
and it is organised around the four ways it could silently not:

  * A WIDENED RESPONSE KILLS THE SECTION. `GET /v1/browse` grew an optional
    `wiki` field. If the generated models had not been refreshed -- or if the
    field had landed required -- the browse read would raise instead of
    degrading, and browse is the most-used call in the product. Both directions
    are pinned below (new field against this client; NO field, an older backend,
    against this client), because either one alone proves nothing: a client that
    rejects the new field and a client that requires it both pass a single-
    direction test.
  * THE EXCERPT DOES NOT RIDE ALONG. A briefing an agent has to know to ask for
    is one it does not read, so the excerpt has to be on `browse_research` and
    it has to carry a pointer written in tool vocabulary.
  * A DEGRADED READ TAKES THE TREE WITH IT. The wiki is a nice-to-have on that
    call; losing the excerpt must never cost the projects.
  * THE WRITE SILENTLY OVERWRITES. `PUT /v1/wiki` answers 428 without a version
    and 409 against a stale one. `probe wiki write` hides the read-then-write
    (or nobody would do it) WITHOUT hiding the conflict (or the version check
    buys nothing).
"""

from __future__ import annotations

import io

import pytest

from probe import cli
from probe._generated.models import BrowseResponse
from probe.mcp.contract import EntityType, MissingMarker, View
from probe.mcp.service import _VIEWS, ResearchReadService
from probe.mcp.source import ResearchOSSource
from probe.sdk import errors
from tests.conftest import make_client


def _service(client) -> ResearchReadService:
    return ResearchReadService(ResearchOSSource(client))


def _tree(**extra) -> dict:
    """A minimal top-level browse response. `extra` adds (or omits) `wiki`."""
    return {
        "projects": [
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "name": "folding",
                "slug": "folding",
                "workspace_id": None,
                "created_at": "2026-08-01T00:00:00Z",
                "updated_at": "2026-08-01T00:00:00Z",
                "experiment_count": 1,
                "active_run_count": 0,
                "direct_run_count": 0,
                "experiments": None,
            }
        ],
        "cursor": None,
        "depth": 1,
        "limit": 50,
        "truncated": False,
        **extra,
    }


_EXCERPT = {
    "state": "ok",
    "text": "# lab\n\nHarbor has no generic-k8s backend. Do not re-try DOKS.",
    "truncated": False,
    "read_all": "GET /v1/wiki",
}


# -- T8: the generated models, BOTH directions --------------------------------
# The trap this feature was flagged for: a widened response makes an older
# generated model raise, which kills the whole section rather than degrading it.


def test_a_browse_payload_carrying_the_new_wiki_field_is_accepted(client, app):
    """FORWARD compatibility: this client, a backend that HAS the wiki.

    The direction that fails when the models are stale. Asserted at both layers
    that could reject it -- the generated model, which is where a required or
    unknown-forbidding field would raise, and the live client call, which is the
    thing agents actually go through.
    """
    payload = _tree(wiki=_EXCERPT)

    assert BrowseResponse.model_validate(payload).wiki.text == _EXCERPT["text"]

    app.browse_response = payload
    tree = client.browse()
    assert tree["wiki"]["text"] == _EXCERPT["text"]
    assert len(tree["projects"]) == 1


def test_a_browse_payload_with_no_wiki_field_at_all_is_accepted(client, app):
    """BACKWARD compatibility: this client, a backend that predates the wiki.

    The converse, and NOT redundant with the test above. That one would still
    pass if `wiki` had shipped required; this one is the only thing that catches
    it. Between them they pin the field as genuinely optional in both
    directions, which is what makes shipping the two halves independently safe.
    """
    payload = _tree()
    assert "wiki" not in payload

    assert BrowseResponse.model_validate(payload).wiki is None

    app.supports_wiki = False
    app.browse_response = payload
    tree = client.browse()
    assert tree.get("wiki") is None
    assert len(tree["projects"]) == 1


def test_the_mcp_browse_reads_both_shapes_without_raising(client, app):
    """The same two directions one layer up.

    The models are only half the story: `browse_research` reaches into the
    payload, and reaching into an absent key is the other way this breaks. An
    old backend must produce a tree with no wiki, not a KeyError.
    """
    service = _service(client)

    app.browse_response = _tree(wiki=_EXCERPT)
    assert service.browse_research()["data"]["wiki"]["text"] == _EXCERPT["text"]

    app.browse_response = _tree()
    old = service.browse_research()
    assert "wiki" not in old["data"]
    assert old["completeness"]["state"] == "complete"
    assert len(old["data"]["projects"]) == 1


# -- T8: the excerpt rides on the orientation call ----------------------------


def test_the_orientation_call_carries_the_wiki_without_being_asked(client, app):
    """THE WHOLE POINT OF THE FEATURE. `browse_research` is the call an agent
    makes on arrival; a briefing behind a view it has to know to ask for is one
    it does not read."""
    app.browse_response = _tree(wiki=_EXCERPT)

    data = _service(client).browse_research()["data"]

    assert data["wiki"]["text"] == _EXCERPT["text"]
    assert data["projects"], "the tree still arrives alongside it"


def test_the_pointer_is_rewritten_into_tool_vocabulary(client, app):
    """The backend names its own surface (`GET /v1/wiki`), correctly. An agent
    holding this envelope has no HTTP client, so the pointer has to name a tool
    it can actually call -- otherwise it is a dead end that reads like a door."""
    app.browse_response = _tree(wiki=_EXCERPT)

    wiki = _service(client).browse_research()["data"]["wiki"]

    assert wiki["read_all"] == 'get_entity(ref="wiki")'
    assert "GET /v1/wiki" not in wiki["read_all"]


def test_a_truncated_excerpt_says_so(client, app):
    """`truncated` is passed through rather than inferred from the text length: a
    document that happens to be exactly the bound is NOT truncated, and only the
    producer can tell which one this is."""
    app.browse_response = _tree(wiki={**_EXCERPT, "truncated": True})

    assert _service(client).browse_research()["data"]["wiki"]["truncated"] is True


def test_a_scoped_browse_carries_no_wiki_and_claims_nothing(client, app):
    """The wiki belongs to the TENANT, not to the project you scoped into. The
    backend omits it there; the client must not fill the gap with an empty one,
    which would read as 'this team has written nothing down'."""
    app.browse_response = _tree()

    scoped = _service(client).browse_research(scope="project:11111111-1111-1111-1111-111111111111")

    assert "wiki" not in scoped["data"]


# -- T8: degrade, never raise -------------------------------------------------


def test_an_unavailable_wiki_still_returns_the_tree(client, app):
    """The excerpt is a nice-to-have riding along with the most-used read in the
    product. A wiki read that failed must cost the excerpt and NOTHING else."""
    app.browse_response = _tree(
        wiki={"state": "unavailable", "text": None, "truncated": False, "read_all": "GET /v1/wiki"}
    )

    envelope = _service(client).browse_research()

    assert len(envelope["data"]["projects"]) == 1, "the tree survived"
    assert envelope["data"]["wiki"]["state"] == "unavailable"
    assert envelope["completeness"]["state"] == "partial"
    assert MissingMarker.TEAM_WIKI in envelope["completeness"]["missing"]


def test_an_absent_wiki_is_not_reported_as_a_gap(client, app):
    """`unavailable` and absent are OPPOSITE claims and only one of them is a
    degradation. Marking every wiki-less browse partial would put a permanent
    'degraded' flag on the product's most-used read, which trains agents to
    ignore the signal entirely."""
    app.browse_response = _tree()

    envelope = _service(client).browse_research()

    assert envelope["completeness"]["state"] == "complete"
    assert MissingMarker.TEAM_WIKI not in envelope["completeness"]["missing"]


def test_a_malformed_excerpt_is_dropped_rather_than_propagated(client, app):
    """There is no shape of broken excerpt worth losing the tree over."""
    app.browse_response = _tree(wiki="not a dict")

    envelope = _service(client).browse_research()

    assert "wiki" not in envelope["data"]
    assert len(envelope["data"]["projects"]) == 1


# -- T8: get_entity(ref="wiki") -----------------------------------------------


def test_the_wiki_ref_takes_no_id(client, app):
    """A singleton: one document per tenant, and the credential names the tenant.
    `wiki:<id>` is refused rather than ignored, because silently dropping the id
    would confirm the caller's misconception by answering successfully."""
    app.wiki = {"body": "# lab", "version": 4, "updated_at": "2026-08-05T04:00:00Z"}
    service = _service(client)

    assert service.get_entity("wiki")["data"]["entity"]["body"] == "# lab"

    with pytest.raises(errors.ValidationError, match="takes no id"):
        service.get_entity("wiki:00000000-0000-0000-0000-000000000001")


def test_the_bare_wiki_ref_is_not_mistaken_for_an_id(client):
    """`ref="wiki"` has no colon, so the bare-ref branch would take it for a
    UUID and answer 'a bare ref must be a UUID' -- true, useless, and aimed at a
    caller who followed the tool description exactly."""
    assert _service(client).get_entity("wiki")["data"]["entity_type"] == "wiki"


def test_the_card_is_the_whole_document_not_an_excerpt(client, app):
    """The excerpt already rode along on browse. An agent that asks HERE has seen
    it and wants the rest, so a second excerpt would cost a third call."""
    body = "# lab\n" + ("detail line\n" * 400)
    app.wiki = {"body": body, "version": 9, "updated_at": "2026-08-05T04:00:00Z"}

    card = _service(client).get_entity("wiki", token_budget=100_000)

    assert card["data"]["entity"]["body"] == body
    assert card["data"]["available_views"] == ["card", "versions"]


def test_an_empty_wiki_says_so_instead_of_looking_like_a_failed_read(client):
    """`GET /v1/wiki` answers `{body: "", version: 0}` for a team that never had
    one -- never a 404. A blank body with no explanation is indistinguishable
    from a read that returned nothing."""
    card = _service(client).get_entity("wiki")

    assert card["data"]["entity"] == {"body": "", "version": 0, "updated_at": None}
    assert "no wiki yet" in card["data"]["note"]
    assert card["completeness"]["state"] == "complete", "empty is an ANSWER, not a gap"


def test_the_versions_view_reuses_the_existing_vocabulary(client):
    """`versions` already means 'the history of this thing' for artifacts and
    experiments. A wiki-specific spelling would be a second word for one idea."""
    assert (EntityType.WIKI, View.VERSIONS) in _VIEWS
    assert "versions" in _service(client).get_entity("wiki")["data"]["available_views"]


def test_the_history_carries_no_bodies_not_even_the_current_one(client, app):
    """`WikiVersionOut` drops the body deliberately -- a 50-row page of 20k
    documents is a megabyte. `get_entity` puts the resolved entity in every
    response, and for a wiki that entity IS the current document, so the view
    has to override it or it would undo the saving at the last step on the one
    view designed to avoid it."""
    app.wiki = {"body": "SECRET-CURRENT-BODY", "version": 2, "updated_at": "2026-08-05T00:00:00Z"}
    app.wiki_history = [
        {"version": 2, "author": "agent:wiki", "summary": "nightly",
         "created_at": "2026-08-05T04:00:00Z", "size_chars": 19},
        {"version": 1, "author": "user:abc", "summary": None,
         "created_at": "2026-08-04T09:00:00Z", "size_chars": 12},
    ]

    data = _service(client).get_entity("wiki", view="versions")["data"]

    assert "SECRET-CURRENT-BODY" not in str(data)
    assert data["entity"] == {"version": 2, "updated_at": "2026-08-05T00:00:00Z"}
    assert data["current_version"] == 2
    assert [row["version"] for row in data["versions"]] == [2, 1]
    assert [row["author"] for row in data["versions"]] == ["agent:wiki", "user:abc"]


def test_older_history_beyond_the_page_is_named_rather_than_hidden(client, app):
    """The backend pages by `before_version` (a keyset) and this tool's cursor is
    an offset into the rows a view returned. The two cannot be the same number,
    so the view serves the newest page and SAYS the rest exists -- a full page
    that read as the whole history would be a confident wrong answer."""
    app.wiki = {"body": "x", "version": 300, "updated_at": "2026-08-05T00:00:00Z"}
    app.wiki_history = [
        {"version": v, "author": "agent:wiki", "summary": None,
         "created_at": "2026-08-05T04:00:00Z", "size_chars": 10}
        for v in range(300, 0, -1)
    ]

    envelope = _service(client).get_entity("wiki", view="versions", token_budget=100_000)

    assert MissingMarker.WIKI_VERSIONS_BEYOND_PAGE in envelope["completeness"]["missing"]
    assert envelope["completeness"]["state"] == "partial"


def test_a_short_history_is_not_reported_as_truncated(client, app):
    """The marker is emitted from what the backend actually reported, never
    inferred: a false 'there is more' is the same class of lie as hiding a real
    one."""
    app.wiki = {"body": "x", "version": 1, "updated_at": "2026-08-05T00:00:00Z"}
    app.wiki_history = [
        {"version": 1, "author": "agent:wiki", "summary": None,
         "created_at": "2026-08-05T04:00:00Z", "size_chars": 1}
    ]

    envelope = _service(client).get_entity("wiki", view="versions")

    assert envelope["completeness"]["missing"] == []
    assert envelope["completeness"]["state"] == "complete"


def test_the_mcp_never_offers_a_write(client):
    """READ-ONLY by design: writes are CLI/SDK only. A write view here would be
    the one door in this server that changes tenant state."""
    wiki_views = {view for kind, view in _VIEWS if kind == EntityType.WIKI}
    assert wiki_views == {View.CARD, View.VERSIONS}

    with pytest.raises(errors.ValidationError, match="not available for a wiki"):
        _service(client).get_entity("wiki", view="notes")


# -- T9: the SDK --------------------------------------------------------------


def test_the_document_round_trips_through_the_sdk(client):
    page = client.get_wiki()
    assert page == {"body": "", "version": 0, "updated_at": None}

    written = client.set_wiki("# lab\n\nGKE, not DOKS.\n", page["version"], summary="seed")

    assert written["version"] == 1
    assert client.get_wiki()["body"] == "# lab\n\nGKE, not DOKS.\n"


def test_a_stale_write_loses_and_is_told_what_it_lost_to(client, app):
    """Losing is ORDINARY here -- the other writer is usually the nightly sweep,
    which no caller can see coming -- so the 409 carries the current body and the
    loser can merge without a second request."""
    client.set_wiki("first", 0)
    app.wiki["body"] = "the sweep got here first"
    app.wiki["version"] = 7

    with pytest.raises(errors.ConflictError) as caught:
        client.set_wiki("mine", 1)

    assert caught.value.detail["current_version"] == 7
    assert caught.value.detail["current_body"] == "the sweep got here first"
    assert app.wiki["body"] == "the sweep got here first", "nothing was overwritten"


def test_a_version_checked_write_is_never_journaled(app, tmp_path):
    """The one write in this client that does NOT go through `Client.write`.

    In async mode `write` journals and returns None, so the outbox would deliver
    a version-checked write minutes later against a version that has moved, and
    hand the 409 to a drainer with nobody to merge it. A precondition checked
    after the fact is not a precondition.
    """
    queued = make_client(app, tmp_spool=tmp_path / "spool", async_writes=True)

    assert queued.set_wiki("straight to the wire", 0)["version"] == 1
    assert app.wiki["body"] == "straight to the wire"


def test_the_history_list_carries_no_bodies(client):
    client.set_wiki("a" * 40, 0, summary="first")
    client.set_wiki("b" * 10, 1)

    rows = client.wiki_versions()["versions"]

    assert [r["version"] for r in rows] == [2, 1], "newest first"
    assert [r["size_chars"] for r in rows] == [10, 40]
    assert [r["summary"] for r in rows] == [None, "first"]
    assert not any("body" in r for r in rows)


def test_a_revert_copies_forward_rather_than_rewinding(client):
    """History is never rewritten: reverting to 1 does not delete 2, it appends
    1's body as 3. So a revert is itself revertible."""
    client.set_wiki("original", 0)
    client.set_wiki("replacement", 1)

    reverted = client.revert_wiki(1)

    assert reverted == {"body": "original", "version": 3, "updated_at": reverted["updated_at"]}
    assert [r["version"] for r in client.wiki_versions()["versions"]] == [3, 2, 1]


def test_a_backend_without_the_wiki_says_upgrade_rather_than_not_found(client, app):
    """No wiki route can 404 for a data reason -- `GET /v1/wiki` answers an empty
    document for a team that has never had one, and none of the paths carry an id
    to be wrong about. So a bare 'Not Found' on a document the caller was told
    always exists reads as data loss."""
    app.supports_wiki = False

    for call in (
        lambda: client.get_wiki(),
        lambda: client.set_wiki("x", 0),
        lambda: client.wiki_versions(),
        lambda: client.revert_wiki(1),
    ):
        with pytest.raises(errors.NotFoundError, match="predates the team wiki"):
            call()


def test_reverting_to_a_version_that_does_not_exist_is_not_an_upgrade_nag(client):
    """The revert 404 is the one AMBIGUOUS one -- absent version, or absent
    feature -- so it is resolved rather than assumed. Telling someone to upgrade
    a server that is already current sends them to fix the wrong thing."""
    client.set_wiki("only version", 0)

    with pytest.raises(errors.NotFoundError) as caught:
        client.revert_wiki(99)

    assert "predates the team wiki" not in str(caught.value)
    assert "99" in str(caught.value)


def test_an_oversized_document_fails_client_side(client):
    """The 20,000-character cap is in the generated model, so it fails before the
    request leaves rather than as a server 422."""
    with pytest.raises(Exception, match="at most 20000"):
        client.set_wiki("x" * 20_001, 0)


# -- T9: the CLI --------------------------------------------------------------


@pytest.fixture
def wired(app, tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli, "Client", lambda **_kw: make_client(app, tmp_spool=tmp_path / "spool")
    )
    return app


def test_wiki_write_hides_the_read_then_write(wired, capsys, monkeypatch):
    """`PUT /v1/wiki` answers 428 without a version, so the CLI reads the current
    one and writes with it in ONE command. The alternative is teaching every
    caller to make the read call first, which is a precondition they eventually
    skip."""
    import json as _json

    monkeypatch.setattr("sys.stdin", io.StringIO("# lab\n\nGKE, not DOKS.\n"))

    assert cli.main(["wiki", "write"]) == 0

    calls = [(r.method, r.url.path) for r in wired.requests if r.url.path.startswith("/v1/wiki")]
    assert calls == [("GET", "/v1/wiki"), ("PUT", "/v1/wiki")], "the read is the hidden half"
    put = next(r for r in wired.requests if r.method == "PUT")
    assert _json.loads(put.content)["version"] == 0, "and its answer is what the write asserts"
    assert wired.wiki["version"] == 1
    assert "wrote the team wiki (version 1)" in capsys.readouterr().err


def test_wiki_write_accepts_literal_text_a_file_and_stdin(wired, capsys, tmp_path, monkeypatch):
    """Same three input shapes as `notes write`, through the same `_text_value`."""
    assert cli.main(["wiki", "write", "literal"]) == 0
    assert wired.wiki["body"] == "literal"

    document = tmp_path / "wiki.md"
    document.write_text("from a file")
    assert cli.main(["wiki", "write", f"@{document}"]) == 0
    assert wired.wiki["body"] == "from a file"

    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    assert cli.main(["wiki", "write", "-"]) == 0
    assert wired.wiki["body"] == "from stdin"
    capsys.readouterr()


def test_wiki_read_prints_the_document(wired, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("decided on GKE\n"))
    cli.main(["wiki", "write"])
    capsys.readouterr()

    assert cli.main(["wiki", "read"]) == 0
    assert capsys.readouterr().out == "decided on GKE\n"


def test_wiki_read_on_an_empty_team_says_so_on_stderr(wired, capsys):
    """Version 0 is a real answer. Printing nothing at all would leave a caller
    deciding whether to seed the document unable to tell it from a failed read."""
    assert cli.main(["wiki", "read"]) == 0

    out = capsys.readouterr()
    assert out.out == ""
    assert "empty (version 0)" in out.err


def test_a_losing_write_reports_the_conflict_and_changes_nothing(wired, capsys):
    """The hidden read-then-write must NOT hide the conflict. Silently retrying
    with the newer version would be the same last-one-wins overwrite the version
    check exists to prevent, done automatically."""
    cli.main(["wiki", "write", "mine"])
    capsys.readouterr()
    wired.wiki = {"body": "the sweep wrote this", "version": 12,
                  "updated_at": "2026-08-05T04:00:00Z"}

    assert cli.main(["wiki", "write", "mine again", "--if-version", "1"]) == 1

    out = capsys.readouterr()
    assert out.out == "", "stdout is the success receipt; a failed write writes none"
    assert "moved to version 12" in out.err
    assert "NOTHING WAS WRITTEN" in out.err
    assert "the sweep wrote this" in out.err, "show them what it moved to"
    assert "--if-version 12" in out.err, "and how to write again"
    assert wired.wiki["body"] == "the sweep wrote this"


def test_if_version_asserts_a_precondition_the_caller_already_holds(wired, capsys):
    """Without it the CLI's fresh read narrows the race to milliseconds but does
    not remove it, and a caller that read the document EARLIER -- merged a
    conflict, edited by hand -- has a real precondition and no way to say so."""
    cli.main(["wiki", "write", "one"])
    cli.main(["wiki", "write", "two"])
    capsys.readouterr()

    assert cli.main(["wiki", "write", "three", "--if-version", "1"]) == 1
    assert wired.wiki["body"] == "two"

    assert cli.main(["wiki", "write", "three", "--if-version", "2"]) == 0
    assert wired.wiki["body"] == "three"


def test_wiki_versions_and_revert_from_the_cli(wired, capsys):
    import json

    cli.main(["wiki", "write", "first", "--summary", "seed"])
    cli.main(["wiki", "write", "second"])
    capsys.readouterr()

    assert cli.main(["wiki", "versions"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [r["version"] for r in listed["versions"]] == [2, 1]
    assert listed["versions"][1]["summary"] == "seed"

    assert cli.main(["wiki", "revert", "1"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt == {"version": 3, "reverted_to": 1}
    assert wired.wiki["body"] == "first"


def test_the_cli_on_a_pre_wiki_backend_names_the_fix(wired, capsys):
    wired.supports_wiki = False

    assert cli.main(["wiki", "read"]) == 1
    assert "predates the team wiki" in capsys.readouterr().err
