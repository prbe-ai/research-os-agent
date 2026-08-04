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
    # `run start` resolves its experiment instead of creating it, so the smoke
    # tests below need one to exist. Seeding it through the CLI rather than the
    # fake's dict keeps the precondition honest — and exercises `experiment
    # create`, which is the command that replaced the implicit path.
    cli.main(["project", "create", "p"])
    cli.main(
        ["experiment", "create", "e", "--hypothesis", "h", "--project", "p"]
    )
    return app


def test_run_start_prints_id(wired, capsys):
    rc = cli.main(
        [
            "run",
            "start",
            "--experiment",
            "e",
            "--name",
            "r1",
            "--description",
            "Initial run context",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out.strip()
    assert out in wired.runs
    assert wired.runs[out]["description"] == "Initial run context"


def test_run_set_updates_title_and_description(wired, capsys):
    cli.main(["run", "start", "--experiment", "e", "--name", "r1"])
    run_id = capsys.readouterr().out.strip()

    rc = cli.main(
        [
            "run",
            "set",
            run_id,
            "--name",
            "Named baseline",
            "--description",
            "Operator-authored context",
        ]
    )

    assert rc == 0
    assert wired.runs[run_id]["name"] == "Named baseline"
    assert wired.runs[run_id]["description"] == "Operator-authored context"

    assert cli.main(["run", "set", run_id, "--name", "Retitled baseline"]) == 0
    assert wired.runs[run_id]["name"] == "Retitled baseline"
    assert wired.runs[run_id]["description"] == "Operator-authored context"


def test_run_set_updates_description_without_changing_title(wired, capsys):
    cli.main(["run", "start", "--experiment", "e", "--name", "keep-me"])
    run_id = capsys.readouterr().out.strip()

    rc = cli.main(
        ["run", "set", run_id, "--description", "Description-only edit"]
    )

    assert rc == 0
    assert wired.runs[run_id]["name"] == "keep-me"
    assert wired.runs[run_id]["description"] == "Description-only edit"


def test_run_set_requires_a_field(wired, capsys):
    cli.main(["run", "start", "--experiment", "e", "--name", "r1"])
    run_id = capsys.readouterr().out.strip()

    assert cli.main(["run", "set", run_id]) == 2
    assert "pass at least one of --name/--description" in capsys.readouterr().err


def test_log_command(wired, capsys):
    # make a run first
    cli.main(["run", "start", "--experiment", "e", "--name", "r1"])
    run_id = capsys.readouterr().out.strip()
    rc = cli.main(["log", run_id, "loss=0.42", "acc=0.9", "--step", "3"])
    assert rc == 0
    assert wired.metrics_inserted == 2
    body = json.loads(wired.requests[-1].content)
    assert body["points"][0]["step_index"] == 3


def test_link_command(wired, capsys):
    cli.main(["run", "start", "--experiment", "e", "--name", "r1"])
    run_id = capsys.readouterr().out.strip()
    rc = cli.main(["link", run_id, "--set", "wandb_run_id=abc", "--set", "gpu_job=rp-1"])
    assert rc == 0
    assert wired.runs[run_id]["foreign_keys"] == {
        "wandb_run_id": "abc",
        "gpu_job": "rp-1",
    }


def test_child_command(wired, capsys):
    cli.main(["run", "start", "--experiment", "e", "--name", "r1"])
    parent = capsys.readouterr().out.strip()
    rc = cli.main(
        [
            "run",
            "child",
            parent,
            "--name",
            "step-1",
            "--description",
            "Resumed from the baseline",
            "--relation",
            "resume",
        ]
    )
    assert rc == 0
    child = capsys.readouterr().out.strip()
    assert wired.runs[child]["parent_run_id"] == parent
    assert wired.runs[child]["parent_relation"] == "resume"
    assert wired.runs[child]["description"] == "Resumed from the baseline"


def test_artifact_add_forwards_span_content_type_and_meta(wired, capsys, tmp_path):
    cli.main(["run", "start", "--experiment", "e", "--name", "r1"])
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
    app.seed_experiment("e")
    durable = tmp_path / "shared-pvc" / "spool"
    assert cli.main(
        [
            "--spool-dir",
            str(durable),
            "run",
            "start",
            "--experiment",
            "e",
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


def test_help_offers_upload_and_no_stale_hook_adapter(capsys):
    """The `hook session` adapter is GONE; session linking is automatic now.

    This used to assert the hook adapter appeared in help, separated from the
    upload command. That adapter recorded a run's coding session into a
    free-form metadata field which the ingest upsert overwrites wholesale on
    re-push (`metadata = EXCLUDED.metadata`), so anything stored there silently
    vanished. Attribution now rides a request header into runs.foreign_keys and
    run_sessions, which merge instead. Kept as an inverted guard so the dead
    command cannot quietly reappear alongside the automatic path.
    """
    # typer/click return an exit code from main() rather than raising SystemExit.
    rc = cli.main(["--help"])
    assert rc == 0
    output = capsys.readouterr().out
    assert "the project's notes" in output
    assert "internal coding-agent adapter commands" not in output
    # Invoking it must fail outright, not just be undocumented. Asserted by
    # exit code rather than by scanning help text for "hook": rich wraps the
    # help panel at terminal width, so substring checks there are width-
    # dependent and fail for reasons that have nothing to do with the command.
    assert cli.main(["hook", "session", "attach", "r", "--session-id", "s"]) != 0


def test_a_backend_error_exits_nonzero(wired, capsys, monkeypatch):
    """A failed request must set a FAILING exit code, not print and exit 0.

    A CLI that reports an error on stderr while exiting 0 is worse than one that
    crashes: every `set -e` script, CI step, and agent loop treats it as success
    and keeps going on data it never got. Locked in here because the asset-group
    removal was prompted by exactly this shape of report.
    """
    import importlib

    from probe.sdk import errors

    # `probe.cli.main` the SUBMODULE, not `probe.cli.main` the re-exported function
    # that shadows it on the package.
    impl = importlib.import_module("probe.cli.main")

    def boom(*_a, **_kw):
        raise errors.error_for(404, "Not Found")

    monkeypatch.setattr(impl, "_print_json", boom)
    rc = cli.main(["artifact", "versions", "00000000-0000-4000-8000-000000000000"])
    assert rc == 1, "a failed request must not exit 0"
    # Reported on stderr, so stdout stays clean for callers piping JSON.
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ")
    assert captured.out == ""


def test_the_asset_command_group_is_gone(wired):
    """`probe asset ...` 404s against the backend now that /v1/assets is deleted,
    so the group is removed rather than left to fail at runtime. An unknown
    command is a usage error (exit 2), never a success."""
    assert cli.main(["asset", "list"]) == 2


def test_artifact_list_accepts_a_project_slug(wired, capsys):
    """`--project` takes the slug people remember, not only the uuid.

    Every ``/v1/projects/{project_id}`` route types the path param as a UUID, so
    a slug reached the server as a 422 about UUID parsing rather than a lookup.
    `experiment list --project` and `project patch` both took a slug, so the one
    listing that did not was indistinguishable from a malformed request.
    """
    assert cli.main(["artifact", "list", "--project", "p"]) == 0

    listing = next(
        r for r in wired.requests if "/artifacts" in r.url.path and r.method == "GET"
    )
    assert "p" not in listing.url.path.rsplit("/", 1)[-1], (
        "the slug reached the server unresolved"
    )


def _one_artifact(tmp_path, capsys) -> str:
    """Create a run + artifact through the CLI and return the artifact id."""
    cli.main(["run", "start", "--experiment", "e", "--name", "r-del"])
    run_id = capsys.readouterr().out.strip()
    f = tmp_path / "a.txt"
    f.write_text("x\n")
    cli.main(["artifact", "add", run_id, str(f), "--name", "a.txt"])
    capsys.readouterr()
    cli.main(["artifact", "list", run_id])
    listed = json.loads(capsys.readouterr().out.strip())
    return listed[0]["id"]


def _spy_confirm(monkeypatch):
    """Record confirm calls. `probe.cli.main` is the entry-point FUNCTION
    re-exported in __init__, which shadows the submodule of the same name."""
    import importlib

    cli_main = importlib.import_module("probe.cli.main")
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        cli_main.typer, "confirm", lambda text, **kw: (seen.append((text, kw)), True)[1]
    )
    return seen


def test_artifact_delete_confirms_like_every_other_delete(
    wired, capsys, monkeypatch, tmp_path
):
    """`artifact delete` took no --yes and prompted for nothing.

    Its four siblings all guard themselves, so passing --yes here — the habit the
    rest of the CLI teaches — failed the whole batch instead. That was the lucky
    outcome: accepted-and-ignored would have destroyed the rows before anyone
    read them.
    """
    artifact_id = _one_artifact(tmp_path, capsys)
    seen = _spy_confirm(monkeypatch)

    assert cli.main(["artifact", "delete", artifact_id]) == 0
    assert len(seen) == 1
    text, kwargs = seen[0]
    assert f"permanently delete artifact {artifact_id}" in text
    assert kwargs.get("abort") is True, "a declined prompt must abort, not fall through"


def test_delete_yes_skips_the_prompt(wired, capsys, monkeypatch, tmp_path):
    """--yes is the documented escape hatch on every delete verb, now including this one."""
    artifact_id = _one_artifact(tmp_path, capsys)
    seen = _spy_confirm(monkeypatch)

    assert cli.main(["artifact", "delete", artifact_id, "--yes"]) == 0
    assert seen == []
