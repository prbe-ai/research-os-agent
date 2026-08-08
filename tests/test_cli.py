"""CLI smoke against the fake API (Client is swapped for the fake-backed one)."""

from __future__ import annotations

import io
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
    assert "pass at least one of --name/--description/--notes" in capsys.readouterr().err


def test_run_set_writes_notes_without_touching_the_description(wired, capsys):
    """The gap-3 shape: a run whose number is real but whose harness was not.

    `--notes` has to be able to land WITHOUT rewriting `--description`, because
    the caveat is learned after the run and the description is the only record of
    what the run was for. One field would force a trade.
    """
    cli.main(
        [
            "run",
            "start",
            "--experiment",
            "e",
            "--name",
            "smoke-oracle",
            "--description",
            "Oracle-patch smoke over one SWE-smith instance",
        ]
    )
    run_id = capsys.readouterr().out.strip()

    rc = cli.main(
        [
            "run",
            "set",
            run_id,
            "--notes",
            "Scored 0.0 by a broken verifier (pytest exit code, not per-test).",
        ]
    )

    assert rc == 0
    assert wired.runs[run_id]["notes"].startswith("Scored 0.0 by a broken verifier")
    assert wired.runs[run_id]["description"] == (
        "Oracle-patch smoke over one SWE-smith instance"
    )
    assert wired.runs[run_id]["name"] == "smoke-oracle"


def test_run_set_notes_reads_a_file_and_stdin(wired, capsys, tmp_path, monkeypatch):
    """Prose arrives from a heredoc or a file, not only a quoted argument."""
    cli.main(["run", "start", "--experiment", "e", "--name", "r1"])
    run_id = capsys.readouterr().out.strip()

    doc = tmp_path / "caveat.md"
    doc.write_text("# Why this is suspect\n\n40 of 113 tests were uncollectable.\n")
    assert cli.main(["run", "set", run_id, "--notes", f"@{doc}"]) == 0
    assert "uncollectable" in wired.runs[run_id]["notes"]

    monkeypatch.setattr("sys.stdin", io.StringIO("piped caveat\n"))
    assert cli.main(["run", "set", run_id, "--notes", "-"]) == 0
    assert wired.runs[run_id]["notes"] == "piped caveat\n"


def test_run_set_notes_empty_string_clears_rather_than_reading_as_absent(wired, capsys):
    """`--notes ''` is the documented clear, so it must not fall through to
    "no field passed" — that would exit 2 and leave the stale note in place."""
    cli.main(["run", "start", "--experiment", "e", "--name", "r1"])
    run_id = capsys.readouterr().out.strip()
    cli.main(["run", "set", run_id, "--notes", "temporary"])

    assert cli.main(["run", "set", run_id, "--notes", ""]) == 0
    assert wired.runs[run_id]["notes"] == ""


def test_group_notes_at_create_and_set(wired, capsys):
    cli.main(["run", "start", "--experiment", "e", "--name", "r1"])
    experiment_id = wired.runs[capsys.readouterr().out.strip()]["experiment_id"]

    rc = cli.main(
        [
            "group",
            "create",
            experiment_id,
            "--name",
            "h100-hunt",
            "--notes",
            "Chasing 16xH100 across us-central1.",
        ]
    )
    assert rc == 0
    group_id = json.loads(capsys.readouterr().out)["id"]
    assert wired.groups[group_id]["notes"] == "Chasing 16xH100 across us-central1."

    assert (
        cli.main(["group", "set", group_id, "--notes", "Abandoned: every zone exhausted."])
        == 0
    )
    assert wired.groups[group_id]["notes"] == "Abandoned: every zone exhausted."


def test_group_set_requires_a_field(wired, capsys):
    cli.main(["run", "start", "--experiment", "e", "--name", "r1"])
    experiment_id = wired.runs[capsys.readouterr().out.strip()]["experiment_id"]
    cli.main(["group", "create", experiment_id, "--name", "g"])
    group_id = json.loads(capsys.readouterr().out)["id"]

    assert cli.main(["group", "set", group_id]) == 2
    assert "pass at least one of --name/--spec/--notes" in capsys.readouterr().err


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


