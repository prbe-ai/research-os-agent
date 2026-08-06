"""``probe wandb ...`` — a self-contained Typer sub-app over connectors.wandb.

NOT WIRED YET. ``cli/main.py`` is owned by another lane this cycle, so the one
line that mounts this app lives there and is not written here::

    from .wandb_import import app as wandb_app   # with the other cli imports
    app.add_typer(wandb_app, name="wandb")       # beside the other add_typer calls

Everything below works the moment that line lands; nothing else needs changing.

The commands are deliberately thin — every decision (tier reporting, redaction,
batching, the project-is-an-input rule) belongs to the connector, so the CLI and
a programmatic caller cannot drift apart.
"""

from __future__ import annotations

import json

import typer

from ..connectors import wandb as wb

app = typer.Typer(help="Import Weights & Biases runs into Probe.", no_args_is_help=True)
key_app = typer.Typer(help="Manage the stored W&B API key.", no_args_is_help=True)
app.add_typer(key_app, name="key")


def _echo(payload: object) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))


@app.command("discover")
def discover(root: str = typer.Argument(..., help="Folder tree to scan.")) -> None:
    """List the W&B run directories under ROOT and the tier each would import at."""
    rows = []
    for run in wb.read_local_runs(root):
        rows.append(
            {
                "run_id": run.run_id,
                "project": run.project,
                "name": run.display_name,
                "tier": run.tier.value,
                "metrics": len(run.metrics),
                "points": run.total_points,
                "coverage": run.coverage_note(),
                "source": run.source,
                "warnings": run.warnings,
            }
        )
    _echo(rows)


@app.command("import-local")
def import_local(
    root: str = typer.Argument(..., help="Folder tree holding wandb/ run dirs."),
    project: str = typer.Option(
        ...,
        "--project",
        help="EXISTING Probe project to import into. Required: a W&B project "
        "does not map 1:1 onto a Probe project, and this runs after a file import.",
    ),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report the plan, write nothing."),
) -> None:
    """Import every readable W&B run under ROOT into an existing Probe project."""
    from ..sdk.client import Client

    client = Client()
    results = wb.import_local_runs(client, root, project=project, dry_run=dry_run)
    _echo(
        [
            {
                "wandb_run_id": r.wandb_run_id,
                "probe_run_id": r.probe_run_id,
                "tier": r.tier.value,
                "metrics_written": r.metrics_written,
                "points_written": r.points_written,
                "requests": r.requests,
                "warnings": r.warnings,
            }
            for r in results
        ]
    )


@app.command("import-hosted")
def import_hosted(
    entity: str = typer.Argument(..., help="W&B entity (team or user)."),
    wandb_project: str = typer.Argument(..., help="W&B project name."),
    project: str = typer.Option(..., "--project", help="EXISTING Probe project."),
    run_id: list[str] = typer.Option(None, "--run", help="Limit to these W&B run ids."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report the plan, write nothing."),
) -> None:
    """Pull runs from wandb.ai and import them into an existing Probe project."""
    from ..sdk.client import Client

    client = Client()
    runs = wb.fetch_hosted_runs(entity, wandb_project, run_ids=run_id or None)
    results = [
        wb.import_wandb_run(client, run, project=project, dry_run=dry_run) for run in runs
    ]
    _echo(
        [
            {
                "wandb_run_id": r.wandb_run_id,
                "probe_run_id": r.probe_run_id,
                "tier": r.tier.value,
                "points_written": r.points_written,
            }
            for r in results
        ]
    )


@key_app.command("set")
def key_set(
    key: str = typer.Option(
        ...,
        "--key",
        prompt=True,
        hide_input=True,
        help="W&B API key from https://wandb.ai/authorize.",
    ),
) -> None:
    """Store a W&B API key in the active probe config context.

    Prompted with the input hidden and never echoed back — the confirmation
    prints where it landed, not what was stored.
    """
    path = wb.store_api_key(key)
    typer.echo(f"stored the W&B API key in {path}")


@key_app.command("status")
def key_status() -> None:
    """Report whether a key is configured and where it comes from. Never prints it."""
    _echo(wb.api_key_status())
