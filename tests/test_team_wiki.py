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


def _seed_pages(app, *pages: tuple[str, str, str]) -> None:
    """Put `(type, slug, body)` triples in the fake, through its commit path so
    version and history are consistent with what a real write would leave."""
    for wiki_type, slug, body in pages:
        app._wiki_page_commit(wiki_type, slug, body, slug.replace("-", " "))


def test_the_bare_wiki_ref_is_the_front_page(client, app):
    """`ref="wiki"` still answers, and still with one markdown body. It is the
    orienting read every agent is told to make first, so it must not become
    "list the pages, then pick one" -- that is two calls to answer the question
    "what does this lab work on"."""
    app.wiki_index = {
        "body": "# lab\nWe train SQL agents.\n",
        "entries": [],
        "updated_at": "2026-08-05T04:00:00Z",
        "version": 4,
    }
    data = _service(client).get_entity("wiki")["data"]

    assert data["entity_type"] == "wiki"
    assert data["entity"]["body"] == "# lab\nWe train SQL agents.\n"


def test_a_page_ref_opens_one_page(client, app):
    """`wiki:<type>/<slug>` used to be REFUSED -- one document per tenant meant
    an id could only be a misconception. Pages made the value meaningful, so it
    resolves now."""
    _seed_pages(app, ("runbook", "slack-stuck", "Ping the on-call.\n"))

    data = _service(client).get_entity("wiki:runbook/slack-stuck")["data"]

    assert data["entity"]["body"] == "Ping the on-call.\n"
    assert data["entity"]["slug"] == "slack-stuck"


def test_a_ref_that_is_not_a_page_is_refused_rather_than_guessed_at(client):
    """The value has a shape now, and anything else is still refused. Guessing
    would mean answering successfully with the wrong page, which is worse than
    an error the caller can read."""
    with pytest.raises(errors.ValidationError, match="does not name a wiki page"):
        _service(client).get_entity("wiki:00000000-0000-0000-0000-000000000001")

    with pytest.raises(errors.ValidationError, match="does not name a wiki page"):
        _service(client).get_entity("wiki:runbook")


def test_the_pages_view_lists_every_page_without_bodies(client, app):
    """The wiki's table of contents. A view rather than a second tool: an agent
    that read the front page and wants what is behind one sentence of it is
    still asking about the same entity."""
    _seed_pages(
        app,
        ("runbook", "slack-stuck", "x" * 500),
        ("repo", "prbe-knowledge", "y" * 500),
    )
    data = _service(client).get_entity("wiki", view="pages")["data"]

    assert data["count"] == 2
    assert {row["slug"] for row in data["pages"]} == {"slack-stuck", "prbe-knowledge"}
    # The saving is the whole point of the view existing.
    assert "xxxxx" not in str(data) and "yyyyy" not in str(data)


def test_an_empty_wiki_says_so_instead_of_looking_like_a_failed_read(client):
    """A blank body with no explanation is indistinguishable from a read that
    returned nothing."""
    card = _service(client).get_entity("wiki")

    assert "no wiki pages yet" in card["data"]["note"]
    assert card["completeness"]["state"] == "complete", "empty is an ANSWER, not a gap"


def test_pages_behind_a_blank_front_page_are_not_reported_as_an_empty_wiki(client, app):
    """THE distinction that decides what the agent does next.

    A team can have real pages and no front page -- the nightly run writes the
    overview last. An agent told only "empty" stops there and re-derives
    everything those pages already say."""
    _seed_pages(app, ("runbook", "slack-stuck", "Ping the on-call.\n"))
    app.wiki_index = {
        "body": "",
        "entries": [{"wiki_type": "runbook", "slug": "slack-stuck"}],
        "updated_at": None,
        "version": None,
    }
    note = _service(client).get_entity("wiki")["data"]["note"]

    assert "1 page(s)" in note
    assert 'view="pages"' in note, "and it must name the call that shows them"


def test_the_mcp_never_offers_a_write(client):
    """READ-ONLY by design: writes are CLI/SDK only. A write view here would be
    the one door in this server that changes tenant state."""
    wiki_views = {view for kind, view in _VIEWS if kind == EntityType.WIKI}
    assert wiki_views == {View.CARD, View.VERSIONS, View.PAGES}

    with pytest.raises(errors.ValidationError, match="not available for a wiki"):
        _service(client).get_entity("wiki", view="notes")


# -- the SDK ------------------------------------------------------------------


def test_a_page_round_trips_through_the_sdk(client):
    created = client.set_wiki_page(
        "runbook", "slack-stuck", title="Slack stuck", body="Ping the on-call.\n", version=0
    )
    assert created["version"] == 1

    assert client.get_wiki_page("runbook", "slack-stuck")["body"] == "Ping the on-call.\n"
    assert client.list_wiki_pages()["count"] == 1


