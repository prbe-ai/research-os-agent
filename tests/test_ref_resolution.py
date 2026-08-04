"""Resolving a ref to the thing it names -- and refusing when it names two.

The incident these cover (observed 2026-08-04, Richard's Workspace):

    A: slug ``parity-smoke-18cdeb5``  id ``6fa49e87-...-9f477c0d880e``
    B: slug ``6fa49e87-...-9f477c0d880e``  id ``17acbbb2-...-0d44b23bc558``

``probe project delete 6fa49e87-...`` means B to the person typing it and meant A
to the CLI, which parsed the ref as a UUID and never asked whether a slug matched.
It would have permanently deleted A, silently. Both projects are gone now, so
these build the collision from scratch rather than reaching for them.

The load-bearing assertion in the headline test is not the exit code -- it is that
BOTH projects are still there afterwards. A refusal that still deleted something
would pass an exit-code-only check.
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


# -- the headline: a ref that names two projects is refused -------------------
def test_a_ref_that_is_both_an_id_and_a_slug_deletes_nothing(collision, capsys):
    """THE regression. Before the fix this exited 0 having deleted A."""
    code = cli.main(["project", "delete", A_ID, "--yes"])

    assert code != 0
    # The whole point: nothing was destroyed while we worked out what was meant.
    assert set(collision.projects) == {A_ID, B_ID}
    err = capsys.readouterr().err
    assert "--by-id" in err and "--by-slug" in err
    # Both candidates named, so the operator can tell which is which.
    assert "Parity smoke" in err and B_ID in err


def test_the_refusal_survives_yes(collision):
    """``--yes`` skips the prompt; it must not skip the ambiguity check.

    Ambiguity is not a confirmation -- there is no answer to "are you sure" that
    tells us WHICH project was meant, and scripts pass --yes by default.
    """
    assert cli.main(["project", "delete", A_ID, "--yes"]) != 0
    assert set(collision.projects) == {A_ID, B_ID}


def test_by_id_deletes_the_project_with_that_id(collision):
    assert cli.main(["project", "delete", A_ID, "--yes", "--by-id"]) == 0

    assert set(collision.projects) == {B_ID}


def test_by_slug_deletes_the_project_with_that_slug(collision):
    """Case 1 of the bug: a UUID-shaped slug was previously unreachable."""
    assert cli.main(["project", "delete", B_SLUG, "--yes", "--by-slug"]) == 0

    assert set(collision.projects) == {A_ID}


def test_by_id_and_by_slug_together_are_refused(collision):
    assert cli.main(["project", "delete", A_ID, "--yes", "--by-id", "--by-slug"]) != 0
    assert set(collision.projects) == {A_ID, B_ID}


# -- no collision: both spellings still work, on every verb -------------------
def test_a_uuid_shaped_slug_resolves_on_its_own(app, client, monkeypatch):
    """With no id collision, B's UUID-shaped slug is not ambiguous -- just a slug."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=B_ID, slug=B_SLUG)

    assert cli.main(["project", "delete", B_SLUG, "--yes"]) == 0
    assert app.projects == {}


def test_an_ordinary_slug_still_resolves(app, client, monkeypatch):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug=A_SLUG)
    _project(app, pid=B_ID, slug="second")

    assert cli.main(["project", "delete", A_SLUG, "--yes"]) == 0
    assert set(app.projects) == {B_ID}


def test_a_plain_id_still_resolves(app, client, monkeypatch):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug=A_SLUG)
    _project(app, pid=B_ID, slug="second")

    assert cli.main(["project", "delete", B_ID, "--yes"]) == 0
    assert set(app.projects) == {A_ID}


def test_a_missing_ref_says_so(app, client, monkeypatch):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)

    assert cli.main(["project", "delete", "nope", "--yes"]) != 0


# -- the false absence: the 200-row scan --------------------------------------
def test_a_slug_past_the_old_200_row_cap_still_resolves(app, client, monkeypatch):
    """Bug 2: ``list_projects(limit=200)`` reported a real project as absent.

    A false absence is worse than a slow lookup -- it reads as "that project does
    not exist", which is acted on by creating a duplicate. Resolution is a
    server-side ``?slug=`` on a UNIQUE column now, so row 201 is not special.
    """
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    for i in range(250):
        _project(app, pid=str(uuid.uuid4()), slug=f"filler-{i:03d}")
    _project(app, pid=A_ID, slug=A_SLUG)

    assert cli.main(["project", "delete", A_SLUG, "--yes"]) == 0
    assert A_ID not in app.projects


def test_resolution_does_not_page_through_the_listing(collision, monkeypatch):
    """Guards the mechanism, not just the outcome: no listing scan at all.

    The outcome test above would also pass if someone "fixed" the cap by paging
    the whole tenant, which is the same false-absence risk one release later.
    """

    def boom(*a, **kw):
        raise AssertionError("resolution must not scan list_projects")

    monkeypatch.setattr(Client, "list_projects", boom, raising=True)

    assert cli.main(["project", "delete", A_SLUG, "--yes", "--by-slug"]) == 0
    assert A_ID not in collision.projects


# -- the inverse resolver carries the same rule -------------------------------
def test_the_slug_resolver_refuses_the_same_collision(collision, client):
    """``_project_slug`` had the bug mirrored: UUID-shaped ref -> assumed an id."""
    with pytest.raises(Exception) as exc:
        impl._project_slug(client, A_ID)

    assert "--by-slug" in str(exc.value)


def test_the_slug_resolver_still_passes_a_plain_slug_through(collision, client):
    """Unchanged on purpose: absence here is ``run start``'s to report, with the
    near-miss guard that names what the typo was close to."""
    assert impl._project_slug(client, "never-created") == "never-created"


