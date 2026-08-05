"""One rule: a bare ref is the slug, an id is written ``id:<uuid>``.

These exist because the previous rule -- accept either spelling bare and work out
which was meant -- deleted the wrong project. Observed 2026-08-04:

    A: slug ``parity-smoke-18cdeb5``  id ``6fa49e87-...-9f477c0d880e``
    B: slug ``6fa49e87-...-9f477c0d880e``  id ``17acbbb2-...-0d44b23bc558``

``probe project delete 6fa49e87-...`` meant B to the person typing it and A to
the CLI, and A is what went.

So the load-bearing test here is not that the collision is *detected*. It is that
the collision no longer EXISTS: the fixture below still builds those two
projects, and the ref that used to be ambiguous now has exactly one meaning.
"""

from __future__ import annotations

import importlib
import json
import uuid

import pytest

from probe import cli
from probe.sdk.client import Client

# `probe.cli.main` the SUBMODULE is shadowed by `probe.cli.main` the function, so
# the private resolvers have to be reached through the import system directly.
impl = importlib.import_module("probe.cli.main")

from tests.conftest import _T0, _WS_MINE

# The real pair, kept verbatim: A's id IS B's slug.
A_ID = "6fa49e87-2b72-4700-8c86-9f477c0d880e"
A_SLUG = "parity-smoke-18cdeb5"
B_ID = "17acbbb2-c63c-429b-966e-0d44b23bc558"
B_SLUG = A_ID


def _project(app, *, pid: str, slug: str, name: str | None = None) -> dict:
    row = {
        "id": pid,
        "slug": slug,
        "name": name or slug,
        "customer_id": "lab-42",
        "workspace_id": _WS_MINE,
        "description": None,
        "metadata": {},
        "created_at": _T0,
    }
    app.projects[pid] = row
    return row


@pytest.fixture
def collision(app, client, monkeypatch):
    """The two projects from the incident, and the CLI wired to the fake."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug=A_SLUG, name="Parity smoke")
    _project(app, pid=B_ID, slug=B_SLUG)
    return app


# -- the rule -----------------------------------------------------------------
def test_a_bare_ref_is_the_slug_even_when_it_is_also_an_id(collision):
    """THE regression, restated for the new rule.

    ``A_ID`` is A's id AND B's slug. Bare, it means B -- the slug -- and there is
    no case where it could mean A. The string that used to be ambiguous now has
    exactly one reading.
    """
    assert cli.main(["project", "delete", B_SLUG, "--yes"]) == 0

    assert set(collision.projects) == {A_ID}, "the slug's project should go, not the id's"


def test_the_id_form_reaches_the_other_one(collision):
    assert cli.main(["project", "delete", f"id:{A_ID}", "--yes"]) == 0

    assert set(collision.projects) == {B_ID}


def test_an_ordinary_slug_resolves(app, client, monkeypatch):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug=A_SLUG)

    assert cli.main(["project", "delete", A_SLUG, "--yes"]) == 0
    assert app.projects == {}


def test_the_slug_prefix_is_accepted_and_means_the_same_thing(collision):
    """``slug:`` says out loud what a bare ref already meant."""
    assert cli.main(["project", "delete", f"slug:{B_SLUG}", "--yes"]) == 0

    assert set(collision.projects) == {A_ID}


# -- the migration: a bare uuid stops working, LOUDLY -------------------------
def test_a_bare_uuid_that_is_an_id_says_exactly_what_to_write(app, client, monkeypatch, capsys):
    """The one cost of the rule, and it is paid in an error message.

    Nothing resolves to the wrong thing during the migration, because the old
    reading is gone -- there is no entity for a bare uuid to land on by mistake.
    """
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug=A_SLUG)

    code = cli.main(["project", "delete", A_ID, "--yes"])

    assert code != 0
    assert A_ID in app.projects, "nothing may be deleted while telling the caller to re-type"
    err = capsys.readouterr().err
    assert "it is a project ID" in err
    assert f"id:{A_ID}" in err


def test_a_bare_uuid_that_is_nothing_still_hints_at_the_prefix(app, client, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)

    assert cli.main(["project", "delete", A_ID, "--yes"]) != 0

    err = capsys.readouterr().err
    assert "no project with slug" in err and f"id:{A_ID}" in err


def test_a_missing_slug_reports_the_slug_it_looked_for(app, client, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)

    assert cli.main(["project", "delete", "nope", "--yes"]) != 0
    assert "no project with slug 'nope'" in capsys.readouterr().err


# -- the flags are gone -------------------------------------------------------
@pytest.mark.parametrize("flag", ["--by-id", "--by-slug"])
def test_the_disambiguator_flags_no_longer_exist(collision, flag):
    """They were only ever needed for a collision that can no longer be expressed.

    Two spellings for one decision was the wart; the prefix is the one that
    generalises, because a single command line takes more than one ref.
    """
    assert cli.main(["project", "delete", A_SLUG, flag, "--yes"]) != 0
    assert set(collision.projects) == {A_ID, B_ID}


def test_one_command_line_can_name_two_refs_in_different_forms(app, client, monkeypatch):
    """Why a prefix and not a `--uuid` flag: a flag cannot say WHICH ref it means."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    proj = _project(app, pid=A_ID, slug="folding")
    exp = app.seed_experiment("dockq-sweep", project_id=proj["id"])

    assert impl._project_id(client, f"id:{A_ID}") == A_ID
    assert impl._ref(client, "experiment", "dockq-sweep").id == exp["id"]


