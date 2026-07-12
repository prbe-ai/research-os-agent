"""`exp` - the research-os CLI implementation, built on typer.

Thin wrapper over the SDK. The write path a coding agent (or a shell script) calls
to record experiment data. Data writes are fail-open (spool locally, never block).
Read convenience verbs (`get`, `bundle`) wrap the same read service the MCP tools use.

Connection flags (`--base-url/--token/--ingest-token/--hmac-secret`) are global and
go before the command: `exp --token ros_pat_x log RUN loss=0.1`. `login` also accepts
them directly so `exp login --token ...` works. Config lives in ~/.config/ros/config.json.

Auth: `exp login` pastes a `ros_pat_...` token (air-gap friendly); a device flow is
future work.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from enum import Enum
from typing import Any

import click
import typer

from .. import __version__, errors
from ..sdk.client import Client
from ..sdk.config import clear_file, load_file, resolve, save_file


# -- global connection state (set by the root callback) ---------------------
@dataclass
class Conn:
    base_url: str | None = None
    token: str | None = None
    ingest_token: str | None = None
    hmac_secret: str | None = None


_conn = Conn()


# -- enums (choices) --------------------------------------------------------
class Relation(str, Enum):
    fork = "fork"
    resume = "resume"
    retry = "retry"
    branch = "branch"


class EndStatus(str, Enum):
    completed = "completed"
    failed = "failed"
    crashed = "crashed"
    canceled = "canceled"


class EventKind(str, Enum):
    intent = "intent"
    hypothesis = "hypothesis"
    decision = "decision"
    observation = "observation"
    failure = "failure"
    result = "result"
    deviation = "deviation"
    next_step = "next_step"


class AssetMode(str, Enum):
    readonly = "readonly"
    copy = "copy"


# -- helpers ----------------------------------------------------------------
def _kv_pairs(items: list[str] | None, *, cast_float: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise typer.BadParameter(f"expected key=value, got: {item!r}")
        key, _, raw = item.partition("=")
        if cast_float:
            try:
                out[key] = float(raw)
            except ValueError as exc:
                raise typer.BadParameter(f"metric {key!r} must be numeric, got {raw!r}") from exc
        else:
            try:
                out[key] = json.loads(raw)
            except json.JSONDecodeError:
                out[key] = raw
    return out


def _json_value(raw: str | None) -> dict | None:
    if raw is None:
        return None
    if raw.startswith("@"):
        from pathlib import Path

        raw = Path(raw[1:]).read_text()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise typer.BadParameter("expected a JSON object")
    return value


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _client() -> Client:
    # `Client` is a module global so the CLI package can monkeypatch it in tests.
    return Client(
        base_url=_conn.base_url,
        token=_conn.token,
        ingest_token=_conn.ingest_token,
        hmac_secret=_conn.hmac_secret,
    )


def _run_handle(client: Client, run_id: str):
    from ..sdk.run import Run

    return Run(client, client.get_run(run_id))


def _version_cb(value: bool) -> None:
    if value:
        typer.echo(f"exp {__version__}")
        raise typer.Exit()


# -- app --------------------------------------------------------------------
app = typer.Typer(
    name="exp",
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Research OS CLI. Run/event/artifact commands upload experiments; "
        "the `hook` group is reserved for deterministic coding-agent adapters."
    ),
)


@app.callback()
def _root(
    base_url: str = typer.Option(None, "--base-url"),
    token: str = typer.Option(None, "--token"),
    ingest_token: str = typer.Option(None, "--ingest-token"),
    hmac_secret: str = typer.Option(None, "--hmac-secret"),
    version: bool = typer.Option(
        False, "--version", callback=_version_cb, is_eager=True, help="show version"
    ),
) -> None:
    _conn.base_url = base_url
    _conn.token = token
    _conn.ingest_token = ingest_token
    _conn.hmac_secret = hmac_secret


# -- auth -------------------------------------------------------------------
@app.command()
def login(
    base_url: str = typer.Option(None, "--base-url"),
    token: str = typer.Option(None, "--token"),
    ingest_token: str = typer.Option(None, "--ingest-token"),
    hmac_secret: str = typer.Option(None, "--hmac-secret"),
) -> None:
    """Save endpoint + token, verifying the user token if present."""
    data = load_file()
    settings = resolve(
        base_url=base_url or _conn.base_url,
        token=token or _conn.token,
        ingest_token=ingest_token or _conn.ingest_token,
        hmac_secret=hmac_secret or _conn.hmac_secret,
    )
    data["base_url"] = settings.base_url
    if settings.token:
        data["token"] = settings.token
    if settings.ingest_token:
        data["ingest_token"] = settings.ingest_token
    if settings.hmac_secret:
        data["hmac_secret"] = settings.hmac_secret
    if settings.token:
        with Client(settings=settings) as c:
            who = c.me()
        print(f"logged in to {settings.base_url} as {who.get('email', who)}")
    else:
        print(f"saved endpoint {settings.base_url} (no user token set)")
    path = save_file(data)
    print(f"config: {path}")


@app.command()
def logout() -> None:
    """Revoke the calling token and clear local config."""
    try:
        with _client() as c:
            c.logout()
        print("token revoked")
    except errors.RosError as exc:
        print(f"revoke skipped ({exc})", file=sys.stderr)
    clear_file()
    print("local config cleared")


@app.command()
def whoami() -> None:
    """Show the current principal."""
    with _client() as c:
        _print_json(c.me())


# -- run lifecycle ----------------------------------------------------------
run_app = typer.Typer(no_args_is_help=True, help="run lifecycle")
app.add_typer(run_app, name="run")


@run_app.command("start")
def run_start(
    experiment: str = typer.Option(..., "--experiment"),
    hypothesis: str = typer.Option(..., "--hypothesis"),
    name: str = typer.Option(..., "--name"),
    experiment_name: str = typer.Option(None, "--experiment-name"),
    project: str = typer.Option(None, "--project"),
    source: str = typer.Option("api", "--source"),
    external_id: str = typer.Option(None, "--external-id"),
    config: list[str] = typer.Option(None, "--config", metavar="k=v"),
    tag: list[str] = typer.Option(None, "--tag"),
) -> None:
    """Open a run (creating its experiment/project as needed)."""
    with _client() as c:
        run = c.run(
            experiment=experiment,
            experiment_name=experiment_name,
            hypothesis=hypothesis,
            name=name,
            project=project,
            source=source,
            external_id=external_id,
            config=_kv_pairs(config) if config else None,
            tags=tag or None,
        )
    print(run.id)


@run_app.command("child")
def run_child(
    run: str = typer.Argument(...),
    name: str = typer.Option(..., "--name"),
    relation: Relation = typer.Option(Relation.fork, "--relation"),
    source: str = typer.Option("api", "--source"),
    external_id: str = typer.Option(None, "--external-id"),
) -> None:
    """Open a sub-run under an existing run."""
    with _client() as c:
        parent = c.get_run(run)
        child = c.create_run(
            parent["experiment_id"],
            name,
            parent_run_id=run,
            parent_relation=relation.value,
            source=source,
            external_id=external_id,
        )
    print(child.id)


@run_app.command("end")
def run_end(
    run: str = typer.Argument(...),
    status: EndStatus = typer.Option(EndStatus.completed, "--status"),
) -> None:
    """Close a run."""
    with _client() as c:
        _run_handle(c, run).finish(status.value)
    print(f"{run} -> {status.value}")


@run_app.command("check")
def run_check(run: str = typer.Argument(...)) -> None:
    """Assess capture completeness (exit 2 if incomplete)."""
    with _client() as c:
        result = c.check_run(run)
    _print_json(result)
    if result.get("state") != "complete":
        raise typer.Exit(2)


# -- exec (process correlation) ---------------------------------------------
@app.command(
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
    help="execute a local command with run/process correlation: exp exec RUN -- cmd ...",
)
def exec(
    ctx: typer.Context,
    run: str = typer.Argument(...),
    cwd: str = typer.Option(None, "--cwd"),
) -> None:
    argv = list(ctx.args)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        raise typer.BadParameter("exp exec requires a command after --")
    with _client() as c:
        result = _run_handle(c, run).execute(argv, cwd=cwd)
    raise typer.Exit(result.returncode)


# -- metrics ----------------------------------------------------------------
@app.command()
def log(
    run: str = typer.Argument(...),
    metric: list[str] = typer.Argument(..., metavar="key=value..."),
    step: int = typer.Option(None, "--step"),
    kind: str = typer.Option("model", "--kind"),
    dim: list[str] = typer.Option(None, "--dim", metavar="k=v"),
) -> None:
    """Append metric points. --dim adds series dimensions (fold #9)."""
    metrics = _kv_pairs(metric, cast_float=True)
    dims = _kv_pairs(dim) if dim else None
    with _client() as c:
        _run_handle(c, run).log(metrics, step=step, kind=kind, dimensions=dims)
    print(f"logged {len(metrics)} metric(s) to {run}")


# -- spans ------------------------------------------------------------------
span_app = typer.Typer(no_args_is_help=True, help="trajectory spans")
app.add_typer(span_app, name="span")


@span_app.command("add")
def span_add(
    run: str = typer.Argument(...),
    span_type: str = typer.Option(..., "--type"),
    name: str = typer.Option(None, "--name"),
    step: int = typer.Option(None, "--step"),
    provider: str = typer.Option(None, "--provider"),
    external_key: str = typer.Option(None, "--external-key"),
    parent: str = typer.Option(None, "--parent"),
    status: str = typer.Option("running", "--status"),
    attr: list[str] = typer.Option(None, "--attr", metavar="k=v"),
) -> None:
    """Upsert a span."""
    with _client() as c:
        span_id = _run_handle(c, run).span(
            span_type,
            name=name,
            step_index=step,
            provider=provider,
            external_key=external_key,
            parent_span_id=parent,
            status=status,
            attributes=_kv_pairs(attr) if attr else None,
        )
    print(span_id)


# -- artifacts --------------------------------------------------------------
artifact_app = typer.Typer(no_args_is_help=True, help="artifacts")
app.add_typer(artifact_app, name="artifact")


@artifact_app.command("add")
def artifact_add(
    run: str = typer.Argument(...),
    path: str = typer.Argument(None),
    uri: str = typer.Option(None, "--uri"),
    name: str = typer.Option(None, "--name"),
    kind: str = typer.Option("file", "--kind"),
    step: int = typer.Option(None, "--step"),
) -> None:
    """Record an artifact."""
    resolved = name
    if resolved is None and path:
        import os

        resolved = os.path.basename(path)
    if resolved is None:
        raise typer.BadParameter("artifact needs --name (or a path to derive it from)")
    with _client() as c:
        _run_handle(c, run).log_artifact(
            resolved, path=path, uri=uri, kind=kind, step_index=step
        )
    print(f"artifact {resolved!r} recorded on {run}")


# -- link / snapshot / flush / reads ----------------------------------------
@app.command()
def link(
    run: str = typer.Argument(...),
    set_pairs: list[str] = typer.Option(..., "--set", metavar="k=v"),
) -> None:
    """Attach foreign keys (stored under metadata.foreign_keys)."""
    keys = _kv_pairs(set_pairs)
    with _client() as c:
        _run_handle(c, run).link(**keys)
    print(f"linked {', '.join(keys)} to {run}")


@app.command()
def snapshot(
    run: str = typer.Argument(...),
    cwd: str = typer.Option(None, "--cwd"),
    no_env: bool = typer.Option(False, "--no-env"),
    no_gpu: bool = typer.Option(False, "--no-gpu"),
) -> None:
    """Non-disruptive code + env capture."""
    with _client() as c:
        snap = _run_handle(c, run).snapshot(
            cwd=cwd, include_env=not no_env, include_gpu=not no_gpu
        )
    print(f"snapshot {snap['git']['commit'][:12]} -> {snap['git']['ref']}")


@app.command()
def flush() -> None:
    """Replay spooled writes."""
    with _client() as c:
        sent = c.flush()
    print(f"flushed {sent} spooled write(s)")


@app.command()
def get(
    run: str = typer.Argument(...),
    include_deleted: bool = typer.Option(False, "--include-deleted"),
) -> None:
    """Print a run."""
    with _client() as c:
        _print_json(c.get_run(run, include_deleted=include_deleted))


@app.command()
def bundle(run: str = typer.Argument(...)) -> None:
    """Print a run bundle (run + series + artifacts)."""
    with _client() as c:
        _print_json(c.run_bundle(run))


# -- structured research notes ----------------------------------------------
# (backend `events` are server-emitted + read-only; a research note is stored as a
# kind="note" artifact. `exp events` reads the backend lifecycle log.)
note_app = typer.Typer(no_args_is_help=True, help="upload structured research knowledge")
app.add_typer(note_app, name="note")


@note_app.command("add")
def note_add(
    run: str = typer.Argument(...),
    kind: EventKind = typer.Option(..., "--kind"),
    statement: str = typer.Option(..., "--statement"),
    evidence: list[str] = typer.Option(None, "--evidence"),
    authority: str = typer.Option("agent_summarized", "--authority"),
    confidence: float = typer.Option(None, "--confidence"),
    supersedes: str = typer.Option(None, "--supersedes"),
    meta: list[str] = typer.Option(None, "--meta", metavar="k=v"),
) -> None:
    """Append a research note (normal experiment upload; agents/researchers/SDK)."""
    with _client() as c:
        result = c.notes.add(
            run,
            kind.value,
            statement,
            evidence_refs=evidence,
            authority=authority,
            confidence=confidence,
            supersedes=supersedes,
            metadata=_kv_pairs(meta) if meta else None,
        )
    _print_json(result)


@app.command()
def events(run: str = typer.Argument(...)) -> None:
    """Read the backend lifecycle events for a run (fold #10, read-only)."""
    with _client() as c:
        _print_json(c.events.for_run(run))


# -- reserved hook adapter ABI (no hooks installed this release) -------------
hook_app = typer.Typer(
    no_args_is_help=True,
    help="internal coding-agent adapter commands (hooks are not installed yet)",
)
session_app = typer.Typer(no_args_is_help=True, help="correlate/checkpoint coding-agent sessions")
hook_app.add_typer(session_app, name="session")
app.add_typer(hook_app, name="hook")


@session_app.command("attach")
def hook_session_attach(
    run: str = typer.Argument(...),
    session_id: str = typer.Option(..., "--session-id"),
    agent: str = typer.Option("claude-code", "--agent"),
    transcript_path: str = typer.Option(None, "--transcript-path"),
    cwd: str = typer.Option(None, "--cwd"),
) -> None:
    with _client() as c:
        result = c.sessions.attach(
            run, session_id, agent=agent, transcript_path=transcript_path, cwd=cwd
        )
    _print_json(result)


@session_app.command("checkpoint")
def hook_session_checkpoint(
    run: str = typer.Argument(...),
    session_id: str = typer.Option(..., "--session-id"),
    transcript_path: str = typer.Option(None, "--transcript-path"),
    reason: str = typer.Option("checkpoint", "--reason"),
) -> None:
    with _client() as c:
        result = c.sessions.checkpoint(
            run, session_id, transcript_path=transcript_path, reason=reason
        )
    _print_json(result)


@session_app.command("detach")
def hook_session_detach(
    run: str = typer.Argument(...),
    session_id: str = typer.Option(..., "--session-id"),
    reason: str = typer.Option("session_end", "--reason"),
) -> None:
    with _client() as c:
        result = c.sessions.detach(run, session_id, reason=reason)
    _print_json(result)


# -- reusable asset registry (fold #5) --------------------------------------
asset_app = typer.Typer(no_args_is_help=True, help="named asset registry + zero-copy versions")
app.add_typer(asset_app, name="asset")


@asset_app.command("register")
def asset_register(
    name: str = typer.Argument(...),
    kind: str = typer.Option("dataset", "--kind"),
    description: str = typer.Option(None, "--description"),
    tag: list[str] = typer.Option(None, "--tag"),
) -> None:
    """Create a named asset (409 if the name already exists)."""
    with _client() as c:
        result = c.assets.register(name, kind=kind, description=description, tags=tag or None)
    _print_json(result)


@asset_app.command("version")
def asset_version(
    asset_id: str = typer.Argument(...),
    from_artifact: str = typer.Option(None, "--from-artifact", help="zero-copy from an artifact id"),
    content_hash: str = typer.Option(None, "--content-hash"),
    uri: str = typer.Option(None, "--uri"),
    label: str = typer.Option(None, "--label"),
) -> None:
    """Pin a new immutable version (zero-copy from an artifact, or by content_hash)."""
    with _client() as c:
        result = c.assets.add_version(
            asset_id, from_artifact_id=from_artifact, content_hash=content_hash, uri=uri, label=label
        )
    _print_json(result)


@asset_app.command("list")
def asset_list() -> None:
    """List assets."""
    with _client() as c:
        _print_json(c.assets.list().items)


@asset_app.command("materialize")
def asset_materialize(
    name: str = typer.Argument(...),
    to: str = typer.Option(..., "--to", help="local destination path"),
    kind: str = typer.Option(None, "--kind"),
    requirement: str = typer.Option(None, "--requirement", help="exact version number or label"),
) -> None:
    """Download a pinned asset version's bytes to a local path."""
    with _client() as c:
        result = c.assets.materialize(name, to, kind=kind, requirement=requirement)
    _print_json(result)


# -- lineage edges (fold #2) ------------------------------------------------
edge_app = typer.Typer(no_args_is_help=True, help="lineage edges (run/artifact/asset_version)")
app.add_typer(edge_app, name="edge")


@edge_app.command("add")
def edge_add(
    source: str = typer.Option(..., "--source", metavar="type:id"),
    relation: str = typer.Option(..., "--relation"),
    target: str = typer.Option(..., "--target", metavar="type:id"),
) -> None:
    """Add a lineage edge. --source/--target are `type:id` (type in run|artifact|asset_version)."""
    st, _, sid = source.partition(":")
    tt, _, tid = target.partition(":")
    if not sid or not tid:
        raise typer.BadParameter("source/target must be `type:id`")
    with _client() as c:
        result = c.add_edge(
            source_type=st, source_id=sid, relation=relation, target_type=tt, target_id=tid
        )
    _print_json(result)


# -- experiment versions (fold #6) ------------------------------------------
version_app = typer.Typer(no_args_is_help=True, help="immutable experiment version manifests")
app.add_typer(version_app, name="version")


@version_app.command("create")
def version_create(
    experiment_id: str = typer.Argument(...),
    label: str = typer.Option(None, "--label"),
) -> None:
    """Mint an immutable experiment version (launch-time manifest)."""
    with _client() as c:
        result = c.experiment_version(experiment_id, label=label)
    _print_json(result)


@version_app.command("list")
def version_list(experiment_id: str = typer.Argument(...)) -> None:
    """List an experiment's versions."""
    with _client() as c:
        _print_json(c.list_experiment_versions(experiment_id))


# -- entrypoint -------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """Run the CLI, returning a process exit code (never calls sys.exit itself)."""
    try:
        result = app(args=argv, prog_name="exp", standalone_mode=False)
    except click.exceptions.Exit as exc:  # --help, --version, explicit typer.Exit
        return int(exc.exit_code)
    except click.exceptions.Abort:
        print("aborted", file=sys.stderr)
        return 1
    except click.ClickException as exc:  # usage / bad-parameter errors
        exc.show()
        return exc.exit_code or 2
    except errors.RosError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except SystemExit as exc:  # defensive: coerce any stray SystemExit to a code
        code = exc.code
        if isinstance(code, int):
            return code
        if code is not None:
            print(str(code), file=sys.stderr)
            return 1
        return 0
    return result if isinstance(result, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