# -- consistency across the delete verbs --------------------------------------
def test_experiment_delete_accepts_a_slug(app, client, monkeypatch):
    """New: experiments have slugs, and ``experiment delete`` took ids only, so a
    slug reached the UUID-typed route as a 422."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    exp = app.seed_experiment("ablation-7")

    assert cli.main(["experiment", "delete", "ablation-7", "--yes"]) == 0
    assert exp["id"] not in app.experiments


def test_run_delete_accepts_a_petname_and_deletes_by_id(app, client, monkeypatch):
    """New: ``run get <petname>`` worked and ``run delete <petname>`` 422'd.

    Asserts the canonical id is what reached DELETE -- the fake auto-creates a run
    for an unknown ref on GET, so an exit-code-only check would pass on a fiction.
    """
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    exp = app.seed_experiment("e")
    run = app._new_run(
        "aaaaaaaa-1111-4222-8333-444444444444", exp["id"], {"short_id": "tunneling-sambar-254"}
    )

    assert cli.main(["run", "delete", "tunneling-sambar-254", "--yes"]) == 0
    assert run["id"] not in app.runs
    # The petname must not have been taken for an id and auto-vivified as a run.
    assert "tunneling-sambar-254" not in app.runs


def test_every_delete_verb_prompts_with_the_resolved_entity(collision, monkeypatch, capsys):
    """The prompt names what will die, not the string that was typed.

    Confirming the ref back is confirming your own typo; in the collision it is
    precisely the string that does not identify the target.
    """
    seen: list[str] = []
    monkeypatch.setattr(
        "typer.confirm", lambda question, **kw: seen.append(question) or True
    )

    assert cli.main(["project", "delete", A_SLUG]) == 0

    assert len(seen) == 1
    assert "Parity smoke" in seen[0] and A_SLUG in seen[0] and A_ID in seen[0]


# -- the sibling resolvers carry the same rule --------------------------------
def test_the_artifact_anchor_refuses_the_collision(collision, client):
    """``_anchor_id_for`` is "additive, never a gate" -- with one exception.

    An unresolvable anchor still passes through (the server 422s it). An
    AMBIGUOUS one does not: it resolves to two projects, and picking either files
    the upload into a project that may not be the caller's at all. That failure is
    silent and is discovered by whoever later reads a project full of someone
    else's artifacts.
    """
    with pytest.raises(Exception) as exc:
        impl._anchor_id_for(client, impl.Anchor.PROJECT, A_ID)

    assert "--by-id" in str(exc.value)


def test_the_artifact_anchor_still_passes_an_unresolvable_ref_through(collision, client):
    """The "never a gate" half, unchanged: the server owns that 422."""
    assert impl._anchor_id_for(client, impl.Anchor.PROJECT, "not-a-project") == "not-a-project"


def test_the_backfill_anchor_refuses_the_collision(collision, client):
    """The backfill anchor names every artifact an import uploads."""
    from probe.cli import backfill

    with pytest.raises(ValueError) as exc:
        backfill._resolve_ref(client, A_ID)

    assert "--by-id" in str(exc.value)


# -- the flag-free selector ---------------------------------------------------
def test_the_id_prefix_selects_the_project_with_that_id(collision):
    assert cli.main(["project", "delete", f"id:{A_ID}", "--yes"]) == 0
    assert set(collision.projects) == {B_ID}


def test_the_slug_prefix_selects_the_project_with_that_slug(collision):
    assert cli.main(["project", "delete", f"slug:{B_SLUG}", "--yes"]) == 0
    assert set(collision.projects) == {A_ID}


def test_the_prefix_works_where_the_flags_do_not_exist(collision, client):
    """The reason the prefix exists.

    `--project` is taken by roughly a dozen commands that never declare
    --by-id/--by-slug. Without the prefix the ambiguity error there names two
    flags the command does not have, and project A -- whose id IS the colliding
    string -- cannot be addressed on those commands at all.
    """
    assert impl._project_id(client, f"id:{A_ID}") == A_ID


def test_the_error_names_the_prefix_not_only_the_flags(collision, capsys):
    cli.main(["project", "delete", A_ID, "--yes"])

    err = capsys.readouterr().err
    assert f"id:{A_ID}" in err and f"slug:{A_ID}" in err


def test_an_ordinary_slug_containing_no_selector_is_untouched(app, client, monkeypatch):
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    _project(app, pid=A_ID, slug="ordinary-slug")

    assert cli.main(["project", "delete", "ordinary-slug", "--yes"]) == 0
    assert app.projects == {}


# -- the same rule on the non-delete ref sites --------------------------------
def test_run_list_resolves_an_experiment_slug(app, client, monkeypatch, capsys):
    """`--experiment` shipped the slug into a UUID-typed query param, so it came
    back as a raw pydantic uuid_parsing dump instead of a listing."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    exp = app.seed_experiment("ablation-9")

    assert cli.main(["run", "list", "--experiment", "ablation-9"]) == 0

    seen = json.loads(capsys.readouterr().out)
    assert seen["items"] == [] or all(r["experiment_id"] == exp["id"] for r in seen["items"])


def test_an_experiment_artifact_anchor_takes_a_slug(app, client, monkeypatch):
    """`--project my-slug` resolved and `--experiment my-slug` 422'd, on the same
    command line."""
    monkeypatch.setattr(cli, "Client", lambda **kw: client)
    exp = app.seed_experiment("anchor-me")

    assert impl._anchor_id_for(client, impl.Anchor.EXPERIMENT, "anchor-me") == exp["id"]


def test_a_run_anchor_is_left_alone(client):
    """Runs have no slug: they anchor by id or petname, resolved server-side."""
    assert impl._anchor_id_for(client, impl.Anchor.RUN, "some-run-ref") == "some-run-ref"