# -- absence is absence -------------------------------------------------------
def test_a_slug_past_the_old_200_row_cap_still_resolves(app, client, monkeypatch):
    """Resolution is a server-side ?slug= on a UNIQUE column, not a paged scan."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    for i in range(250):
        _project(app, pid=str(uuid.uuid4()), slug=f"filler-{i:03d}")
    _project(app, pid=A_ID, slug=A_SLUG)

    assert cli.main(["project", "delete", A_SLUG, "--yes"]) == 0
    assert A_ID not in app.projects


def test_resolution_never_scans_the_listing(collision, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("resolution must not scan list_projects")

    monkeypatch.setattr(Client, "list_projects", boom)

    assert cli.main(["project", "delete", B_SLUG, "--yes"]) == 0


def test_a_backend_that_ignores_the_slug_filter_refuses_rather_than_guesses(
    collision, app, client, monkeypatch
):
    """FastAPI DROPS a query param a route does not declare, so an engine
    predating ?slug= answers an unfiltered page. Reading that as "no slug
    matched" would turn a live project into a false absence."""
    for i in range(30):
        _project(app, pid=str(uuid.uuid4()), slug=f"filler-{i:03d}")
    original = client.transport.get

    def unfiltered(path, *, params=None):
        if path == "/v1/projects" and params and "slug" in params:
            rows = original(path, params=None)
            listing = rows.get("items", rows) if isinstance(rows, dict) else rows
            return [r for r in listing if r["id"] != B_ID]
        return original(path, params=params)

    monkeypatch.setattr(client.transport, "get", unfiltered)

    assert cli.main(["project", "delete", B_SLUG, "--yes"]) != 0
    assert A_ID in collision.projects and B_ID in collision.projects


# -- the inverse resolver -----------------------------------------------------
def test_the_slug_resolver_passes_a_bare_ref_through(collision, client):
    """A bare ref IS a slug now, so there is nothing to translate -- and absence
    stays ``run start``'s to report, with the near-miss guard that names what a
    typo was close to."""
    assert impl._project_slug(client, "never-created") == "never-created"
    assert impl._project_slug(client, A_SLUG) == A_SLUG


def test_the_slug_resolver_translates_an_id_ref(collision, client):
    assert impl._project_slug(client, f"id:{A_ID}") == A_SLUG


# -- consistency across the delete verbs --------------------------------------
def test_experiment_delete_accepts_a_slug(app, client, monkeypatch):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    exp = app.seed_experiment("ablation-7")

    assert cli.main(["experiment", "delete", "ablation-7", "--yes"]) == 0
    assert exp["id"] not in app.experiments


def test_experiment_delete_accepts_an_id_ref(app, client, monkeypatch):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    exp = app.seed_experiment("ablation-8")

    assert cli.main(["experiment", "delete", f"id:{exp['id']}", "--yes"]) == 0
    assert exp["id"] not in app.experiments