def test_a_stale_page_write_loses_and_is_told_what_it_lost_to(client, app):
    """Losing is ORDINARY here, not exceptional: the other writer is usually the
    nightly synthesis run, which no caller can see coming. So the 409 carries the
    body, and the loser can merge without a second request."""
    _seed_pages(app, ("runbook", "slack-stuck", "the agent's version\n"))

    with pytest.raises(errors.ConflictError) as caught:
        client.set_wiki_page(
            "runbook", "slack-stuck", title="t", body="mine\n", version=0
        )

    detail = caught.value.detail
    assert detail["current_version"] == 1
    assert detail["current_body"] == "the agent's version\n"


def test_a_version_checked_page_write_is_never_journaled(app, tmp_path):
    """In `async_writes` mode the outbox would deliver a version-checked write
    minutes later, against a version that has since moved -- turning a conflict
    the caller could have merged into one it never sees. So this call bypasses
    the journal, like `set_wiki` before it."""
    c = make_client(app, tmp_spool=tmp_path / "spool", async_writes=True)
    c.set_wiki_page("runbook", "x", title="t", body="now, not later\n", version=0)

    assert app.wiki_pages["runbook/x"]["body"] == "now, not later\n"


def test_a_backend_without_pages_says_upgrade_rather_than_not_found(client, app):
    """"Not Found" on a wiki a caller was told always exists reads as data loss.
    The message has to name the fix instead."""
    app.supports_wiki = False

    with pytest.raises(errors.NotFoundError, match="predates the multi-page team wiki"):
        client.list_wiki_pages()


def test_a_missing_page_is_a_real_404_not_an_upgrade_nag(client):
    """A mistyped slug is the ORDINARY case. Rewriting its 404 into "upgrade
    your server" would hide it behind an instruction that cannot help."""
    with pytest.raises(errors.NotFoundError) as caught:
        client.get_wiki_page("runbook", "no-such-page")

    assert "predates" not in str(caught.value)


# -- the CLI ------------------------------------------------------------------


@pytest.fixture
def wired(app, tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli, "Client", lambda **_kw: make_client(app, tmp_spool=tmp_path / "spool")
    )
    return app


def test_wiki_write_hides_the_read_then_write(wired, capsys, monkeypatch):
    """The route answers 428 without a version, so the CLI reads the current one
    and writes with it in ONE command. The alternative is teaching every caller
    to make the read call first, which is a precondition they eventually skip."""
    import json as _json

    _seed_pages(wired, ("runbook", "slack-stuck", "old\n"))
    wired.requests.clear()
    monkeypatch.setattr("sys.stdin", io.StringIO("new\n"))

    assert cli.main(["wiki", "write", "runbook/slack-stuck"]) == 0

    calls = [(r.method, r.url.path) for r in wired.requests if "/v1/wiki" in r.url.path]
    assert calls == [
        ("GET", "/v1/wiki/pages/runbook/slack-stuck"),
        ("PUT", "/v1/wiki/pages/runbook/slack-stuck"),
    ], "the read is the hidden half"
    put = next(r for r in wired.requests if r.method == "PUT")
    assert _json.loads(put.content)["version"] == 1, "and its answer is what the write asserts"
    assert wired.wiki_pages["runbook/slack-stuck"]["body"] == "new\n"


def test_writing_a_page_that_does_not_exist_yet_creates_it(wired, capsys, monkeypatch):
    """A page that does not exist reads as 404, and version 0 is how a create
    asserts exactly that. Without this branch, seeding a new page would need a
    different command from editing an old one."""
    monkeypatch.setattr("sys.stdin", io.StringIO("brand new\n"))

    assert cli.main(["wiki", "write", "runbook/fresh"]) == 0
    assert wired.wiki_pages["runbook/fresh"]["body"] == "brand new\n"
    assert wired.wiki_pages["runbook/fresh"]["version"] == 1
    capsys.readouterr()


def test_wiki_write_accepts_literal_text_a_file_and_stdin(wired, capsys, tmp_path, monkeypatch):
    """Same three input shapes as `notes write`, through the same `_text_value`."""
    assert cli.main(["wiki", "write", "runbook/x", "literal"]) == 0
    assert wired.wiki_pages["runbook/x"]["body"] == "literal"

    document = tmp_path / "wiki.md"
    document.write_text("from a file")
    assert cli.main(["wiki", "write", "runbook/x", f"@{document}"]) == 0
    assert wired.wiki_pages["runbook/x"]["body"] == "from a file"

    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    assert cli.main(["wiki", "write", "runbook/x", "-"]) == 0
    assert wired.wiki_pages["runbook/x"]["body"] == "from stdin"
    capsys.readouterr()


