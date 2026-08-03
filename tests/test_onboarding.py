"""First-run onboarding: lazy device auth and explicit creation."""

from __future__ import annotations

import json
import re

import pytest

from probe import cli, errors
from tests.conftest import make_client


# -- SDK: explicit run placement + experiment patching ------------------------
def _create_experiment(client, slug, name, *, hypothesis):
    project = client.resolve_project("test-project")
    if project is None:
        project = client.create_project("test-project")
    return client.create_experiment(
        slug,
        name,
        hypothesis=hypothesis,
        project_id=project["id"],
    )


def test_run_with_no_experiment_refuses_rather_than_inventing_one(app, client):
    """The context fallback is gone, and its absence has to be LOUD.

    It used to derive the slug from the git repo or script name and create it, so
    running from the wrong directory silently filed work under a new experiment
    named after that directory. Nothing failed; the record was just wrong."""
    with pytest.raises(errors.ValidationError, match="needs an experiment slug"):
        client.run()
    assert app.experiments == {}


def test_run_names_near_misses_so_a_typo_is_obvious(app, client):
    """A mistyped slug is by definition close to a real one; this is the whole
    reason resolution beats get-or-create."""
    _create_experiment(client, "dockq-sweep", "DockQ", hypothesis="temp 0.7 wins")
    with pytest.raises(errors.NotFoundError, match="dockq-sweep") as caught:
        client.run(experiment="dockq-sweeep", name="r1")
    assert "Did you mean" in str(caught.value)
    assert '--hypothesis "..." --project PROJECT_SLUG' in str(caught.value)


def test_an_unknown_project_raises_rather_than_being_created_or_ignored(app, client):
    """Both halves of the resolve have to fail loudly, not just the experiment.

    Mutation testing killed this file twice on the project branch alone: once by
    swapping the raise for a silent `create_project`, once by substituting
    `{"id": None}` so an unknown project was quietly dropped. Every other test
    seeds the project it names, so both mutants survived a green suite. This
    asserts the branch itself.
    """
    client.create_project("folding")
    _create_experiment(client, "dockq", "DockQ", hypothesis="h")
    before = len(client.list_projects().items)

    with pytest.raises(errors.NotFoundError, match="project"):
        client.run(project="foldingg", experiment="dockq", name="r1")

    # not created behind our back, and not silently ignored either
    assert len(client.list_projects().items) == before
    assert app.runs == {}


def test_a_deleted_slug_is_absent_not_a_dead_end(app, client):
    """Archiving made one slug both "does not exist" (lookup) and "already exists"
    (create), which is why resolution needed a third outcome. Deleting frees the
    slug, so absent means absent and the advice a NotFoundError gives is followable."""
    proj = client.create_project("folding")
    client.delete_project(proj["id"])

    assert client.resolve_project("folding") is None

    with pytest.raises(errors.NotFoundError) as caught:
        client.run(project="folding", experiment="whatever", name="r1")
    assert "ARCHIVED" not in str(caught.value)
    # And the advice actually works — the slug really is creatable again.
    assert client.create_project("folding")["id"] != proj["id"]


def test_creating_an_experiment_with_a_positional_hypothesis_is_refused(client):
    """`create_experiment("slug", "my hypothesis")` used to silently set the NAME.

    hypothesis was the third positional with a None default, so the common
    two-argument call bound a hypothesis to `name` and then failed the required-
    hypothesis check for the wrong reason — or worse, passed with a nonsense name.
    Keyword-only makes the mistake unrepresentable."""
    with pytest.raises(TypeError):
        client.create_experiment("e1", "E1", "temp 0.7 wins")


def test_an_explicit_hypothesis_survives(app, client):
    _create_experiment(client, "e1", "E1", hypothesis="temp 0.7 wins")
    client.run(experiment="e1", name="r1")
    (experiment,) = app.experiments.values()
    assert experiment["hypothesis"] == "temp 0.7 wins"


def test_unnamed_run_uses_the_inline_timestamp_fallback(app, client):
    _create_experiment(client, "e1", "E1", hypothesis="h")
    run = client.run(experiment="e1")
    assert re.fullmatch(r"run-\d{8}-\d{6}", run.name)