def test_artifact_notes_reach_the_wire_on_a_project_anchor(wired, capsys, tmp_path):
    """0095. `--meta` is run-only and ScopedUploadRequest forbids extras, so before
    `notes` a project upload had NO way to describe itself -- which is why agents
    concatenated the description onto `name` and broke the extension, the preview
    and the derived folder. The name must stay a bare path."""
    artifact = tmp_path / "tape_utils.py"
    artifact.write_text("import torch\n")

    assert cli.main(
        [
            "artifact",
            "add",
            str(artifact),
            "--project",
            "p",
            "--name",
            "odyssey/experiments/tape_utils.py",
            "--notes",
            "shared TAPE plumbing: prediction heads + tokenizer glue.",
        ]
    ) == 0

    presign = next(
        request for request in wired.requests if request.url.path.endswith("/artifacts/uploads")
    )
    body = json.loads(presign.content)
    assert body["notes"] == "shared TAPE plumbing: prediction heads + tokenizer glue."
    assert body["name"] == "odyssey/experiments/tape_utils.py"


def test_notes_are_not_rejected_as_a_run_only_field(wired, capsys, tmp_path):
    """`--kind/--step/--span/--meta` are refused on a non-run anchor. `--notes`
    must NOT join them: it is the one descriptive field every anchor accepts."""
    artifact = tmp_path / "f.csv"
    artifact.write_text("a,b\n")
    assert cli.main(
        ["artifact", "add", str(artifact), "--project", "p", "--notes", "a description"]
    ) == 0


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


# --- one vocabulary across the nouns -----------------------------------------


def test_every_addressable_kind_amends_with_set(wired, capsys):
    """`project patch` was the odd verb out; experiments, runs and groups `set`.

    An agent that learned `experiment set` and guessed `project set` got
    `No such command`, so the amend path it had just been told to use did not
    exist for one of the three kinds."""
    cli.main(["project", "create", "vocab-p"])

    assert cli.main(["project", "set", "vocab-p", "--description", "via set"]) == 0

    # The original spelling still resolves -- hidden, not removed, because it is
    # in scripts. Both must reach the same command.
    assert cli.main(["project", "set", "vocab-p", "--name", "A"]) == 0
    assert cli.main(["project", "patch", "vocab-p", "--name", "B"]) == 0