def test_a_malformed_page_ref_names_the_shape_it_wanted(wired, capsys):
    """`probe wiki read runbook` is the mistake a reader actually makes. The
    error has to say what a page looks like AND how to find one."""
    assert cli.main(["wiki", "read", "runbook"]) == 2

    err = capsys.readouterr().err
    assert "<type>/<slug>" in err
    assert "probe wiki list" in err


def test_wiki_read_with_no_argument_prints_the_front_page(wired, capsys):
    """THE common case stays one word. An agent orienting itself types
    `probe wiki read` and gets prose, not a list it then has to navigate."""
    wired.wiki_index = {
        "body": "# lab\nWe train SQL agents.\n",
        "entries": [],
        "updated_at": "2026-08-05T04:00:00Z",
        "version": 2,
    }
    assert cli.main(["wiki", "read"]) == 0
    assert capsys.readouterr().out == "# lab\nWe train SQL agents.\n"


def test_wiki_read_prints_one_page(wired, capsys):
    _seed_pages(wired, ("runbook", "slack-stuck", "Ping the on-call.\n"))

    assert cli.main(["wiki", "read", "runbook/slack-stuck"]) == 0
    assert capsys.readouterr().out == "Ping the on-call.\n"


def test_wiki_read_on_an_empty_team_says_so_on_stderr(wired, capsys):
    """Not an error and not silence: an empty wiki and a failed read look
    identical on stdout, and the difference decides whether the reader writes a
    page or files a bug."""
    assert cli.main(["wiki", "read"]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no front page yet" in captured.err


def test_wiki_list_on_an_empty_team_says_so_on_stderr(wired, capsys):
    assert cli.main(["wiki", "list"]) == 0

    captured = capsys.readouterr()
    assert "no pages yet" in captured.err
    assert '"pages": []' in captured.out or '"pages":[]' in captured.out


def test_a_losing_write_reports_the_conflict_and_changes_nothing(wired, capsys):
    """Everything goes to STDERR and stdout stays empty. A successful write
    prints a JSON receipt on stdout, so putting the other writer's markdown
    there would let `probe wiki write ... > out.json` produce a plausible file
    for a write that never happened."""
    _seed_pages(wired, ("runbook", "slack-stuck", "the nightly run's text\n"))

    assert cli.main(["wiki", "write", "runbook/slack-stuck", "mine", "--if-version", "99"]) == 1

    captured = capsys.readouterr()
    assert captured.out == "", "a lost write must not print a receipt"
    assert "NOTHING WAS WRITTEN" in captured.err
    assert "the nightly run's text" in captured.err, "the loser is handed what it lost to"
    assert "--if-version 1" in captured.err, "and told how to retry"
    assert wired.wiki_pages["runbook/slack-stuck"]["body"] == "the nightly run's text\n"


def test_if_version_asserts_a_precondition_the_caller_already_holds(wired, capsys):
    """The read is SKIPPED when the caller supplied a version: making it anyway
    would spend a request to fetch a number we are about to ignore, and would
    read as though the flag were advisory."""
    _seed_pages(wired, ("runbook", "x", "one\n"))
    wired.requests.clear()

    assert cli.main(["wiki", "write", "runbook/x", "two", "--if-version", "1"]) == 0

    calls = [(r.method, r.url.path) for r in wired.requests if "/v1/wiki" in r.url.path]
    assert calls == [("PUT", "/v1/wiki/pages/runbook/x")], "no hidden read"
    capsys.readouterr()


def test_wiki_versions_and_revert_from_the_cli(wired, capsys):
    _seed_pages(wired, ("runbook", "x", "one\n"))
    cli.main(["wiki", "write", "runbook/x", "two", "--if-version", "1"])
    capsys.readouterr()

    assert cli.main(["wiki", "versions", "runbook/x"]) == 0
    assert '"version": 2' in capsys.readouterr().out

    assert cli.main(["wiki", "revert", "runbook/x", "--to", "1"]) == 0
    out = capsys.readouterr()
    assert '"reverted_to": 1' in out.out
    # FORWARD, never in place: the revert is itself a new version.
    assert wired.wiki_pages["runbook/x"]["version"] == 3


def test_the_cli_on_a_pre_pages_backend_names_the_fix(wired, capsys):
    """"Not Found" on a wiki the CLI just told you to read is the worst
    available message. It has to name the upgrade instead."""
    wired.supports_wiki = False

    assert cli.main(["wiki", "list"]) != 0
    assert "predates the multi-page team wiki" in capsys.readouterr().err
