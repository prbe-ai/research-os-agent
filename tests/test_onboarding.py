"""First-run onboarding: lazy device auth, contextual defaults, explicit creation."""

from __future__ import annotations

import subprocess

import pytest

from probe import cli, errors
from probe.sdk import defaults
from tests.conftest import make_client


# -- defaults derivation ------------------------------------------------------
def _init_repo(path):
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)


def test_default_experiment_slug_uses_git_repo_name(tmp_path):
    repo = tmp_path / "My Fold_Sweep"
    repo.mkdir()
    _init_repo(repo)
    assert defaults.default_experiment_slug(cwd=str(repo)) == "my-fold-sweep"


def test_default_experiment_slug_falls_back_to_script(tmp_path, monkeypatch):
    monkeypatch.setattr(defaults.sys, "argv", ["/x/train_dockq.py"])
    assert defaults.default_experiment_slug(cwd=str(tmp_path)) == "train-dockq"


def test_default_run_name_is_timestamped():
    assert defaults.default_run_name().startswith("run-20")


def test_auto_hypothesis_is_marked_and_contextual(tmp_path, monkeypatch):
    repo = tmp_path / "foldrepo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.setattr(defaults.sys, "argv", ["/x/train.py"])
    text = defaults.auto_hypothesis("foldrepo", cwd=str(repo))
    assert text.startswith(defaults.AUTO_HYPOTHESIS_PREFIX)
    assert "foldrepo" in text
    assert "probe experiment set" in text


# -- SDK: run() defaults + experiment patching --------------------------------
def test_run_with_no_experiment_refuses_rather_than_inventing_one(app, client, monkeypatch):
    """The context fallback is gone, and its absence has to be LOUD.

    It used to derive the slug from the git repo or script name and create it, so
    running from the wrong directory silently filed work under a new experiment
    named after that directory. Nothing failed; the record was just wrong."""
    monkeypatch.setattr(defaults, "default_experiment_slug", lambda cwd=None: "ctx-slug")
    with pytest.raises(errors.ValidationError, match="needs an experiment slug"):
        client.run()
    assert app.experiments == {}


def test_run_names_near_misses_so_a_typo_is_obvious(app, client):
    """A mistyped slug is by definition close to a real one; this is the whole
    reason resolution beats get-or-create."""
    client.create_experiment("dockq-sweep", "DockQ", hypothesis="temp 0.7 wins")
    with pytest.raises(errors.NotFoundError, match="dockq-sweep") as caught:
        client.run(experiment="dockq-sweeep", name="r1")
    assert "Did you mean" in str(caught.value)


def test_an_unknown_project_raises_rather_than_being_created_or_ignored(app, client):
    """Both halves of the resolve have to fail loudly, not just the experiment.

    Mutation testing killed this file twice on the project branch alone: once by
    swapping the raise for a silent `create_project`, once by substituting
    `{"id": None}` so an unknown project was quietly dropped. Every other test
    seeds the project it names, so both mutants survived a green suite. This
    asserts the branch itself.
    """
    client.create_project("folding")
    client.create_experiment("dockq", "DockQ", hypothesis="h")
    before = len(client.list_projects().items)

    with pytest.raises(errors.NotFoundError, match="project"):
        client.run(project="foldingg", experiment="dockq", name="r1")

    # not created behind our back, and not silently ignored either
    assert len(client.list_projects().items) == before
    assert app.runs == {}


def test_an_archived_slug_says_archived_not_missing(app, client):
    """Archived is a THIRD outcome, and conflating it with absent is a dead end.

    The unique constraint does not ignore archived rows, so the plain lookup says
    "does not exist" and the create that advice implies says "already exists" —
    about the same slug. `ensure_project` used to escape that via the conflict's
    existing_id; resolution has to be able to see the archived row instead.
    """
    proj = client.create_project("folding")
    client.archive_project(proj["id"])

    assert client.resolve_project("folding") is None
    assert client.resolve_project("folding", include_archived=True)["id"] == proj["id"]

    with pytest.raises(errors.NotFoundError, match="ARCHIVED") as caught:
        client.run(project="folding", experiment="whatever", name="r1")
    assert "probe project restore folding" in str(caught.value)