def test_every_addressable_kind_has_a_read_verb(wired, capsys):
    """Experiments had none at all, and a run's was only the top-level `probe get`.

    `project get` / `workspace get` / `group get` all existed, so the two gaps
    were exactly where someone would look first after writing something."""
    cli.main(["project", "create", "vocab-r"])
    cli.main(["experiment", "create", "vocab-e", "--project", "vocab-r",
              "--hypothesis", "h"])
    capsys.readouterr()

    assert cli.main(["experiment", "get", "vocab-e"]) == 0
    assert json.loads(capsys.readouterr().out)["slug"] == "vocab-e"

    cli.main(["run", "start", "--experiment", "vocab-e", "--name", "r"])
    run_id = capsys.readouterr().out.strip()

    assert cli.main(["run", "get", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == run_id
    # The older top-level spelling keeps working.
    assert cli.main(["get", run_id]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == run_id


# -- artifact move ------------------------------------------------------------
#
# The CLI's job here is small and exact: resolve the refs, pass the level and the
# target through, and relay whatever the server says. The rules about WHICH moves
# are legal live server-side and are moving right now (lateral project->project
# is landing), so every test below is about the relay, not the rules.


def seed_project_artifact(app, name="scorer.py"):
    """A project-anchored artifact, and its id."""
    project_id = next(p["id"] for p in app.projects.values() if p["slug"] == "p")
    cli.main([
        "artifact", "add", "--project", "p", "--uri", "s3://bucket/scorer.py",
        "--name", name,
    ])
    row = app.artifacts[f"project:{project_id}"][-1]
    return row["id"], project_id


def test_artifact_move_resolves_the_target_slug_and_relays_the_move(wired, capsys):
    artifact_id, _ = seed_project_artifact(wired)
    cli.main(["experiment", "create", "target-exp", "--project", "p", "--hypothesis", "h"])
    experiment_id = next(
        e["id"] for e in wired.experiments.values() if e["slug"] == "target-exp"
    )
    capsys.readouterr()

    rc = cli.main(["artifact", "move", artifact_id, "--to", "experiment",
                   "--target", "target-exp"])

    assert rc == 0
    moved = json.loads(capsys.readouterr().out)
    assert moved["id"] == artifact_id, "a move keeps the artifact's id"
    assert wired.moves == [{
        "artifact_id": artifact_id,
        "level": "experiment",
        # The slug was resolved to an id before it left: `--target target-exp`
        # must not reach a route whose path param is UUID-typed.
        "target_id": experiment_id,
    }]
    assert artifact_id in {a["id"] for a in wired.artifacts[f"experiment:{experiment_id}"]}


def test_artifact_move_accepts_an_id_prefixed_target(wired, capsys):
    artifact_id, _ = seed_project_artifact(wired)
    cli.main(["experiment", "create", "by-id", "--project", "p", "--hypothesis", "h"])
    experiment_id = next(
        e["id"] for e in wired.experiments.values() if e["slug"] == "by-id"
    )
    capsys.readouterr()

    rc = cli.main(["artifact", "move", artifact_id, "--to", "experiment",
                   "--target", f"id:{experiment_id}"])

    assert rc == 0
    assert wired.moves[-1]["target_id"] == experiment_id


def test_artifact_move_promote_sends_no_target_the_caller_did_not_give(wired, capsys):
    """A promote's destination follows from the artifact's own chain, server-side.
    The CLI must not fill one in -- inventing a target is how a promote silently
    becomes a move to somewhere else."""
    artifact_id, project_id = seed_project_artifact(wired)
    capsys.readouterr()

    assert cli.main(["artifact", "move", artifact_id, "--to", "project"]) == 0

    body = json.loads(wired.requests[-1].content)
    assert body == {"level": "project"}, "no target_id may be synthesized"
    assert wired.moves == [{
        "artifact_id": artifact_id, "level": "project", "target_id": project_id,
    }]


def test_artifact_move_relays_a_lateral_refusal_verbatim(wired, capsys):
    """Lateral (project -> project) is landing on the backend right now. Until it
    does, the server refuses -- and the CLI must say what the SERVER said, not
    gate on a rule of its own that would then have to be found and removed."""
    artifact_id, _ = seed_project_artifact(wired)
    cli.main(["project", "create", "elsewhere"])
    elsewhere = next(p["id"] for p in wired.projects.values() if p["slug"] == "elsewhere")
    wired.artifact_move_error = (
        422, "a lateral move needs target_id to sit inside the current subtree"
    )
    capsys.readouterr()

    rc = cli.main(["artifact", "move", artifact_id, "--to", "project",
                   "--target", "elsewhere"])

    assert rc != 0
    err = capsys.readouterr().err
    assert "a lateral move needs target_id to sit inside the current subtree" in err
    # The request was still SHAPED as a lateral move: the moment the backend
    # accepts it, this command works with no client change.
    body = json.loads(wired.requests[-1].content)
    assert body == {"level": "project", "target_id": elsewhere}


def test_artifact_move_relays_a_409_verbatim(wired, capsys):
    artifact_id, _ = seed_project_artifact(wired)
    wired.artifact_move_error = (
        409, {"message": "an identical artifact is already at that scope"}
    )
    capsys.readouterr()

    assert cli.main(["artifact", "move", artifact_id, "--to", "project"]) != 0
    assert "an identical artifact is already at that scope" in capsys.readouterr().err


def test_artifact_move_rejects_a_level_the_contract_does_not_declare(wired, capsys):
    artifact_id, _ = seed_project_artifact(wired)
    capsys.readouterr()
    # `workspace`/`shared` files move on the share/unshare rails, not here, so
    # AnchorLevel does not carry them and typer refuses before any request.
    assert cli.main(["artifact", "move", artifact_id, "--to", "workspace"]) != 0
    assert wired.moves == []


# -- metrics backfill (one batch, one request) --------------------------------


def derived_posts(app, run_id):
    return [b for b in app.metric_batches_posted if b.get("origin") == "derived"]


def test_metrics_backfill_sends_a_whole_curve_as_one_request(wired, tmp_path, capsys):
    """`probe log --derived` takes ONE step per invocation by design, so a
    20k-step backfill through it is 20k round trips. This is the batch door."""
    cli.main(["run", "start", "--experiment", "e", "--name", "r"])
    run_id = capsys.readouterr().out.strip()
    series = tmp_path / "curve.jsonl"
    series.write_text(
        "".join(json.dumps({"step": i, "value": i / 10}) + "\n" for i in range(200))
    )
    before = len([r for r in wired.requests if r.url.path == f"/v1/runs/{run_id}/metrics"])

    rc = cli.main([
        "metrics", "backfill", run_id, "--key", "eval/auroc",
        "--producer", "score_auc.py", "--from", str(series),
        "--note", "recomputed from logits", "--input", "eval/logits",
        "--code-ref", "scripts/score_auc.py@abc123",
    ])

    assert rc == 0
    out = capsys.readouterr().out
    assert "backfilled 200 derived point(s)" in out and "1 request" in out
    posts = [
        r for r in wired.requests
        if r.url.path == f"/v1/runs/{run_id}/metrics" and r.method == "POST"
    ]
    assert len(posts) - before == 1, f"a curve is ONE batch, got {len(posts) - before}"
    assert len(wired.metric_points_posted[run_id]) == 200

    (batch,) = derived_posts(wired, run_id)
    assert batch["origin"] == "derived"
    assert batch["provenance"]["producer"] == "score_auc.py"
    assert batch["provenance"]["note"] == "recomputed from logits"
    assert batch["provenance"]["code_ref"] == "scripts/score_auc.py@abc123"
    assert [p["step_index"] for p in batch["points"][:3]] == [0, 1, 2]


def test_metrics_backfill_reads_stdin_by_default(wired, monkeypatch, capsys):
    cli.main(["run", "start", "--experiment", "e", "--name", "r"])
    run_id = capsys.readouterr().out.strip()
    monkeypatch.setattr(
        "sys.stdin", io.StringIO('{"step": 1, "value": 0.5}\n{"step": 2, "value": 0.7}\n')
    )

    rc = cli.main(["metrics", "backfill", run_id, "--key", "k", "--producer", "agent"])

    assert rc == 0
    assert "backfilled 2 derived point(s)" in capsys.readouterr().out
    (batch,) = derived_posts(wired, run_id)
    assert [p["value"] for p in batch["points"]] == [0.5, 0.7]


def test_metrics_backfill_accepts_a_step_to_value_map_in_step_order(wired, tmp_path, capsys):
    """JSON object keys are strings, so insertion order puts step 10 before step
    2 and the backfilled curve reads as written out of order."""
    cli.main(["run", "start", "--experiment", "e", "--name", "r"])
    run_id = capsys.readouterr().out.strip()
    series = tmp_path / "curve.json"
    series.write_text(json.dumps({"10": 1.0, "2": 0.5, "1": 0.25}))

    assert cli.main([
        "metrics", "backfill", run_id, "--key", "k", "--producer", "agent",
        "--from", str(series),
    ]) == 0

    (batch,) = derived_posts(wired, run_id)
    assert [p["step_index"] for p in batch["points"]] == [1, 2, 10]


def test_metrics_backfill_accepts_a_json_array_of_pairs(wired, tmp_path, capsys):
    cli.main(["run", "start", "--experiment", "e", "--name", "r"])
    run_id = capsys.readouterr().out.strip()
    series = tmp_path / "curve.json"
    series.write_text(json.dumps([[0, 1.5], [1, 2.5]]))

    assert cli.main([
        "metrics", "backfill", run_id, "--key", "k", "--producer", "agent",
        "--from", str(series), "--dim", "rank=0", "--kind", "system",
    ]) == 0

    (batch,) = derived_posts(wired, run_id)
    assert [(p["step_index"], p["value"]) for p in batch["points"]] == [(0, 1.5), (1, 2.5)]
    assert all(p["dimensions"] == {"rank": 0} and p["kind"] == "system"
               for p in batch["points"])


def test_metrics_backfill_needs_provenance_and_points(wired, tmp_path, capsys):
    cli.main(["run", "start", "--experiment", "e", "--name", "r"])
    run_id = capsys.readouterr().out.strip()
    # A computed series without provenance is indistinguishable from a measured
    # one, so --producer is required by the parser, not by the server.
    assert cli.main(["metrics", "backfill", run_id, "--key", "k"]) != 0
    empty = tmp_path / "empty.jsonl"
    empty.write_text("\n\n")
    capsys.readouterr()
    assert cli.main([
        "metrics", "backfill", run_id, "--key", "k", "--producer", "a",
        "--from", str(empty),
    ]) != 0
    assert "no points" in capsys.readouterr().err
    assert wired.metric_batches_posted == []


def test_log_derived_single_step_is_unchanged(wired, capsys):
    """REGRESSION guard: the batch door is an ADDITION. `log --derived` keeps its
    one-step contract, its --step requirement and its synchronous path."""
    cli.main(["run", "start", "--experiment", "e", "--name", "r"])
    run_id = capsys.readouterr().out.strip()

    assert cli.main([
        "log", run_id, "acc=0.9", "--step", "7", "--derived", "--producer", "claude-code",
    ]) == 0
    assert "logged 1 derived metric(s)" in capsys.readouterr().out
    (batch,) = derived_posts(wired, run_id)
    assert batch["provenance"]["producer"] == "claude-code"
    assert [p["step_index"] for p in batch["points"]] == [7]

    # Still refuses a derived write with no step to land on.
    assert cli.main([
        "log", run_id, "acc=0.9", "--derived", "--producer", "claude-code",
    ]) != 0


def test_metrics_backfill_reads_a_one_row_jsonl_file(wired, tmp_path, capsys):
    """A single-line JSONL file is ALSO a valid whole JSON document, so the point
    spelling has to win over the step->value map spelling — otherwise one row
    parses as a map whose steps are the strings "step" and "value"."""
    cli.main(["run", "start", "--experiment", "e", "--name", "r"])
    run_id = capsys.readouterr().out.strip()
    series = tmp_path / "one.jsonl"
    series.write_text('{"step": 4, "value": 1.5}\n')

    assert cli.main([
        "metrics", "backfill", run_id, "--key", "k", "--producer", "agent",
        "--from", str(series),
    ]) == 0

    (batch,) = derived_posts(wired, run_id)
    assert [(p["step_index"], p["value"]) for p in batch["points"]] == [(4, 1.5)]


def test_metrics_backfill_rejects_a_non_numeric_step_as_a_usage_error(
    wired, tmp_path, capsys
):
    """Not a traceback: `main()` catches usage errors, and a hand-edited series
    with a bad key is a usage error."""
    cli.main(["run", "start", "--experiment", "e", "--name", "r"])
    run_id = capsys.readouterr().out.strip()
    series = tmp_path / "bad.json"
    series.write_text(json.dumps({"epoch-3": 1.0}))

    rc = cli.main([
        "metrics", "backfill", run_id, "--key", "k", "--producer", "agent",
        "--from", str(series),
    ])

    assert rc == 2
    assert "epoch-3" in capsys.readouterr().err
    assert wired.metric_batches_posted == []


# -- reproduce pull surface ---------------------------------------------------


def _start_run(capsys) -> str:
    cli.main(["run", "start", "--experiment", "e", "--name", "r1"])
    return capsys.readouterr().out.strip()


def test_run_reproduce_prints_the_server_record(wired, capsys):
    rid = _start_run(capsys)
    assert cli.main(["run", "reproduce", rid]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run"]["id"] == rid
    assert out["restore_command"].startswith("probe snapshot-restore")
    assert out["completeness"]["state"] in {"incomplete", "unverified"}


def test_run_reproduce_export_writes_a_portable_bundle(wired, capsys, tmp_path):
    rid = _start_run(capsys)
    dest = tmp_path / "repro.json"
    assert cli.main(["run", "reproduce", rid, "--export", str(dest)]) == 0
    assert f"wrote {dest}" in capsys.readouterr().out
    assert json.loads(dest.read_text())["run"]["id"] == rid


def test_experiment_reproduce_prints_run_summaries(wired, capsys):
    _start_run(capsys)  # a bare ref is a slug, so address the experiment by its slug "e"
    assert cli.main(["experiment", "reproduce", "e"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["completeness"]["total"] >= 1
    assert out["runs"][0]["reproduce_url"].endswith("/reproduce")


def test_experiment_freeze_mints_a_version(wired, capsys):
    _start_run(capsys)
    assert cli.main(["experiment", "freeze", "e", "--label", "v1"]) == 0
    minted = json.loads(capsys.readouterr().out)
    # freeze is an ergonomic alias for the version mint (POST .../versions)
    assert minted.get("label") == "v1" or "id" in minted


def test_run_reproduce_materialize_writes_inputs_and_manifest(wired, capsys, tmp_path, monkeypatch):
    from probe.sdk.client import Client

    rid = _start_run(capsys)
    # The fake's reproduce route returns empty inputs; substitute a record that
    # exercises the writer — one inlined input and one omitted (too large).
    record = {
        "run": {"id": rid, "env_ref": None, "config": {}},
        "restore_command": "probe snapshot-restore x",
        "inputs_decision": [
            {"artifact": {"name": "inputs-decision.json"}, "content": '{"dataset": "d1"}',
             "content_omitted_reason": None},
            {"artifact": {"name": "big.bin"}, "content": None,
             "content_omitted_reason": "too large to inline (2 MiB)"},
        ],
        "lockfiles": [],
        "completeness": {"state": "unverified", "missing": [], "advisories": []},
    }
    monkeypatch.setattr(Client, "run_reproduce", lambda self, run_id: record)

    dest = tmp_path / "work"
    assert cli.main(["run", "reproduce", rid, "--materialize", str(dest)]) == 0
    assert (dest / "reproduce-manifest.json").exists()
    assert json.loads((dest / "inputs-decision.json").read_text())["dataset"] == "d1"
    # an omitted input is NOT silently skipped — it leaves a marker naming the reason
    assert (dest / "big.bin.omitted").read_text().startswith("too large")


def test_run_reproduce_export_and_materialize_are_mutually_exclusive(wired, capsys, tmp_path):
    rid = _start_run(capsys)
    rc = cli.main(
        ["run", "reproduce", rid, "--export", str(tmp_path / "x.json"), "--materialize", str(tmp_path / "d")]
    )
    assert rc == 2  # BadParameter -> click usage exit code
