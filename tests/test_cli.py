"""CLI smoke against the fake API (Client is swapped for the fake-backed one)."""

from __future__ import annotations

import json

import pytest

from probe import cli
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


def test_help_separates_hook_adapter_from_experiment_upload(capsys):
    # typer/click return an exit code from main() rather than raising SystemExit.
    rc = cli.main(["--help"])
    assert rc == 0
    output = capsys.readouterr().out
    assert "upload structured research knowledge" in output
    assert "internal coding-agent adapter commands" in output