def test_run_delete_accepts_a_petname_and_deletes_by_id(app, client, monkeypatch):
    """A petname is server-minted and not UUID-shaped, so runs cannot collide and
    stay polymorphic. Asserts the canonical id reached DELETE -- the fake
    auto-creates a run for an unknown ref on GET, so an exit-code-only check
    would pass on a fiction."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    exp = app.seed_experiment("e")
    run = app._new_run(
        "aaaaaaaa-1111-4222-8333-444444444444", exp["id"], {"short_id": "tunneling-sambar-254"}
    )

    assert cli.main(["run", "delete", "tunneling-sambar-254", "--yes"]) == 0
    assert run["id"] not in app.runs
    assert "tunneling-sambar-254" not in app.runs


def test_every_delete_verb_prompts_with_the_resolved_entity(collision, monkeypatch, capsys):
    """The prompt names what will die, not the string that was typed."""
    seen: list[str] = []
    monkeypatch.setattr("typer.confirm", lambda question, **kw: seen.append(question) or True)

    assert cli.main(["project", "delete", A_SLUG]) == 0

    assert len(seen) == 1
    assert "Parity smoke" in seen[0] and A_SLUG in seen[0] and A_ID in seen[0]


# -- the non-delete ref sites carry the same rule -----------------------------
def test_run_list_resolves_an_experiment_slug(app, client, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    exp = app.seed_experiment("ablation-9")

    assert cli.main(["run", "list", "--experiment", "ablation-9"]) == 0

    seen = json.loads(capsys.readouterr().out)
    assert seen["items"] == [] or all(r["experiment_id"] == exp["id"] for r in seen["items"])


def test_an_experiment_artifact_anchor_takes_a_slug(app, client, monkeypatch):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    exp = app.seed_experiment("anchor-me")

    assert impl._anchor_id_for(client, impl.Anchor.EXPERIMENT, "anchor-me") == exp["id"]


def test_an_id_anchor_costs_no_request_at_all(app, client, monkeypatch):
    """`id:` IS the id, so the anchor path -- what an agent filing thousands of
    artifacts pays for -- does not need a round trip to confirm it."""
    _project(app, pid=A_ID, slug="anchor-proj")
    seen: list[str] = []
    original = client.transport.get
    monkeypatch.setattr(
        client.transport, "get", lambda path, **kw: (seen.append(path), original(path, **kw))[1]
    )

    assert impl._anchor_id_for(client, impl.Anchor.PROJECT, f"id:{A_ID}") == A_ID
    assert seen == [], seen


def test_a_run_anchor_is_left_alone(client):
    assert impl._anchor_id_for(client, impl.Anchor.RUN, "some-run-ref") == "some-run-ref"


def test_an_async_artifact_anchor_is_resolved_before_it_is_queued(app, client, monkeypatch):
    """The async branch returned BEFORE the anchor was resolved, so a queued write
    carried the raw ref into the journal and the drainer POSTed it minutes later."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    monkeypatch.setattr(impl, "_async_client", lambda: client)
    proj = _project(app, pid=A_ID, slug="async-anchor")
    queued: list[tuple] = []
    monkeypatch.setattr(
        impl,
        "_artifact_add_async",
        lambda anchor, anchor_id, *a, **kw: queued.append((anchor, anchor_id)),
    )
    # The root callback recomputes _conn.async_mode on every invocation, so
    # setting the attribute here would be overwritten before the command runs.
    monkeypatch.setenv("PROBE_ASYNC", "1")

    cli.main(["artifact", "add", "--uri", "s3://x/y", "--name", "n", "--project", "async-anchor"])

    assert queued and queued[0][1] == proj["id"], queued


# -- legacy rows minted before the backend reserved the prefix ----------------
def test_a_project_slugged_id_colon_foo_is_still_reachable(app, client, monkeypatch):
    """Only the FIRST prefix is consumed, so `slug:id:foo` names it. New ones
    cannot be created -- the backend reserves both prefixes -- but rows that
    predate that rule must stay addressable."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug="id:foo")

    assert cli.main(["project", "delete", "slug:id:foo", "--yes"]) == 0
    assert app.projects == {}


# -- name: the third form -----------------------------------------------------
def test_a_name_ref_resolves_when_it_picks_out_one_row(app, client, monkeypatch):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug=A_SLUG, name="Parity smoke")
    _project(app, pid=B_ID, slug="other", name="Something else")

    assert cli.main(["project", "delete", "name:Parity smoke", "--yes"]) == 0
    assert set(app.projects) == {B_ID}


def test_a_name_ref_is_case_insensitive(app, client, monkeypatch):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug=A_SLUG, name="Parity Smoke")

    assert cli.main(["project", "delete", "name:parity smoke", "--yes"]) == 0
    assert app.projects == {}


def test_a_name_matching_two_rows_is_refused_with_their_slugs(app, client, monkeypatch, capsys):
    """Names carry no uniqueness constraint, so this is a normal outcome -- which
    is exactly why it must not be ranked. The slugs are listed because the slug is
    the thing that would have been unambiguous."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug="first", name="Duplicate")
    _project(app, pid=B_ID, slug="second", name="Duplicate")

    code = cli.main(["project", "delete", "name:Duplicate", "--yes"])

    assert code != 0
    assert set(app.projects) == {A_ID, B_ID}, "an ambiguous name must delete nothing"
    err = capsys.readouterr().err
    assert "first" in err and "second" in err


def test_a_name_that_matches_nothing_says_so(app, client, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug=A_SLUG, name="Parity smoke")

    assert cli.main(["project", "delete", "name:Nothing", "--yes"]) != 0
    assert "no project named" in capsys.readouterr().err


