"""`probe` - the Probe Research CLI implementation, built on typer.

Thin wrapper over the SDK. The write path a coding agent (or a shell script) calls
to record experiment data. Data writes are fail-open (spool locally, never block).
Read convenience verbs (`get`, `bundle`) wrap the same read service the MCP tools use.

Connection flags (`--base-url/--token/--ingest-token/--hmac-secret`) are global and
go before the command: `probe --token probe_pat_x log RUN loss=0.1`. `login` also accepts
them directly so `probe login --token ...` works. Config lives in ~/.config/probe/config.json.

Auth: `probe login --device` runs the browser handoff (RFC 8628) and captures the
`probe_pat_...` token; `probe login --token probe_pat_...` is the air-gap paste path.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import click
import typer

from .. import __version__, errors
from ..sdk.client import Client
from ..sdk.config import clear_file, config_path, load_file, resolve, save_file
from ..sdk.device import DeviceLoginError, DevicePrompt, device_login, hostname


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
        typer.echo(f"probe {__version__}")
        raise typer.Exit()


# -- app --------------------------------------------------------------------
app = typer.Typer(
    name="probe",
    no_args_is_help=True,
    add_completion=False,
    help=(
        "Probe Research CLI. Run/event/artifact commands upload experiments; "
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
    device: bool = typer.Option(
        True,
        "--device/--endpoint-only",
        help="browser-assisted login (the default); --endpoint-only saves the endpoint without minting a token",
    ),
) -> None:
    """Log in. Bare ``probe login`` runs the browser handoff (RFC 8628) — approve
    in the dashboard, no token to see or paste.

    Pass ``--token probe_pat_...`` for the air-gap paste path, or
    ``--endpoint-only`` to just save ``--base-url`` without minting a token.
    """
    data = load_file()
    resolved_token = token or _conn.token
    base = base_url or _conn.base_url

    if device and not resolved_token:
        endpoint = resolve(base_url=base).base_url
        print(f"opening {endpoint} for browser approval…")

        def _show(prompt: DevicePrompt) -> None:
            print(f"  visit: {prompt.verification_uri_complete}")
            print(f"  code:  {prompt.user_code}")

        try:
            resolved_token = device_login(endpoint, on_prompt=_show)
        except DeviceLoginError as exc:
            print(f"device login failed: {exc}", file=sys.stderr)
            raise typer.Exit(1) from exc

    settings = resolve(
        base_url=base,
        token=resolved_token,
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


# -- mcp read credential ----------------------------------------------------
mcp_app = typer.Typer(no_args_is_help=True, help="the read-only credential the MCP surface uses")
app.add_typer(mcp_app, name="mcp")

mcp_token_app = typer.Typer(no_args_is_help=True, help="manage the read-only MCP token")
mcp_app.add_typer(mcp_token_app, name="token")

_READ_ONLY_SCOPES = {"read"}


def _normalize_token(raw: str) -> str:
    """Undo how tokens actually arrive: pasted with `Bearer `, quotes, or a newline."""
    token = raw.strip()
    for _ in range(2):  # e.g. "Bearer probe_pat_x" needs both peels
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
            token = token[1:-1].strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
    return token


def _checked_token(raw: str) -> str:
    token = _normalize_token(raw)
    if not token:
        # click's, not typer's: typer.BadParameter is not a click.ClickException, so
        # main()'s handler misses it and the user gets a traceback.
        raise click.BadParameter("token is empty")
    # No prefix check: the server takes both `ros_pat_` and `probe_pat_`, and the
    # prefix is only a discriminator — real auth is a sha256 lookup.
    if any(c.isspace() or ord(c) < 32 for c in token):
        raise click.BadParameter("token contains whitespace or control characters")
    return token


def _fingerprint(token: str) -> str:
    """Enough to compare two tokens without printing either."""
    return f"…{token[-4:]} (sha256:{hashlib.sha256(token.encode()).hexdigest()[:8]})"


def _verify(token: str, base_url: str) -> tuple[str, dict | None]:
    """Ask the API who this token is. Returns (state, identity).

    state: ``ok`` | ``rejected`` (definitive 401/403) | ``unreachable`` (blip).
    """
    try:
        with Client(base_url=base_url, token=token, fail_open=False) as client:
            return "ok", client.me()
    except (errors.AuthError, errors.ScopeError):  # 401, 403 — both definitive
        return "rejected", None
    except (errors.TransportError, errors.ServerError):
        return "unreachable", None


@mcp_token_app.command("set")
def mcp_token_set(
    token: str = typer.Option(None, "--token", help="paste a read-only token (air-gap path)"),
    allow_write: bool = typer.Option(False, "--allow-write", help="persist even if it can write"),
    verify: bool = typer.Option(True, "--verify/--no-verify", help="check the token against /v1/me"),
) -> None:
    """Store the read-only token the MCP uses. Re-run to rotate — it replaces, never appends.

    Bare `probe mcp token set` mints a read-only token in the browser, so nothing is
    pasted and no secret lands in your shell history or `ps` output.
    """
    base = resolve(base_url=_conn.base_url).base_url
    if token is not None:
        # `--token ""` is a mistake to report, not a cue to open a browser.
        secret = _checked_token(token)
    else:
        def _show(prompt: DevicePrompt) -> None:
            print(f"  visit: {prompt.verification_uri_complete}")
            print(f"  code:  {prompt.user_code}")

        print(f"opening {base} to mint a read-only token…")
        try:
            secret = device_login(
                base,
                scopes=["read"],
                token_name=f"Probe Research MCP (read-only) · {hostname()}",
                on_prompt=_show,
            )
        except DeviceLoginError as exc:
            print(f"device login failed: {exc}", file=sys.stderr)
            raise typer.Exit(1) from exc

    state, identity = _verify(secret, base) if verify else ("skipped", None)
    if state == "rejected":
        # Persisting a token the API already refuses just moves the failure somewhere
        # quieter — the MCP would load its tools and fail every call.
        print("error: the API rejected this token; nothing was saved", file=sys.stderr)
        raise typer.Exit(1)

    scopes = set((identity or {}).get("scopes") or [])
    if scopes and not scopes <= _READ_ONLY_SCOPES and not allow_write:
        print(
            f"error: this token carries {sorted(scopes)}; the MCP credential should be read-only.\n"
            "       Mint a read-only one with `probe mcp token set` (no --token), "
            "or pass --allow-write to override.",
            file=sys.stderr,
        )
        raise typer.Exit(1)

    data = load_file()
    data["mcp_token"] = secret
    data.setdefault("base_url", base)
    path = save_file(data)

    who = (identity or {}).get("email") or "unverified"
    # Never report success without saying whether it was actually checked — an
    # unverified write that reads like a verified one is how this broke before.
    note = {
        "ok": f"verified: yes ({who}, scopes={sorted(scopes) or 'unknown'})",
        "unreachable": "verified: no (API unreachable — run `probe mcp status` to recheck)",
        "skipped": "verified: no (--no-verify)",
    }[state]
    print(f"saved mcp_token {_fingerprint(secret)} to {path}\n{note}")
    if scopes and not scopes <= _READ_ONLY_SCOPES:
        print("warning: this token can write; the MCP surface is read-only by design")
    elif state != "ok":
        # The read-only guard runs on the verified path only. Say so, rather than let
        # an unchecked token look like a checked one that passed.
        print("warning: scopes unchecked — this token may be able to write")
    print("Restart any MCP client that is already running, or reconnect it, to pick this up.")


@mcp_token_app.command("unset")
def mcp_token_unset() -> None:
    """Remove the stored read-only MCP token."""
    data = load_file()
    if data.pop("mcp_token", None) is None:
        print("no mcp_token stored")
        return
    print(f"removed mcp_token from {save_file(data)}")


@mcp_app.command("headers")
def mcp_headers() -> None:
    """Emit the MCP Authorization header as JSON (for a client's headers helper)."""
    settings = resolve(base_url=_conn.base_url)
    if not settings.mcp_token:
        print(
            "no MCP token: set PROBE_MCP_TOKEN or run `probe mcp token set`",
            file=sys.stderr,
        )
        raise typer.Exit(1)
    print(json.dumps({"Authorization": f"Bearer {settings.mcp_token}"}))


@mcp_app.command("env")
def mcp_env() -> None:
    """Print the export line, for MCP clients that only read the environment.

    Prints a secret to stdout. Nothing is written to a shell profile: a tool that
    edits rc files it did not author breaks `export X=$(op read …)` and compound
    statements. Add the line yourself, or use a client that supports a headers helper.
    """
    settings = resolve(base_url=_conn.base_url)
    if not settings.mcp_token:
        print("no MCP token: run `probe mcp token set` first", file=sys.stderr)
        raise typer.Exit(1)
    print(f"export PROBE_MCP_TOKEN={shlex.quote(settings.mcp_token)}")


def _stale_literal_copies(token: str | None) -> list[str]:
    """Places that pin a *different* literal token and would outlive a rotation."""
    path = Path.home() / ".claude.json"
    try:
        servers = json.loads(path.read_text()).get("mcpServers") or {}
    except (OSError, json.JSONDecodeError, AttributeError):
        return []
    stale = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        pinned = [
            (cfg.get("env") or {}).get("PROBE_MCP_TOKEN"),
            (cfg.get("headers") or {}).get("Authorization"),
        ]
        for value in pinned:
            if isinstance(value, str) and "pat_" in value and (not token or token not in value):
                stale.append(f"~/.claude.json -> mcpServers.{name}")
                break
    return stale


@mcp_app.command("status")
def mcp_status() -> None:
    """Diagnose the MCP credential: where it comes from, whether it still works."""
    settings = resolve(base_url=_conn.base_url)
    file_token = load_file().get("mcp_token")
    env_token = os.environ.get("PROBE_MCP_TOKEN")
    token = settings.mcp_token

    print(f"config:   {config_path()}")
    print(f"endpoint: {settings.base_url}")
    if not token:
        print("token:    none — run `probe mcp token set`")
        raise typer.Exit(1)

    source = "environment (PROBE_MCP_TOKEN)" if env_token else "config file"
    print(f"token:    {_fingerprint(token)} from {source}")
    if env_token and file_token and env_token != file_token:
        # The env wins, so a freshly-rotated config token is not what the MCP sends.
        print("          ! the environment and config hold DIFFERENT tokens; the environment wins")
        print(f"          config holds {_fingerprint(file_token)} — open a new shell, or unset PROBE_MCP_TOKEN")

    state, identity = _verify(token, settings.base_url)
    if state == "ok":
        scopes = sorted((identity or {}).get("scopes") or [])
        print(f"verify:   ok — {identity.get('email')} scopes={scopes}")
        if set(scopes) - _READ_ONLY_SCOPES:
            print("          ! this token can write; the MCP surface is read-only by design")
    elif state == "rejected":
        print("verify:   REJECTED — the API refuses this token. Rotate: `probe mcp token set`")
    else:
        print("verify:   unknown — the API was unreachable")

    for place in _stale_literal_copies(token):
        print(f"stale:    ! {place} pins a different token and takes precedence over this one")

    if state == "rejected":
        raise typer.Exit(1)


# -- run lifecycle ----------------------------------------------------------
run_app = typer.Typer(no_args_is_help=True, help="run lifecycle")
app.add_typer(run_app, name="run")


@run_app.command("start")
def run_start(
    experiment: str = typer.Option(None, "--experiment", help="defaults to the git repo / script name"),
    hypothesis: str = typer.Option(
        None, "--hypothesis",
        help="required knowledge for a NEW experiment; omitted -> a marked [auto] placeholder from context",
    ),
    name: str = typer.Option(None, "--name", help="defaults to a timestamped name (+ server petname short_id)"),
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
    help="execute a local command with run/process correlation: probe exec RUN -- cmd ...",
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
        raise typer.BadParameter("probe exec requires a command after --")
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


@artifact_app.command("list")
def artifact_list(
    run: str = typer.Argument(...),
    kind: str = typer.Option(None, "--kind"),
    step_from: int = typer.Option(None, "--step-from"),
    step_to: int = typer.Option(None, "--step-to"),
) -> None:
    """List a run's artifacts (server-filtered): step-window sandbox forensics."""
    with _client() as c:
        _print_json(
            c.list_run_artifacts(run, kind=kind, step_from=step_from, step_to=step_to)
        )


# -- Harbor trial capture (Harbor-ownership Phase 1) --------------------------
trial_app = typer.Typer(no_args_is_help=True, help="capture Harbor sandbox trials into a run")
app.add_typer(trial_app, name="trial")


@trial_app.command("add")
def trial_add(
    run: str = typer.Argument(...),
    trial_dir: str = typer.Argument(..., help="a Harbor trial output directory"),
    step: int = typer.Option(None, "--step", help="training step / Miles rollout_id — the join key"),
    env_type: str = typer.Option(None, "--env-type", help="opaque environment label (e.g. skypilot-fork)"),
    expand: bool = typer.Option(True, "--expand/--no-expand", help="expand a recognized trajectory format into spans"),
    max_spans: int = typer.Option(None, "--max-spans", help="eager expansion window (0 = unlimited)"),
) -> None:
    """Capture one Harbor trial: rollout span + reward metric + labeled file
    uploads + a kind=harbor_trial manifest, all keyed by --step."""
    from ..connectors.harbor import capture_trial

    with _client() as c:
        result = capture_trial(
            _run_handle(c, run),
            trial_dir,
            step_index=step,
            environment={"type": env_type} if env_type else None,
            source_mode="cli",
            expand=expand,
            max_trajectory_spans=max_spans,
        )
    manifest = result.get("manifest") or {}
    _print_json(
        {
            "trial": result["trial"],
            "span_id": result["span_id"],
            "reward": result["reward"],
            "manifest_artifact_id": manifest.get("id") if isinstance(manifest, dict) else None,
            "files": len(result["files"]),
            "uploaded": sum(1 for f in result["files"] if f.get("uploaded")),
            "trajectory": result.get("trajectory"),
        }
    )


@trial_app.command("expand")
def trial_expand(
    run: str = typer.Argument(...),
    manifest_id: str = typer.Argument(..., help="a kind=harbor_trial manifest artifact id"),
    max_spans: int = typer.Option(0, "--max-spans", help="eager expansion window (default 0 = full)"),
) -> None:
    """Retroactively expand a captured trial's stored trajectory into spans —
    e.g. after a parser for its format shipped. Idempotent (deterministic span
    ids), so re-running only upserts."""
    from ..connectors.atif import expand_trajectory

    with _client() as c:
        manifests = {
            a["id"]: a for a in c.list_run_artifacts(run, kind="harbor_trial")
        }
        manifest = manifests.get(manifest_id)
        if manifest is None:
            typer.echo(f"no kind=harbor_trial artifact {manifest_id} on run {run}", err=True)
            raise typer.Exit(1)
        meta = manifest.get("meta") or {}
        traj_entry = next(
            (f for f in meta.get("files") or [] if f.get("role") == "trajectory" and f.get("artifact_id")),
            None,
        )
        if traj_entry is None:
            typer.echo("manifest has no uploaded trajectory file", err=True)
            raise typer.Exit(1)
        presigned = c.transport.post(
            f"/v1/artifacts/{traj_entry['artifact_id']}/download", None
        )
        doc = json.loads(c.transport.get_url(presigned["download_url"]))
        report = expand_trajectory(
            _run_handle(c, run),
            doc,
            root_span_id=str(manifest["span_id"]),
            trial=(meta.get("trial") or {}).get("name") or manifest.get("name"),
            step_index=manifest.get("step_index"),
            max_spans=max_spans,
        )
    _print_json(report)


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
# kind="note" artifact. `probe events` reads the backend lifecycle log.)
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


# -- experiment maintenance ---------------------------------------------------
experiment_app = typer.Typer(no_args_is_help=True, help="experiment maintenance")
app.add_typer(experiment_app, name="experiment")


@experiment_app.command("set")
def experiment_set(
    experiment_id: str = typer.Argument(...),
    hypothesis: str = typer.Option(None, "--hypothesis", help="replace the hypothesis (e.g. an [auto] placeholder)"),
    name: str = typer.Option(None, "--name"),
    description: str = typer.Option(None, "--description"),
) -> None:
    """Update experiment fields — the follow-up to an [auto]-generated hypothesis."""
    if hypothesis is None and name is None and description is None:
        raise typer.BadParameter("pass at least one of --hypothesis/--name/--description")
    with _client() as c:
        result = c.update_experiment(
            experiment_id, hypothesis=hypothesis, name=name, description=description
        )
    _print_json(result)


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
        result = app(args=argv, prog_name="probe", standalone_mode=False)
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