def test_creating_an_experiment_without_a_hypothesis_is_refused(client):
    """No more `[auto]` placeholder. It was first-write-wins, so it became
    permanent unless a human noticed and ran `probe experiment set`."""
    with pytest.raises(errors.ValidationError, match="hypothesis"):
        client.create_experiment("e1", "E1", hypothesis=None, project_id="p")


@pytest.mark.parametrize("project_id", [None, ""])
def test_creating_an_experiment_requires_a_project_id(client, project_id):
    with pytest.raises(errors.ValidationError, match="project_id"):
        client.create_experiment(
            "e1",
            "E1",
            hypothesis="h",
            project_id=project_id,
        )


def test_update_experiment_replaces_the_hypothesis(app, client):
    exp = _create_experiment(client, "e1", "E1", hypothesis="first guess")
    updated = client.update_experiment(exp["id"], hypothesis="dockq > 0.8 at temp 0.7")
    assert updated["hypothesis"] == "dockq > 0.8 at temp 0.7"


def test_update_experiment_requires_a_field(client):
    with pytest.raises(ValueError):
        client.update_experiment("whatever")


def test_update_run_replaces_human_details(app, client):
    _create_experiment(client, "e1", "E1", hypothesis="h")
    run = client.run(experiment="e1", name="before", description="old context")

    updated = client.update_run(
        run.id,
        name="after",
        description="new context",
    )

    assert updated["name"] == "after"
    assert updated["description"] == "new context"


def test_update_run_requires_a_field(client):
    with pytest.raises(ValueError):
        client.update_run("whatever")


# -- SDK: lazy device auth ----------------------------------------------------
def _tokenless_client(app, tmp_path):
    client = make_client(app, tmp_spool=tmp_path / "spool")
    client.settings.token = None
    return client


def test_ensure_authenticated_mints_and_persists_token(app, tmp_path, monkeypatch):

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    calls = {}

    def fake_device_login(base_url, *, on_prompt=None, **kw):
        calls["base_url"] = base_url
        return "ros_pat_minted"

    import probe.sdk.device as device_mod

    monkeypatch.setattr(device_mod, "device_login", fake_device_login)
    client = _tokenless_client(app, tmp_path)
    assert client.ensure_authenticated(interactive=True) is True
    assert client.settings.token == "ros_pat_minted"
    assert calls["base_url"] == "http://test"
    # persisted for the next process, same file `probe login` writes
    from probe.sdk.config import load_context

    assert load_context()["token"] == "ros_pat_minted"
    # the shared Settings object authenticates the existing transport too
    assert client.me()["email"] == "dev@example.com"


def test_ensure_authenticated_noninteractive_leaves_autherror(app, tmp_path):
    client = _tokenless_client(app, tmp_path)
    assert client.ensure_authenticated(interactive=False) is False
    from probe.sdk import errors

    with pytest.raises(errors.AuthError):
        client.me()


class _FakeTty:
    def isatty(self) -> bool:
        return True

    def write(self, _text: str) -> int:  # pragma: no cover - print() plumbing
        return 0

    def flush(self) -> None:  # pragma: no cover - print() plumbing
        pass