def test_creating_an_experiment_with_a_positional_hypothesis_is_refused(client):
    """`create_experiment("slug", "my hypothesis")` used to silently set the NAME.

    hypothesis was the third positional with a None default, so the common
    two-argument call bound a hypothesis to `name` and then failed the required-
    hypothesis check for the wrong reason — or worse, passed with a nonsense name.
    Keyword-only makes the mistake unrepresentable."""
    with pytest.raises(TypeError):
        client.create_experiment("e1", "E1", "temp 0.7 wins")


def test_an_explicit_hypothesis_survives(app, client):
    client.create_experiment("e1", "E1", hypothesis="temp 0.7 wins")
    client.run(experiment="e1", name="r1")
    (experiment,) = app.experiments.values()
    assert experiment["hypothesis"] == "temp 0.7 wins"


def test_creating_an_experiment_without_a_hypothesis_is_refused(client):
    """No more `[auto]` placeholder. It was first-write-wins, so it became
    permanent unless a human noticed and ran `probe experiment set`."""
    with pytest.raises(errors.ValidationError, match="hypothesis"):
        client.create_experiment("e1", "E1", hypothesis=None)


def test_update_experiment_replaces_the_hypothesis(app, client):
    exp = client.create_experiment("e1", "E1", hypothesis="first guess")
    updated = client.update_experiment(exp["id"], hypothesis="dockq > 0.8 at temp 0.7")
    assert updated["hypothesis"] == "dockq > 0.8 at temp 0.7"


def test_update_experiment_requires_a_field(client):
    with pytest.raises(ValueError):
        client.update_experiment("whatever")


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


def test_cli_run_start_requires_an_experiment(wired, capsys, monkeypatch):
    """`--experiment` is required now, so the context fallback cannot fire."""
    monkeypatch.setattr(defaults, "default_experiment_slug", lambda cwd=None: "ctx-slug")
    rc = cli.main(["run", "start"])
    assert rc != 0
    assert wired.experiments == {}


def test_cli_experiment_create_then_run_start(wired, capsys):
    """The two-command shape that replaced the implicit chain."""
    assert cli.main(["experiment", "create", "e", "--hypothesis", "temp 0.7 wins"]) == 0
    capsys.readouterr()
    assert cli.main(["run", "start", "--experiment", "e", "--name", "r1"]) == 0
    assert capsys.readouterr().out.strip() in wired.runs
    (experiment,) = wired.experiments.values()
    assert experiment["hypothesis"] == "temp 0.7 wins"


def test_cli_experiment_set_hypothesis(wired, capsys):
    cli.main(["experiment", "create", "e", "--hypothesis", "h"])
    capsys.readouterr()
    (exp_id,) = wired.experiments
    rc = cli.main(["experiment", "set", exp_id, "--hypothesis", "real hypothesis"])
    assert rc == 0
    assert wired.experiments[exp_id]["hypothesis"] == "real hypothesis"


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
    run = client.run(experiment="dockq-sweep", hypothesis="temp 0.7 wins", name="r1")
    (experiment,) = app.experiments.values()
    assert experiment["slug"] == "dockq-sweep"
    assert experiment["hypothesis"] == "temp 0.7 wins"
    assert run.id in app.runs


def test_without_a_hypothesis_the_sdk_still_refuses_to_create(app, client):
    """Strict is the default on both surfaces. Only `hypothesis=` unlocks it."""
    with pytest.raises(errors.NotFoundError, match="dockq-sweep"):
        client.run(experiment="dockq-sweep", name="r1")
    assert app.experiments == {}


def test_a_second_run_reuses_the_experiment_and_keeps_its_hypothesis(app, client):
    """First-write-wins: reopening never rewrites the hypothesis, so passing a
    different one later is not a silent edit."""
    client.run(experiment="e1", hypothesis="first guess", name="r1")
    client.run(experiment="e1", hypothesis="second thoughts", name="r2")
    (experiment,) = app.experiments.values()
    assert experiment["hypothesis"] == "first guess"
    assert len(app.runs) == 2


def test_a_near_miss_is_refused_even_with_a_hypothesis(app, client):
    """Warn-and-proceed was the obvious answer and it is wrong: a warning is
    invisible from a detached CLI process and inside a training loop. `[auto]`
    already proved that shape fails — it shipped with a fix affordance and nobody
    ever used it."""
    client.create_experiment("dockq-sweep", "DockQ", hypothesis="temp 0.7 wins")
    with pytest.raises(errors.ValidationError, match="near-miss") as caught:
        client.run(experiment="dockq-sweeep", hypothesis="deliberate?", name="r1")
    assert "dockq-sweep" in str(caught.value)
    assert len(app.experiments) == 1


