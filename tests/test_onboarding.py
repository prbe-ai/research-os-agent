"""First-run onboarding: lazy device auth, contextual defaults, [auto] hypothesis."""

from __future__ import annotations

import subprocess

import pytest

from probe import cli
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
def test_run_with_no_identity_args_uses_defaults(app, client, monkeypatch):
    monkeypatch.setattr(defaults, "default_experiment_slug", lambda cwd=None: "ctx-slug")
    monkeypatch.setattr(defaults, "default_run_name", lambda now=None: "run-x")
    run = client.run()
    assert run.name == "run-x"
    (experiment,) = app.experiments.values()
    assert experiment["slug"] == "ctx-slug"
    assert experiment["hypothesis"].startswith(defaults.AUTO_HYPOTHESIS_PREFIX)


def test_explicit_hypothesis_is_never_replaced(app, client):
    client.run(experiment="e1", hypothesis="temp 0.7 wins", name="r1")
    (experiment,) = app.experiments.values()
    assert experiment["hypothesis"] == "temp 0.7 wins"


def test_update_experiment_replaces_auto_hypothesis(app, client):
    run = client.run(experiment="e1", name="r1")  # auto hypothesis
    exp_id = run.experiment_id
    assert app.experiments[exp_id]["hypothesis"].startswith("[auto]")
    updated = client.update_experiment(exp_id, hypothesis="dockq > 0.8 at temp 0.7")
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
    run = client.run(experiment="e1", hypothesis="h", name="r1")
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


def test_cli_run_start_without_identity_flags(wired, capsys, monkeypatch):
    monkeypatch.setattr(defaults, "default_experiment_slug", lambda cwd=None: "ctx-slug")
    rc = cli.main(["run", "start"])
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out in wired.runs
    (experiment,) = wired.experiments.values()
    assert experiment["hypothesis"].startswith("[auto]")


def test_cli_experiment_set_hypothesis(wired, capsys):
    cli.main(["run", "start", "--experiment", "e", "--name", "r1"])
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