def test_run_triggers_lazy_auth(app, tmp_path, monkeypatch):
    """client.run() self-authorizes when interactive auth is possible."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    import sys

    import probe.sdk.device as device_mod

    monkeypatch.setattr(device_mod, "device_login", lambda base_url, **kw: "ros_pat_minted")
    # force the interactive branch without a real TTY
    monkeypatch.setattr(sys, "stdin", _FakeTty())
    monkeypatch.setattr(sys, "stderr", _FakeTty())
    client = _tokenless_client(app, tmp_path)
    # Seeded straight into the fake: `run()` is what self-authorizes, and going
    # through create_experiment first would hit the auth wall before we got there.
    app.seed_experiment("e1")
    run = client.run(experiment="e1", name="r1")
    assert run.id in app.runs
    assert client.settings.token == "ros_pat_minted"


def test_auto_login_disabled_by_env(app, tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_AUTO_LOGIN", "0")
    client = _tokenless_client(app, tmp_path)
    assert client.ensure_authenticated() is False


# -- CLI ------------------------------------------------------------------------
@pytest.fixture
def wired(app, tmp_path, monkeypatch):
    def factory(**_kw):
        return make_client(app, tmp_spool=tmp_path / "spool")

    monkeypatch.setattr(cli, "Client", factory)
    return app


def test_cli_run_start_requires_an_experiment(wired, capsys):
    """`--experiment` is required now, so the context fallback cannot fire."""
    rc = cli.main(["run", "start"])
    assert rc != 0
    assert wired.experiments == {}


def test_cli_experiment_create_then_run_start(wired, capsys):
    """The two-command shape that replaced the implicit chain."""
    assert cli.main(["project", "create", "p"]) == 0
    capsys.readouterr()
    assert (
        cli.main(
            [
                "experiment",
                "create",
                "e",
                "--hypothesis",
                "temp 0.7 wins",
                "--project",
                "p",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["run", "start", "--experiment", "e", "--name", "r1"]) == 0
    assert capsys.readouterr().out.strip() in wired.runs
    (experiment,) = wired.experiments.values()
    assert experiment["hypothesis"] == "temp 0.7 wins"


def test_cli_experiment_create_requires_project_or_active_project(wired, capsys):
    rc = cli.main(["experiment", "create", "missing-project", "--hypothesis", "h"])
    assert rc != 0
    captured = capsys.readouterr()
    assert "pass --project" in (captured.out + captured.err)


def test_cli_experiment_create_uses_the_active_project(wired, capsys):
    assert cli.main(["project", "create", "p"]) == 0
    capsys.readouterr()
    assert cli.main(["project", "use", "p"]) == 0
    capsys.readouterr()
    assert cli.main(["experiment", "create", "active-exp", "--hypothesis", "h"]) == 0
    created = capsys.readouterr().out
    assert '"project_id"' in created


def test_cli_experiment_create_rejects_an_unknown_project(wired, capsys):
    missing = cli.main(
        [
            "experiment",
            "create",
            "missing-exp",
            "--hypothesis",
            "h",
            "--project",
            "missing-project",
        ]
    )
    assert missing != 0
    captured = capsys.readouterr()
    assert "no project" in (captured.out + captured.err).lower()

    assert cli.main(["project", "create", "gone-project"]) == 0
    created = capsys.readouterr().out
    project_id = json.loads(created)["id"]
    assert cli.main(["project", "delete", project_id, "--yes"]) == 0
    capsys.readouterr()
    deleted = cli.main(
        [
            "experiment",
            "create",
            "orphan-exp",
            "--hypothesis",
            "h",
            "--project",
            "gone-project",
        ]
    )
    # A deleted project is simply gone, so this is the same plain "no project"
    # refusal as an unknown slug — not a separate archived state.
    assert deleted != 0
    captured = capsys.readouterr()
    assert "no project" in (captured.out + captured.err).lower()


def test_cli_project_patch_updates_title_and_description(wired, capsys):
    cli.main(["project", "create", "p"])
    project_id = json.loads(capsys.readouterr().out)["id"]

    rc = cli.main(
        [
            "project",
            "patch",
            project_id,
            "--name",
            "Protein folding",
            "--description",
            "DockQ studies",
        ]
    )

    assert rc == 0
    assert wired.projects[project_id]["name"] == "Protein folding"
    assert wired.projects[project_id]["description"] == "DockQ studies"


def test_cli_experiment_set_updates_human_details(wired, capsys):
    cli.main(["project", "create", "p"])
    capsys.readouterr()
    cli.main(["experiment", "create", "e", "--hypothesis", "h", "--project", "p"])
    capsys.readouterr()
    (exp_id,) = wired.experiments
    rc = cli.main(
        [
            "experiment",
            "set",
            exp_id,
            "--hypothesis",
            "real hypothesis",
            "--name",
            "DockQ sweep",
            "--description",
            "Temperature sweep",
        ]
    )
    assert rc == 0
    assert wired.experiments[exp_id]["hypothesis"] == "real hypothesis"
    assert wired.experiments[exp_id]["name"] == "DockQ sweep"
    assert wired.experiments[exp_id]["description"] == "Temperature sweep"


def test_cli_bare_login_runs_device_flow(wired, tmp_path, monkeypatch, capsys):
    import importlib

    cli_main = importlib.import_module("probe.cli.main")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setattr(cli_main, "device_login", lambda endpoint, on_prompt=None: "ros_pat_from_device")
    rc = cli.main(["login"])
    assert rc == 0
    from probe.sdk.config import load_context

    saved = load_context()
    assert saved["token"] == "ros_pat_from_device"
    assert "logged in" in capsys.readouterr().out


def test_cli_login_endpoint_only(wired, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    rc = cli.main(["login", "--endpoint-only", "--base-url", "http://elsewhere"])
    assert rc == 0
    from probe.sdk.config import load_context

    assert load_context()["base_url"] == "http://elsewhere"
    assert "no user token set" in capsys.readouterr().out


# -- SDK create-on-demand (SDK only; the CLI stays strict) ---------------------
def test_a_hypothesis_creates_the_experiment_from_the_sdk(app, client):
    """The one opt-in to creation. A hypothesis is the thing you can only write
    when you know what you are testing, so the cost of a new experiment is one
    sentence and an accident cannot pay it."""
    run = client.run(
        project="test-project",
        experiment="dockq-sweep",
        hypothesis="temp 0.7 wins",
        name="r1",
    )
    (experiment,) = app.experiments.values()
    assert experiment["slug"] == "dockq-sweep"
    assert experiment["hypothesis"] == "temp 0.7 wins"
    assert run.id in app.runs


def test_a_hypothesis_without_a_project_creates_nothing(app, client):
    with pytest.raises(errors.ValidationError, match="explicit project slug"):
        client.run(experiment="dockq-sweep", hypothesis="temp 0.7 wins", name="r1")
    assert app.projects == {}
    assert app.experiments == {}
    assert app.runs == {}


def test_without_a_hypothesis_the_sdk_still_refuses_to_create(app, client):
    """Strict is the default on both surfaces. Only `hypothesis=` unlocks it."""
    with pytest.raises(errors.NotFoundError, match="dockq-sweep"):
        client.run(experiment="dockq-sweep", name="r1")
    assert app.experiments == {}


def test_a_second_run_reuses_the_experiment_and_keeps_its_hypothesis(app, client):
    """First-write-wins: reopening never rewrites the hypothesis, so passing a
    different one later is not a silent edit."""
    client.run(
        project="test-project",
        experiment="e1",
        hypothesis="first guess",
        name="r1",
    )
    client.run(
        project="test-project",
        experiment="e1",
        hypothesis="second thoughts",
        name="r2",
    )
    (experiment,) = app.experiments.values()
    assert experiment["hypothesis"] == "first guess"
    assert len(app.runs) == 2


def test_a_near_miss_is_refused_even_with_a_hypothesis(app, client):
    """Warn-and-proceed was the obvious answer and it is wrong: a warning is
    invisible from a detached CLI process and inside a training loop. `[auto]`
    already proved that shape fails — it shipped with a fix affordance and nobody
    ever used it."""
    _create_experiment(client, "dockq-sweep", "DockQ", hypothesis="temp 0.7 wins")
    with pytest.raises(errors.ValidationError, match="near-miss") as caught:
        client.run(
            project="test-project",
            experiment="dockq-sweeep",
            hypothesis="deliberate?",
            name="r1",
        )
    assert "dockq-sweep" in str(caught.value)
    assert "project_id=PROJECT_ID" in str(caught.value)
    assert "--project PROJECT_SLUG" in str(caught.value)
    assert len(app.experiments) == 1


def test_short_version_names_are_not_near_misses(app, client):
    """The 0.6 cutoff has to leave deliberate short names usable — v1 vs v2 scores
    0.5 — so what this catches is long near-identical slugs."""
    _create_experiment(client, "v1", "V1", hypothesis="h")
    client.run(
        project="test-project",
        experiment="v2",
        hypothesis="the next one",
        name="r1",
    )
    assert sorted(e["slug"] for e in app.experiments.values()) == ["v1", "v2"]


def test_creation_carries_the_project_too(app, client):
    """The project follows the experiment: unlocked by the same hypothesis."""
    before = len(client.list_projects().items)
    client.run(project="folding", experiment="dockq", hypothesis="h", name="r1")
    assert len(client.list_projects().items) == before + 1
    (project,) = [p for p in client.list_projects().items if p["slug"] == "folding"]
    (experiment,) = app.experiments.values()
    assert experiment["project_id"] == project["id"]


def test_a_project_direct_run_never_creates(app, client):
    """A project-direct run cannot carry a hypothesis, so it always resolves
    strictly — and that is the honest home for work with no hypothesis."""
    with pytest.raises(errors.NotFoundError, match="folding"):
        client.run(project="folding", name="r1")
    assert app.runs == {}

    client.create_project("folding")
    run = client.run(project="folding", name="r1")
    assert run.id in app.runs


def test_a_hypothesis_without_an_experiment_is_refused(app, client):
    """A project-direct run has no experiment to hold one."""
    client.create_project("folding")
    with pytest.raises(errors.ValidationError, match="hypothesis"):
        client.run(project="folding", hypothesis="h", name="r1")


def test_a_deleted_experiment_slug_is_recreatable_on_the_create_path(app, client):
    """Create-on-demand used to hit the archived dead end here. A deleted
    experiment frees its slug, so the same call now just creates a fresh one."""
    exp = _create_experiment(client, "dockq", "DockQ", hypothesis="h")
    client.delete_experiment(exp["id"])
    run = client.run(
        project="test-project",
        experiment="dockq",
        hypothesis="h",
        name="r1",
    )
    assert app.runs[run.id]["experiment_id"] != exp["id"]


def test_losing_a_create_race_returns_the_winner(app, client):
    """Get-or-create promises the row exists afterwards, not that WE made it.
    Different from the swallow #87 removed: that hid a typo behind a
    successful-looking create; this resolves a race on the same correct slug."""
    project = client.create_project("test-project")
    winner = app.seed_experiment("dockq", project_id=project["id"])
    app.experiment_conflict_id = winner["id"]
    real = client.resolve_experiment
    calls = {"n": 0}

    def racy(slug, **kw):
        # The sibling wins the race DURING our create: the single look-up before
        # it sees nothing; the one after the 409 sees the row. (It used to be two
        # look-ups — present? archived? — before archiving was removed.)
        calls["n"] += 1
        return None if calls["n"] <= 1 else real(slug, **kw)

    client.resolve_experiment = racy
    assert client.ensure_experiment(
        "dockq", "DockQ", hypothesis="h", project_id=project["id"]
    )["id"] == winner["id"]


# -- the CLI cannot create, on any path ---------------------------------------
def test_cli_run_start_has_no_hypothesis_option(wired, capsys):
    """The slug is hand-typed on every CLI invocation, which is where typos come
    from — so creation there goes through `probe experiment create` only."""
    rc = cli.main(["run", "start", "--experiment", "e", "--hypothesis", "h"])
    assert rc != 0
    combined = capsys.readouterr()
    assert "hypothesis" in (combined.out + combined.err).lower()
    assert wired.experiments == {}


def test_cli_run_start_cannot_create_an_unknown_experiment(wired, capsys):
    rc = cli.main(["run", "start", "--experiment", "does-not-exist"])
    assert rc != 0
    assert wired.experiments == {}
    assert wired.runs == {}


# -- guard holes found in pre-landing review ----------------------------------
def test_a_near_miss_is_refused_even_when_it_lives_in_another_project(app, client):
    """Experiment slugs are unique per TENANT, not per project. Scoping the
    near-miss listing to the named project let a typo of an experiment filed
    elsewhere sail straight through — verified before the fix."""
    other = client.create_project("other")
    client.create_experiment("dockq-sweep", "DockQ", hypothesis="h", project_id=other["id"])
    with pytest.raises(errors.ValidationError, match="near-miss"):
        client.run(project="folding", experiment="dockq-sweeep", hypothesis="h", name="r1")
    assert len(app.experiments) == 1


def test_a_refused_experiment_leaves_no_orphan_project(app, client):
    """ensure_project runs before ensure_experiment, so the refusal has to happen
    first — otherwise the guard against stray identities creates one itself."""
    _create_experiment(client, "dockq-sweep", "DockQ", hypothesis="h")
    before = len(client.list_projects().items)
    with pytest.raises(errors.ValidationError, match="near-miss"):
        client.run(project="brand-new", experiment="dockq-sweeep", hypothesis="h", name="r1")
    assert len(client.list_projects().items) == before


def test_an_empty_hypothesis_creates_nothing(app, client):
    """`hypothesis=args.hypothesis or ""` is an ordinary way to get here. run()
    gated on `is not None` while create_experiment gated on falsiness, so an
    empty string committed a PROJECT before failing on the experiment."""
    before = len(client.list_projects().items)
    with pytest.raises(errors.ValidationError, match="empty"):
        client.run(project="brand-new", experiment="brand-new-exp", hypothesis="", name="r1")
    assert len(client.list_projects().items) == before
    assert app.experiments == {}


def test_a_deleted_project_is_recreated_on_the_create_path(app, client):
    """The create path goes through ensure_project rather than resolve_or_raise.
    A deleted project is simply absent there, so a hypothesis-bearing run
    rebuilds it instead of hitting the old archived dead end."""
    proj = client.create_project("folding")
    client.delete_project(proj["id"])
    client.run(project="folding", experiment="dockq", hypothesis="h", name="r1")
    assert client.resolve_project("folding")["id"] != proj["id"]


def test_a_near_miss_project_is_refused_on_the_create_path(app, client):
    client.create_project("folding-experiments")
    with pytest.raises(errors.ValidationError, match="near-miss"):
        client.run(project="folding-experimentss", experiment="dockq", hypothesis="h", name="r1")
    assert len(client.list_projects().items) == 1


def test_experiment_name_without_a_hypothesis_is_refused(app, client):
    with pytest.raises(errors.ValidationError, match="experiment_name"):
        client.run(experiment="e1", experiment_name="E One", name="r1")


def test_experiment_name_titles_the_experiment_it_creates(app, client):
    client.run(
        project="test-project",
        experiment="e1",
        experiment_name="E One",
        hypothesis="h",
        name="r1",
    )
    (experiment,) = app.experiments.values()
    assert experiment["slug"] == "e1" and experiment["name"] == "E One"


def test_the_near_miss_guard_sees_past_the_first_page(app, client):
    """`limit=200` is the schema maximum and page order is unspecified, so a
    single-page guard silently stops firing — and it is the OLDER slugs that drop
    out of view, which are exactly what a typo is a near-miss of."""
    _create_experiment(client, "dockq-sweep", "DockQ", hypothesis="h")
    for i in range(220):
        _create_experiment(client, f"filler-{i:03d}", f"F{i}", hypothesis="h")
    with pytest.raises(errors.ValidationError, match="near-miss"):
        client.run(
            project="test-project",
            experiment="dockq-sweeep",
            hypothesis="h",
            name="r1",
        )


def test_a_slug_blind_backend_resolves_to_nothing_rather_than_the_wrong_row(app, client, monkeypatch):
    """FastAPI silently drops a query param it does not declare, so a backend
    without `?slug=` answers an unfiltered first page. Taking rows[0] there would
    attach the caller to a real, arbitrary, WRONG experiment — worse than the
    get-or-create it replaced, because it appends your metrics to someone else's."""
    _create_experiment(client, "someone-elses", "X", hypothesis="h")
    real_get = client.transport.get

    def unfiltered(path, *, params=None):
        if path in ("/v1/experiments", "/v1/projects"):
            params = {k: v for k, v in (params or {}).items() if k != "slug"} or None
        return real_get(path, params=params)

    monkeypatch.setattr(client.transport, "get", unfiltered)
    assert client.resolve_experiment("dockq-sweep") is None
    with pytest.raises(errors.NotFoundError):
        client.run(experiment="dockq-sweep", name="r1")
    assert app.runs == {}