def test_short_version_names_are_not_near_misses(app, client):
    """The 0.6 cutoff has to leave deliberate short names usable — v1 vs v2 scores
    0.5 — so what this catches is long near-identical slugs."""
    client.create_experiment("v1", "V1", hypothesis="h")
    client.run(experiment="v2", hypothesis="the next one", name="r1")
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


def test_an_archived_slug_is_still_archived_on_the_create_path(app, client):
    """c6bb237's third outcome must survive create-on-demand: creating would 409
    on the slug, so "not found" would send you into a dead end."""
    exp = client.create_experiment("dockq", "DockQ", hypothesis="h")
    client.archive_experiment(exp["id"])
    with pytest.raises(errors.NotFoundError, match="ARCHIVED"):
        client.run(experiment="dockq", hypothesis="h", name="r1")


def test_losing_a_create_race_returns_the_winner(app, client):
    """Get-or-create promises the row exists afterwards, not that WE made it.
    Different from the swallow #87 removed: that hid a typo behind a
    successful-looking create; this resolves a race on the same correct slug."""
    winner = app.seed_experiment("dockq")
    app.experiment_conflict_id = winner["id"]
    real = client.resolve_experiment
    calls = {"n": 0}

    def racy(slug, **kw):
        # The sibling wins the race DURING our create: the two look-ups before it
        # (present? archived?) both see nothing; the one after the 409 sees the row.
        calls["n"] += 1
        return None if calls["n"] <= 2 else real(slug, **kw)

    client.resolve_experiment = racy
    assert client.ensure_experiment("dockq", "DockQ", hypothesis="h")["id"] == winner["id"]


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
    client.create_experiment("dockq-sweep", "DockQ", hypothesis="h")
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


def test_an_archived_project_says_archived_on_the_create_path(app, client):
    """The existing archived-project test passes no hypothesis, so it only ever
    exercised resolve_or_raise — never ensure_project."""
    proj = client.create_project("folding")
    client.archive_project(proj["id"])
    with pytest.raises(errors.NotFoundError, match="ARCHIVED"):
        client.run(project="folding", experiment="dockq", hypothesis="h", name="r1")
    assert app.experiments == {}


def test_a_near_miss_project_is_refused_on_the_create_path(app, client):
    client.create_project("folding-experiments")
    with pytest.raises(errors.ValidationError, match="near-miss"):
        client.run(project="folding-experimentss", experiment="dockq", hypothesis="h", name="r1")
    assert len(client.list_projects().items) == 1


def test_experiment_name_without_a_hypothesis_is_refused(app, client):
    with pytest.raises(errors.ValidationError, match="experiment_name"):
        client.run(experiment="e1", experiment_name="E One", name="r1")


def test_experiment_name_titles_the_experiment_it_creates(app, client):
    client.run(experiment="e1", experiment_name="E One", hypothesis="h", name="r1")
    (experiment,) = app.experiments.values()
    assert experiment["slug"] == "e1" and experiment["name"] == "E One"


def test_the_near_miss_guard_sees_past_the_first_page(app, client):
    """`limit=200` is the schema maximum and page order is unspecified, so a
    single-page guard silently stops firing — and it is the OLDER slugs that drop
    out of view, which are exactly what a typo is a near-miss of."""
    client.create_experiment("dockq-sweep", "DockQ", hypothesis="h")
    for i in range(220):
        client.create_experiment(f"filler-{i:03d}", f"F{i}", hypothesis="h")
    with pytest.raises(errors.ValidationError, match="near-miss"):
        client.run(experiment="dockq-sweeep", hypothesis="h", name="r1")


def test_a_slug_blind_backend_resolves_to_nothing_rather_than_the_wrong_row(app, client, monkeypatch):
    """FastAPI silently drops a query param it does not declare, so a backend
    without `?slug=` answers an unfiltered first page. Taking rows[0] there would
    attach the caller to a real, arbitrary, WRONG experiment — worse than the
    get-or-create it replaced, because it appends your metrics to someone else's."""
    client.create_experiment("someone-elses", "X", hypothesis="h")
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
