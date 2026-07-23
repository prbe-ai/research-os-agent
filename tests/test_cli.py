"""CLI smoke against the fake API (Client is swapped for the fake-backed one)."""

from __future__ import annotations

import json
from importlib import metadata
from pathlib import Path

import pytest

import probe
from probe import cli
from probe.sdk.surface import Surface
from tests.conftest import make_client


@pytest.fixture
def wired(app, tmp_path, monkeypatch):
    def factory(**_kw):
        return make_client(app, tmp_spool=tmp_path / "spool")

    monkeypatch.setattr(cli, "Client", factory)
    return app


def test_run_start_prints_id(wired, capsys):
    rc = cli.main(
        ["run", "start", "--experiment", "e", "--hypothesis", "h", "--name", "r1"]
    )
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out in wired.runs


def test_log_command(wired, capsys):
    # make a run first
    cli.main(["run", "start", "--experiment", "e", "--hypothesis", "h", "--name", "r1"])
    run_id = capsys.readouterr().out.strip()
    rc = cli.main(["log", run_id, "loss=0.42", "acc=0.9", "--step", "3"])
    assert rc == 0
    assert wired.metrics_inserted == 2
    body = json.loads(wired.requests[-1].content)
    assert body["points"][0]["step_index"] == 3


def test_link_command(wired, capsys):
    cli.main(["run", "start", "--experiment", "e", "--hypothesis", "h", "--name", "r1"])
    run_id = capsys.readouterr().out.strip()
    rc = cli.main(["link", run_id, "--set", "wandb_run_id=abc", "--set", "gpu_job=rp-1"])
    assert rc == 0
    assert wired.runs[run_id]["foreign_keys"] == {
        "wandb_run_id": "abc",
        "gpu_job": "rp-1",
    }


def test_child_command(wired, capsys):
    cli.main(["run", "start", "--experiment", "e", "--hypothesis", "h", "--name", "r1"])
    parent = capsys.readouterr().out.strip()
    rc = cli.main(["run", "child", parent, "--name", "step-1", "--relation", "resume"])
    assert rc == 0
    child = capsys.readouterr().out.strip()
    assert wired.runs[child]["parent_run_id"] == parent
    assert wired.runs[child]["parent_relation"] == "resume"


def test_artifact_add_forwards_span_content_type_and_meta(wired, capsys, tmp_path):
    cli.main(["run", "start", "--experiment", "e", "--hypothesis", "h", "--name", "r1"])
    run_id = capsys.readouterr().out.strip()
    artifact = tmp_path / "trace.jsonl"
    artifact.write_text("{}\n")
    span_id = "0c5d7c41-c6cf-47ad-97c2-3074e03d89fb"

    assert cli.main(
        [
            "artifact",
            "add",
            run_id,
            str(artifact),
            "--kind",
            "trajectory",
            "--span",
            span_id,
            "--content-type",
            "application/x-ndjson",
            "--meta",
            "format=native",
            "--meta",
            "attempt=2",
        ]
    ) == 0

    presign = next(
        request for request in wired.requests if request.url.path.endswith("/artifacts/uploads")
    )
    body = json.loads(presign.content)
    assert body["span_id"] == span_id
    assert body["content_type"] == "application/x-ndjson"
    assert body["kind"] == "trajectory"
    assert body["meta"] == {"format": "native", "attempt": 2}


def test_global_spool_dir_reaches_the_sdk(app, tmp_path, monkeypatch, capsys):
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return make_client(app, tmp_spool=tmp_path / "test-spool")

    monkeypatch.setattr(cli, "Client", factory)
    durable = tmp_path / "shared-pvc" / "spool"
    assert cli.main(
        [
            "--spool-dir",
            str(durable),
            "run",
            "start",
            "--experiment",
            "e",
            "--hypothesis",
            "h",
            "--name",
            "r1",
        ]
    ) == 0
    assert captured["spool_dir"] == str(durable)


def test_cli_client_construction_reports_the_installed_version(
    app, tmp_path, monkeypatch, capsys
):
    captured = {}

    def factory(**kwargs):
        captured.update(kwargs)
        return make_client(app, tmp_spool=tmp_path / "spool")

    monkeypatch.setattr(cli, "Client", factory)
    assert cli.main(["whoami"]) == 0
    capsys.readouterr()

    assert captured["client_headers"] == {
        "X-Probe-Client": "cli",
        "X-Probe-Client-Version": probe.__version__,
    }
    assert captured["surface"] == Surface.CLI.value


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["log", "r-1", "not-a-kv-pair"], id="bad_parameter"),
        pytest.param(["--bogus-flag"], id="no_such_option"),
        pytest.param(["nosuchcommand"], id="unknown_command"),
    ],
)
def test_usage_errors_exit_cleanly_instead_of_raising(argv, capsys):
    """main() must turn a usage error into an exit code, never a traceback.

    Regression guard: typer vendored click into `typer._click`, so main()'s
    `except click.ClickException` (the standalone package's class) silently stopped
    matching anything typer raises, and every usage error escaped as a traceback.
    An unpinned `typer>=0.12` bump was enough to do it, with no test to notice.
    """
    assert cli.main(argv) == 2


def test_help_and_version_exit_zero(capsys):
    assert cli.main(["--help"]) == 0
    assert cli.main(["--version"]) == 0
    assert "probe" in capsys.readouterr().out


def test_distribution_name_matches_pyproject():
    """`probe.__init__` looks the version up by DISTRIBUTION name, so a rename that
    misses one side fails silently: `probe --version` quietly degrades to the
    `0.0.0.dev0` source-tree fallback instead of erroring.

    (It is `probe-research`, not `probe-agent` — the latter is an unrelated project
    already on PyPI that we never owned.)
    """
    import tomllib

    import probe

    pyproject = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
    assert probe._DISTRIBUTION == pyproject["project"]["name"] == "probe-research"


def test_version_resolves_from_the_installed_distribution():
    """Guards the same seam from the other side: an installed tree must report a real
    version. The whole pitch is reproducibility — a client that cannot say what it is
    fails that on its own terms."""
    import probe

    if probe.__version__ == "0.0.0.dev0":
        pytest.skip("not an installed distribution (source tree)")
    assert probe.__version__ == metadata.version("probe-research")


def test_help_separates_hook_adapter_from_experiment_upload(capsys):
    # typer/click return an exit code from main() rather than raising SystemExit.
    rc = cli.main(["--help"])
    assert rc == 0
    output = capsys.readouterr().out
    assert "upload structured research knowledge" in output
    assert "internal coding-agent adapter commands" in output