def test_a_backend_that_ignores_the_name_filter_is_refused(app, client, monkeypatch, capsys):
    """FastAPI DROPS an undeclared query param, so an older engine answers an
    unfiltered page. Acting on row one of THAT is how a ref hits an arbitrary
    entity -- and here the drop is detectable exactly, because a genuine name
    response cannot contain a row named something else."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug=A_SLUG, name="Parity smoke")
    _project(app, pid=B_ID, slug="other", name="Something else")
    # NB: the listing goes through get_page, not get -- patching the wrong one
    # leaves the filter applied and the test passes for no reason.
    original = client.transport.get_page

    def drops_name(path, *, params=None, **kw):
        if path == "/v1/projects" and params and "name" in params:
            params = {k: v for k, v in params.items() if k != "name"}
        return original(path, params=params or None, **kw)

    monkeypatch.setattr(client.transport, "get_page", drops_name)

    code = cli.main(["project", "delete", "name:Parity smoke", "--yes"])

    assert code != 0
    assert set(app.projects) == {A_ID, B_ID}
    assert "does not support looking projects up by name" in capsys.readouterr().err


def test_an_experiment_resolves_by_name(app, client, monkeypatch):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    exp = app.seed_experiment("ablation-7")
    app.experiments[exp["id"]]["name"] = "Ablation seven"

    assert cli.main(["experiment", "delete", "name:Ablation seven", "--yes"]) == 0
    assert exp["id"] not in app.experiments


def test_run_start_can_name_the_run_slug(app, client, monkeypatch, capsys):
    """A nightly job can address the same run every time instead of chasing the
    petname the server would have minted."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    app.seed_experiment("nightly")

    assert cli.main(["run", "start", "--experiment", "nightly", "--slug", "nightly-eval"]) == 0

    rid = capsys.readouterr().out.strip()
    assert app.runs[rid]["short_id"] == "nightly-eval"


def test_run_start_without_a_slug_still_gets_a_petname(app, client, monkeypatch, capsys):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    app.seed_experiment("nightly")

    assert cli.main(["run", "start", "--experiment", "nightly"]) == 0

    rid = capsys.readouterr().out.strip()
    assert app.runs[rid]["short_id"], "the server still names it"


# -- findings from the cross-model adversarial pass ----------------------------
def test_an_explicit_uuid_shaped_slug_is_not_rewritten_as_an_id(app, client, monkeypatch, capsys):
    """THE regression of the regression.

    `from_ambient` reads a bare UUID as an id, which is right for a value the tool
    stored and catastrophically wrong for one a person typed. `resolve()` merges
    flag/env/context into one string and forgets which won, so an explicit
    `--project <uuid-shaped-slug>` was being rewritten to `id:` -- the original
    bug, reintroduced one layer up.
    """
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug=A_SLUG, name="Parity smoke")
    _project(app, pid=B_ID, slug=B_SLUG)  # slug IS A's id
    app.seed_experiment("e", project_id=B_ID)

    # B_SLUG is A_ID. Explicitly passed, it must mean B (the slug), never A.
    assert impl._ambient_project(B_SLUG) == B_SLUG


def test_the_stored_anchor_is_still_read_as_an_id(app, client, monkeypatch):
    """The other half: a bare UUID that came from the CONTEXT is machine-written
    and is still read as an id."""
    from probe.sdk.config import save_context

    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug=A_SLUG)
    save_context({"workspace": {"id": _WS_MINE, "project": A_ID}})

    assert impl._ambient_project(None) == f"id:{A_ID}"


def test_an_artifact_anchor_that_does_not_resolve_is_an_error(app, client, monkeypatch):
    """It used to swallow every failure and pass the raw ref through, on a
    "never a gate" rationale slug-default invalidates: the route types its path
    param as a UUID, so a bare UUID passed through is read BY THE SERVER as an id
    and files the upload against whichever project owns it."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug=A_SLUG)

    with pytest.raises(Exception):
        impl._anchor_id_for(client, impl.Anchor.PROJECT, B_ID)


def test_a_name_match_on_a_paged_listing_refuses(app, client, monkeypatch, capsys):
    """"Exactly one match" cannot be proven from one page."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    for i in range(260):
        _project(app, pid=str(uuid.uuid4()), slug=f"filler-{i:03d}", name="Duplicate")

    assert cli.main(["project", "delete", "name:Duplicate", "--yes"]) != 0
    assert len(app.projects) == 260, "nothing deleted while uniqueness is unproven"


def test_a_backend_that_drops_the_run_slug_is_not_a_success(app, client, monkeypatch):
    """Pydantic ignores an undeclared body field, so an older backend creates a
    randomly-named run and returns 201 -- and the caller exits 0 believing it owns
    a handle it does not."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    app.seed_experiment("nightly")
    original = client.transport.post

    def drops_slug(path, body=None, **kw):
        if isinstance(body, dict):
            body = {k: v for k, v in body.items() if k != "slug"}
        return original(path, body, **kw)

    monkeypatch.setattr(client.transport, "post", drops_slug)

    assert cli.main(["run", "start", "--experiment", "nightly", "--slug", "nightly-eval"]) != 0
