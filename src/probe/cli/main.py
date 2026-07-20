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

import typer
from pydantic import ValidationError

from .. import __version__, errors
from ..models import Scope
from ..sdk.client import Anchor, Client
from ..sdk.config import (
    DEFAULT_BASE_URL,
    clear_context,
    config_path,
    current_context_name,
    delete_context,
    load_context,
    load_file,
    resolve,
    save_context,
    use_context,
)
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
# `Scope` is not redefined here: it is imported from the generated contract models,
# so `make regen` picks up a new backend scope for free instead of drifting.
class Relation(str, Enum):
    fork = "fork"
    resume = "resume"
    retry = "retry"
    branch = "branch"


# The `include` query param is a closed vocabulary in the contract (a const, not a free
# string), so it lives here rather than as a literal at each call site.
_INCLUDE_ARCHIVED = "archived"


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


def _show_device_prompt(prompt: DevicePrompt) -> None:
    """Print the browser URL + user code for a device-flow approval. One definition,
    reused by every command that mints via the device flow (login, token, mcp)."""
    print(f"  visit: {prompt.verification_uri_complete}")
    print(f"  code:  {prompt.user_code}")


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


# Typer vendors its own click (`typer._click`, since 0.13). The standalone `click`
# package is a DIFFERENT module object, so `except click.ClickException` matched
# nothing typer raises and every usage error escaped main() as a traceback instead of
# an exit code — silently, on an unpinned typer bump.
#
# `typer.Exit`/`typer.Abort` are public re-exports of whichever click typer uses.
# ClickException — the base of every usage error (BadParameter, NoSuchOption,
# UsageError, ...) — is not re-exported, but it is reachable from BadParameter's MRO
# under either layout, so resolving it here follows typer rather than pinning to one.
ClickException = next(
    (base for base in typer.BadParameter.__mro__ if base.__name__ == "ClickException"),
    # Never expected; the fallback keeps `import probe.cli` working (a StopIteration at
    # import time would make the whole CLI unusable) and degrades to catching nothing.
    type("_NoClickException", (Exception,), {}),
)


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
    context: str = typer.Option(
        None, "--context", help="name the context to create or overwrite (default: the active one)"
    ),
) -> None:
    """Log in. Bare ``probe login`` runs the browser handoff (RFC 8628) — approve
    in the dashboard, no token to see or paste.

    Pass ``--token probe_pat_...`` for the air-gap paste path, or
    ``--endpoint-only`` to just save ``--base-url`` without minting a token.

    ``--context staging`` logs in under a named context instead of the active one,
    so several endpoints or tenants can coexist on one machine.
    """
    resolved_token = token or _conn.token
    base = base_url or _conn.base_url

    if device and not resolved_token:
        endpoint = resolve(base_url=base).base_url
        print(f"opening {endpoint} for browser approval…")

        try:
            resolved_token = device_login(endpoint, on_prompt=_show_device_prompt)
        except DeviceLoginError as exc:
            print(f"device login failed: {exc}", file=sys.stderr)
            raise typer.Exit(1) from exc

    settings = resolve(
        base_url=base,
        token=resolved_token,
        ingest_token=ingest_token or _conn.ingest_token,
        hmac_secret=hmac_secret or _conn.hmac_secret,
        context=context,
    )
    # None means "leave whatever is already there" in save_context, so an --endpoint-only
    # login never clears a token the user still has.
    updates = {
        "base_url": settings.base_url,
        "token": settings.token or None,
        "ingest_token": settings.ingest_token or None,
        "hmac_secret": settings.hmac_secret or None,
    }
    if settings.token:
        with Client(settings=settings) as c:
            who = c.me()
        print(f"logged in to {settings.base_url} as {who.get('email', who)}")
    else:
        print(f"saved endpoint {settings.base_url} (no user token set)")
    if context:
        use_context(context)
    path = save_context(updates, name=context)
    print(f"config: {path} (context: {context or current_context_name()})")


@app.command()
def logout() -> None:
    """Revoke the calling token and clear local config."""
    try:
        with _client() as c:
            c.logout()
        print("token revoked")
    except errors.RosError as exc:
        print(f"revoke skipped ({exc})", file=sys.stderr)
    # The ACTIVE context only. Deleting the whole file would sign the user out of every
    # other endpoint they have configured, which is not what "logout" means.
    name = current_context_name()
    clear_context(name)
    print(f"local config cleared (context: {name})")


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
        # These used to raise the standalone click's BadParameter to dodge the bug
        # main() now fixes at the root (see the ClickException note above): typer's own
        # BadParameter is caught correctly, so the workaround is gone.
        raise typer.BadParameter("token is empty")
    # No prefix check: the server takes both `ros_pat_` and `probe_pat_`, and the
    # prefix is only a discriminator — real auth is a sha256 lookup.
    if any(c.isspace() or ord(c) < 32 for c in token):
        raise typer.BadParameter("token contains whitespace or control characters")
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
        print(f"opening {base} to mint a read-only token…")
        try:
            secret = device_login(
                base,
                scopes=["read"],
                token_name=f"Probe Research MCP (read-only) · {hostname()}",
                on_prompt=_show_device_prompt,
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

    updates = {"mcp_token": secret}
    if not load_context().get("base_url"):
        updates["base_url"] = base
    path = save_context(updates)

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
    if not load_context().get("mcp_token"):
        print("no mcp_token stored")
        return
    print(f"removed mcp_token from {save_context({'mcp_token': None})}")


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
    file_token = load_context().get("mcp_token")
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

# -- context ----------------------------------------------------------------
context_app = typer.Typer(
    no_args_is_help=True, help="named local contexts: endpoint + credentials + anchors"
)
app.add_typer(context_app, name="context")


def _redact(value: str | None) -> str:
    """Enough of a token to recognize, never enough to use."""
    if not value:
        return "-"
    return f"{value[:12]}…" if len(value) > 12 else "set"


def _context_row(name: str, ctx: dict, *, active: bool) -> dict:
    anchor = ctx.get("workspace") if isinstance(ctx.get("workspace"), dict) else {}
    return {
        "name": name,
        "active": active,
        "base_url": ctx.get("base_url") or DEFAULT_BASE_URL,
        "token": _redact(ctx.get("token")),
        "mcp_token": _redact(ctx.get("mcp_token")),
        "workspace": anchor.get("id"),
        "project": anchor.get("project"),
    }


@context_app.command("list")
def context_list() -> None:
    """List local contexts. Credentials are shown redacted."""
    data = load_file()
    contexts = data.get("contexts") or {}
    if not contexts:
        print("no contexts yet — run `probe login`")
        return
    active = current_context_name(data)
    _print_json(
        [_context_row(n, c or {}, active=n == active) for n, c in sorted(contexts.items())]
    )


@context_app.command("show")
def context_show(
    name: str = typer.Argument(None, help="defaults to the active context"),
) -> None:
    """Show one context as it will actually resolve, env overrides included."""
    target = name or current_context_name()
    ctx = load_context(target)
    if not ctx and target not in (load_file().get("contexts") or {}):
        print(f"no such context: {target}", file=sys.stderr)
        raise typer.Exit(1)
    row = _context_row(target, ctx, active=target == current_context_name())
    # Show the resolved view too: an env var silently outranking the file is exactly
    # the confusion this command exists to end.
    settings = resolve(context=target)
    row["resolved"] = {
        "base_url": settings.base_url,
        "workspace": settings.workspace,
        "project": settings.project,
    }
    _print_json(row)


@context_app.command("use")
def context_use(name: str = typer.Argument(..., help="context to make active")) -> None:
    """Switch the active context, creating it empty if it is new."""
    path = use_context(name)
    print(f"active context: {name} ({path})")


@context_app.command("delete")
def context_delete(name: str = typer.Argument(..., help="context to remove")) -> None:
    """Delete a context and its stored credentials."""
    if name not in (load_file().get("contexts") or {}):
        print(f"no such context: {name}", file=sys.stderr)
        raise typer.Exit(1)
    delete_context(name)
    print(f"deleted context {name} (active: {current_context_name()})")


# -- workspaces -------------------------------------------------------------
workspace_app = typer.Typer(
    no_args_is_help=True, help="workspaces — the folders that own projects"
)
app.add_typer(workspace_app, name="workspace")


def _workspace_row(ws: dict, *, me: str | None) -> dict:
    """Flatten a workspace for display.

    A workspace is one person's folder now, so "whose is it" is the useful column —
    not the retired shared/personal split. ``owner_user_id`` is nullable: a legacy
    null-owner ``shared`` row survives on any install where the retirement script has
    not run, and a client that assumes an owner would crash on exactly those rows.
    """
    owner = ws.get("owner_user_id")
    if owner is None:
        whose = "unowned (legacy)"
    elif me is not None and owner == me:
        whose = "mine"
    else:
        whose = owner
    return {
        "id": ws.get("id"),
        "name": ws.get("name"),
        "slug": ws.get("slug"),
        "kind": ws.get("kind"),
        "whose": whose,
        "projects": ws.get("project_count", 0),
    }


@workspace_app.command("list")
def workspace_list(
    raw: bool = typer.Option(False, "--raw", help="full API objects instead of the summary"),
) -> None:
    """List workspaces. Yours sorts first (server order, preserved).

    Not paginated: there is one workspace per team member, so the list is bounded.
    """
    with _client() as c:
        rows = c.list_workspaces()
        if raw:
            _print_json(rows)
            return
        # Best-effort: labelling "mine" is a nicety, not worth failing the list over.
        try:
            me = (c.me() or {}).get("user_id")
        except errors.RosError:
            me = None
    if not rows:
        # Provisioning is best-effort at onboarding and can silently fail; the next
        # write provisions one. An empty list is a state, not an error.
        print("no workspaces yet — one is provisioned on your first write")
        return
    _print_json([_workspace_row(w, me=me) for w in rows])


@workspace_app.command("get")
def workspace_get(workspace_id: str = typer.Argument(..., help="workspace id")) -> None:
    """Show one workspace."""
    with _client() as c:
        _print_json(c.get_workspace(workspace_id))


@workspace_app.command("rename")
def workspace_rename(
    workspace_id: str = typer.Argument(..., help="workspace id"),
    name: str = typer.Option(..., "--name", help="new display name"),
) -> None:
    """Rename a workspace. Name is the only editable field — slug and ownership
    are server-managed identity."""
    with _client() as c:
        _print_json(c.rename_workspace(workspace_id, name))


@workspace_app.command("use")
def workspace_use(
    workspace_id: str = typer.Argument(..., help="workspace id to make active"),
) -> None:
    """Set the active workspace for this context.

    Clears the active project: a project belongs to exactly one workspace, so keeping
    the old one selected would leave the context pointing at a project that is not in
    the workspace you just switched to.
    """
    with _client() as c:
        ws = c.get_workspace(workspace_id)
    save_context({"workspace": {"id": str(ws["id"]), "project": None}})
    print(f"active workspace: {ws.get('name')} ({ws['id']}) — project cleared")


# -- projects ---------------------------------------------------------------
project_app = typer.Typer(no_args_is_help=True, help="projects — the top of the data model")
app.add_typer(project_app, name="project")


def _resolve_workspace(explicit: str | None) -> str | None:
    """Explicit flag -> PROBE_WORKSPACE -> context. Never a hidden requirement."""
    return resolve(workspace=explicit).workspace


@project_app.command("create")
def project_create(
    slug: str = typer.Argument(..., help="url-safe identifier, unique per tenant"),
    name: str = typer.Option(None, "--name", help="display name (defaults to the slug)"),
    description: str = typer.Option(None, "--description"),
    workspace: str = typer.Option(
        None, "--workspace", help="workspace id; defaults to the active one"
    ),
) -> None:
    """Create a project.

    This is what the CLI was missing: creating a project used to require starting a run,
    which forced an experiment and an invented hypothesis into existence alongside it.
    """
    with _client() as c:
        _print_json(
            c.create_project(
                slug,
                name,
                workspace_id=_resolve_workspace(workspace),
                description=description,
            )
        )


@project_app.command("list")
def project_list(
    workspace: str = typer.Option(
        None, "--workspace", help="workspace id; defaults to the active one"
    ),
    all_workspaces: bool = typer.Option(
        False, "--all", help="every workspace you can see (ignores --workspace and context)"
    ),
    include_archived: bool = typer.Option(False, "--include-archived"),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    cursor: str = typer.Option(None, "--cursor", help="keyset cursor from a previous page"),
) -> None:
    """List projects in a workspace, or across all of them with --all."""
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    if include_archived:
        params["include"] = _INCLUDE_ARCHIVED
    # Omitting workspace_id IS "all workspaces" — the server has no all-sentinel, so
    # --all means "send no filter" rather than some magic value.
    workspace_id = None if all_workspaces else _resolve_workspace(workspace)
    with _client() as c:
        page = c.list_projects(workspace_id=workspace_id, **params)
    _print_json({"items": page.items, "next_cursor": page.next_cursor})


@project_app.command("get")
def project_get(project_id: str = typer.Argument(..., help="project id or slug")) -> None:
    """Show one project."""
    with _client() as c:
        _print_json(c.get_project(project_id))


@project_app.command("use")
def project_use(
    project_id: str = typer.Argument(..., help="project id or slug to make active"),
) -> None:
    """Set the active project for this context, so `run start` and friends default to it."""
    with _client() as c:
        proj = c.get_project(project_id)
    # Pin the project under the workspace that actually owns it, not the ambient one:
    # selecting a project from another workspace should move the anchor, not create a
    # mismatched pair. workspace_id is nullable on legacy rows — fall back to ambient.
    owner = proj.get("workspace_id") or _resolve_workspace(None)
    save_context({"workspace": {"id": str(owner) if owner else None, "project": str(proj["id"])}})
    print(f"active project: {proj.get('slug')} ({proj['id']})")


@project_app.command("patch")
def project_patch(
    project_id: str = typer.Argument(..., help="project id or slug"),
    name: str = typer.Option(None, "--name"),
    description: str = typer.Option(None, "--description"),
    workspace: str = typer.Option(
        None, "--workspace", help="not here — use `probe project move`"
    ),
) -> None:
    """Update a project's display fields."""
    if workspace is not None:
        # Refused on purpose. Re-filing fans out a reindex across every descendant, so
        # it must be the thing you asked for, not a flag that rode along on an edit.
        print(
            "error: --workspace does not belong on `patch` — re-filing a project reindexes\n"
            "       all of its experiments and runs. Use `probe project move` to do that.",
            file=sys.stderr,
        )
        raise typer.Exit(1)
    with _client() as c:
        _print_json(c.update_project(project_id, name=name, description=description))


@project_app.command("move")
def project_move(
    project_id: str = typer.Argument(..., help="project id or slug"),
    workspace: str = typer.Option(..., "--workspace", help="destination workspace id"),
) -> None:
    """Re-file a project into another workspace.

    Reindexes every live descendant experiment and terminal run in the same transaction,
    because those documents denormalize the workspace. A move to the current workspace
    is a no-op and skips the fan-out.
    """
    with _client() as c:
        _print_json(c.move_project(project_id, workspace))


@project_app.command("archive")
def project_archive(project_id: str = typer.Argument(..., help="project id or slug")) -> None:
    """Hide a project without destroying it. The `default` project cannot be archived."""
    with _client() as c:
        _print_json(c.archive_project(project_id))


@project_app.command("restore")
def project_restore(project_id: str = typer.Argument(..., help="project id or slug")) -> None:
    """Un-archive a project."""
    with _client() as c:
        _print_json(c.restore_project(project_id))


# -- tokens -----------------------------------------------------------------
token_app = typer.Typer(no_args_is_help=True, help="API tokens (probe_pat_...)")
app.add_typer(token_app, name="token")


@token_app.command("list")
def token_list() -> None:
    """List my live tokens. Secrets are never shown — match on `token_prefix`."""
    with _client() as c:
        _print_json(c.list_tokens())


@token_app.command("create")
def token_create(
    name: str = typer.Option(..., "--name", help="what this token is for, e.g. 'ci-bot'"),
    scope: list[Scope] = typer.Option(
        None, "--scope",
        help="repeatable; omit to request read+write+delete (never admin). A token can "
             "never exceed the scopes your role confers.",
    ),
    no_browser: bool = typer.Option(False, "--no-browser", help="print the URL instead of opening it"),
) -> None:
    """Mint a token via the browser device flow — approve in the dashboard.

    Minting deliberately requires a human in a browser (a leaked token must not be
    able to mint more tokens), so this prints a URL + code and waits for approval.
    The secret is printed ONCE and never stored; copy it now.
    """
    with _client() as c:
        print(f"opening {c.settings.base_url} for browser approval…")
        try:
            created = c.create_token(
                name,
                scopes=[s.value for s in scope] if scope else None,
                open_browser=not no_browser,
                on_prompt=_show_device_prompt,
            )
        except DeviceLoginError as exc:
            print(f"token creation failed: {exc}", file=sys.stderr)
            raise typer.Exit(1) from exc

    # The token is already minted server-side; its plaintext exists exactly once. Read
    # the secret FIRST so a missing name/id (response drift) can't KeyError before it is
    # shown and orphan an unrecoverable token. name/id are decorative — fall back.
    secret = created["token"]
    label = created.get("name", name)
    token_id = created.get("id", "unknown")
    # Shown once, and only here: not via _print_json (which invites piping it to a
    # file) and never written to config.
    print(f"\ntoken {label!r} created (id: {token_id})")
    print(f"\n  {secret}\n")
    print("^ copy it now — this is the only time it is shown.", file=sys.stderr)


@token_app.command("revoke")
def token_revoke(token_id: str = typer.Argument(..., help="token id (from `probe token list`)")) -> None:
    """Revoke one of my tokens. Revoking a teammate's needs the dashboard."""
    with _client() as c:
        c.revoke_token(token_id)
    print(f"revoked {token_id}")


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
    group: str = typer.Option(None, "--group", help="run group id (see `probe group create`)"),
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
            group_id=group,
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


@run_app.command("delete")
def run_delete(run: str = typer.Argument(...)) -> None:
    """Soft-delete a run (reversible with `probe run restore`)."""
    with _client() as c:
        c.delete_run(run)
    print(f"{run} deleted (restore with `probe run restore {run}`)")


@run_app.command("restore")
def run_restore(run: str = typer.Argument(...)) -> None:
    """Un-delete a soft-deleted run."""
    with _client() as c:
        c.restore_run(run)
    print(f"{run} restored")


@run_app.command("gc")
def run_gc(
    run_id: list[str] = typer.Option(None, "--id", metavar="UUID", help="repeatable; purge these runs (ids, not petnames)"),
    older_than: str = typer.Option(
        None, "--older-than", metavar="TIMESTAMP",
        help="purge runs deleted before this; must carry a timezone, e.g. 2026-07-01T00:00:00Z",
    ),
    yes: bool = typer.Option(False, "--yes", help="skip the confirmation prompt"),
) -> None:
    """PERMANENTLY purge soft-deleted runs (owner/admin). Irreversible.

    Pass exactly one selector: --id (repeatable) or --older-than.
    """
    if bool(run_id) == bool(older_than):
        raise typer.BadParameter("pass exactly one of --id or --older-than")
    target = f"{len(run_id)} run(s)" if run_id else f"every run deleted before {older_than}"
    if not yes:
        typer.confirm(
            f"permanently purge {target}? spans/metrics/artifacts go too, and this cannot be undone",
            abort=True,
        )
    with _client() as c:
        result = c.gc_runs(run_ids=run_id or None, older_than=older_than)
    _print_json(result)


@run_app.command("series")
def run_series(run: str = typer.Argument(...)) -> None:
    """Per-series summary for a run (key/kind/dimensions + first/last/min/max)."""
    with _client() as c:
        _print_json(c.run_series(run))


@run_app.command("metrics")
def run_metrics(
    run: str = typer.Argument(...),
    key: str = typer.Option(None, "--key"),
    kind: str = typer.Option(None, "--kind"),
    limit: int = typer.Option(None, "--limit"),
) -> None:
    """Raw metric points for a run."""
    with _client() as c:
        _print_json(c.run_metrics(run, key=key, kind=kind, limit=limit))


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


@span_app.command("list")
def span_list(
    run: str = typer.Argument(...),
    span_type: str = typer.Option(None, "--type"),
    parent: str = typer.Option(None, "--parent"),
    step_from: int = typer.Option(None, "--step-from"),
    step_to: int = typer.Option(None, "--step-to"),
    limit: int = typer.Option(None, "--limit"),
) -> None:
    """Read a run's spans back."""
    with _client() as c:
        _print_json(
            c.run_spans(
                run,
                span_type=span_type,
                parent_span_id=parent,
                step_from=step_from,
                step_to=step_to,
                limit=limit,
            )
        )


@span_app.command("get")
def span_get(span_id: str = typer.Argument(...)) -> None:
    """Print one span."""
    with _client() as c:
        _print_json(c.get_span(span_id))


# -- artifacts --------------------------------------------------------------
artifact_app = typer.Typer(no_args_is_help=True, help="artifacts")
app.add_typer(artifact_app, name="artifact")


def _pick_anchor(
    *,
    run: str | None,
    project: str | None,
    experiment: str | None,
    workspace: str | None,
    shared: bool,
) -> tuple[Anchor, str | None]:
    """Resolve exactly one anchor from the flags, or fail loudly.

    An artifact hangs off exactly one thing (the DB CHECKs it), so two anchors is a
    mistake worth stopping for rather than silently picking a winner.
    """
    chosen = [
        (Anchor.PROJECT, project),
        (Anchor.EXPERIMENT, experiment),
        (Anchor.WORKSPACE, workspace),
    ]
    given = [(a, v) for a, v in chosen if v is not None]
    if shared:
        given.append((Anchor.SHARED, None))
    if run is not None:
        given.append((Anchor.RUN, run))
    if len(given) > 1:
        names = ", ".join(f"--{a.value}" if a is not Anchor.RUN else "RUN" for a, _ in given)
        raise typer.BadParameter(
            f"an artifact anchors to exactly one thing; got {names}"
        )
    if not given:
        raise typer.BadParameter(
            "needs an anchor: a RUN argument, or --project/--experiment/--workspace/--shared"
        )
    return given[0]


@artifact_app.command("add")
def artifact_add(
    run: str = typer.Argument(None, help="run id — omit when using an anchor flag"),
    path: str = typer.Argument(None, help="local file to upload"),
    uri: str = typer.Option(None, "--uri", help="record a reference to an existing object"),
    name: str = typer.Option(None, "--name"),
    kind: str = typer.Option("file", "--kind", help="run anchor only"),
    step: int = typer.Option(None, "--step", help="run anchor only"),
    content_type: str = typer.Option(None, "--content-type"),
    project: str = typer.Option(None, "--project", help="anchor to a project"),
    experiment: str = typer.Option(None, "--experiment", help="anchor to an experiment"),
    workspace: str = typer.Option(
        None, "--workspace", help="anchor to a workspace (a file, not an artifact)"
    ),
    shared: bool = typer.Option(False, "--shared", help="put it in the team Shared folder"),
) -> None:
    """Record an artifact against a run, project, experiment, workspace, or Shared.

    With a path and no --uri the real upload runs (fingerprint -> presign -> PUT ->
    confirm). With --uri it records a metadata-only reference, which only the run,
    project, and experiment anchors support — a file *is* its bytes.
    """
    anchored = project or experiment or workspace or shared
    if anchored:
        # With an anchor flag there is no RUN, so the single positional is the path.
        # Shifting here (rather than guessing from the value) keeps `add ./f.bin` and
        # `add RUN ./f.bin` both unambiguous.
        if path is not None:
            raise typer.BadParameter(
                "too many arguments: with an anchor flag, pass only the file path"
            )
        path, run = run, None

    anchor, anchor_id = _pick_anchor(
        run=run, project=project, experiment=experiment, workspace=workspace, shared=shared
    )

    resolved = name
    if resolved is None and path:
        resolved = os.path.basename(path)
    if resolved is None:
        raise typer.BadParameter("artifact needs --name (or a path to derive it from)")

    if anchor is Anchor.RUN:
        with _client() as c:
            _run_handle(c, anchor_id).log_artifact(
                resolved, path=path, uri=uri, kind=kind, step_index=step,
                content_type=content_type,
            )
        print(f"artifact {resolved!r} recorded on {anchor_id}")
        return

    if step is not None or kind != "file":
        raise typer.BadParameter(
            f"--kind/--step are run-only; the {anchor.value} upload contract rejects them"
        )
    with _client() as c:
        if uri is not None:
            body = {"name": resolved, "uri": uri, "is_reference": True}
            if content_type:
                body["content_type"] = content_type
            _print_json(c.create_anchored_reference(anchor, anchor_id, body))
        else:
            if not path:
                raise typer.BadParameter("needs a file path (or --uri)")
            _print_json(
                c.upload_file(
                    anchor, anchor_id, resolved, path, content_type=content_type
                )
            )


@artifact_app.command("list")
def artifact_list(
    run: str = typer.Argument(None, help="run id — omit when using an anchor flag"),
    kind: str = typer.Option(None, "--kind", help="run anchor only"),
    step_from: int = typer.Option(None, "--step-from", help="run anchor only"),
    step_to: int = typer.Option(None, "--step-to", help="run anchor only"),
    project: str = typer.Option(None, "--project"),
    experiment: str = typer.Option(None, "--experiment"),
    workspace: str = typer.Option(None, "--workspace"),
    shared: bool = typer.Option(False, "--shared"),
) -> None:
    """List artifacts under an anchor. Run listing is server-filtered by step window."""
    anchor, anchor_id = _pick_anchor(
        run=run, project=project, experiment=experiment, workspace=workspace, shared=shared
    )
    with _client() as c:
        if anchor is Anchor.RUN:
            _print_json(
                c.list_run_artifacts(
                    anchor_id, kind=kind, step_from=step_from, step_to=step_to
                )
            )
            return
        if kind or step_from is not None or step_to is not None:
            raise typer.BadParameter(
                f"--kind/--step-from/--step-to are run-only filters; "
                f"the {anchor.value} listing does not accept them"
            )
        _print_json(c.list_anchored(anchor, anchor_id))


@artifact_app.command("delete")
def artifact_delete(artifact_id: str = typer.Argument(...)) -> None:
    """Delete an artifact."""
    with _client() as c:
        c.delete_artifact(artifact_id)
    print(f"artifact {artifact_id} deleted")


@artifact_app.command("gc-uploads")
def artifact_gc_uploads(
    older_than: str = typer.Option(
        ..., "--older-than", metavar="TIMESTAMP",
        help="sweep uploads started before this; must carry a timezone, e.g. 2026-07-01T00:00:00Z",
    ),
) -> None:
    """Sweep abandoned (never-confirmed) uploads. Confirmed artifacts are untouched."""
    with _client() as c:
        _print_json(c.gc_uploads(older_than))


# -- shared folder ----------------------------------------------------------
shared_app = typer.Typer(no_args_is_help=True, help="the team's Shared folder")
app.add_typer(shared_app, name="shared")


@shared_app.command("list")
def shared_list() -> None:
    """List the team's Shared files."""
    with _client() as c:
        _print_json(c.list_anchored(Anchor.SHARED))


@shared_app.command("add")
def shared_add(
    path: str = typer.Argument(..., help="local file to upload"),
    name: str = typer.Option(None, "--name", help="defaults to the file's basename"),
    content_type: str = typer.Option(None, "--content-type"),
) -> None:
    """Upload a file straight into the team's Shared folder."""
    resolved = name or os.path.basename(path)
    with _client() as c:
        _print_json(
            c.upload_file(Anchor.SHARED, None, resolved, path, content_type=content_type)
        )


@shared_app.command("share")
def shared_share(
    artifact_id: str = typer.Argument(..., help="a workspace file id"),
) -> None:
    """Move one of your workspace files into the team's Shared folder.

    A MOVE, not a copy: the file leaves your workspace listing. Ownership transfers
    and the search index is re-keyed in the same transaction.
    """
    with _client() as c:
        _print_json(c.share_workspace_file(artifact_id))


@shared_app.command("unshare")
def shared_unshare(
    artifact_id: str = typer.Argument(..., help="a shared file id"),
) -> None:
    """Move a Shared file back into your personal workspace."""
    with _client() as c:
        _print_json(c.unshare_file(artifact_id))


@shared_app.command("download")
def shared_download(
    artifact_id: str = typer.Argument(..., help="a shared file id"),
) -> None:
    """Print a presigned download URL for a Shared file."""
    with _client() as c:
        _print_json(c.download_shared_file(artifact_id))


@shared_app.command("delete")
def shared_delete(
    artifact_id: str = typer.Argument(..., help="a shared file id"),
) -> None:
    """Remove a file from the Shared folder (soft delete; recoverable)."""
    with _client() as c:
        c.delete_shared_file(artifact_id)
    print(f"shared file {artifact_id} deleted")


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


@experiment_app.command("archive")
def experiment_archive(experiment_id: str = typer.Argument(...)) -> None:
    """Archive an experiment (reversible; idempotent)."""
    with _client() as c:
        c.archive_experiment(experiment_id)
    print(f"{experiment_id} archived")


@experiment_app.command("restore")
def experiment_restore(experiment_id: str = typer.Argument(...)) -> None:
    """Un-archive an experiment."""
    with _client() as c:
        c.restore_experiment(experiment_id)
    print(f"{experiment_id} restored")


@experiment_app.command("edges")
def experiment_edges(experiment_id: str = typer.Argument(...)) -> None:
    """Print every lineage edge under an experiment."""
    with _client() as c:
        _print_json(c.experiment_edges(experiment_id))


# -- run groups (sweeps / ensembles) ----------------------------------------
group_app = typer.Typer(no_args_is_help=True, help="run groups: sweeps, ensembles, distributed runs")
app.add_typer(group_app, name="group")


@group_app.command("create")
def group_create(
    experiment_id: str = typer.Argument(...),
    name: str = typer.Option(..., "--name"),
    kind: str = typer.Option("group", "--kind", help="e.g. sweep, ensemble"),
    spec: str = typer.Option(None, "--spec", metavar="JSON|@file", help="e.g. a sweep search space"),
) -> None:
    """Create a run group. Pass the printed id to `probe run start --group`."""
    with _client() as c:
        result = c.create_group(experiment_id, name, kind=kind, spec=_json_value(spec))
    _print_json(result)


@group_app.command("list")
def group_list(experiment_id: str = typer.Argument(...)) -> None:
    """List an experiment's run groups."""
    with _client() as c:
        _print_json(c.list_groups(experiment_id))


@group_app.command("get")
def group_get(group_id: str = typer.Argument(...)) -> None:
    """Print one run group."""
    with _client() as c:
        _print_json(c.get_group(group_id))


@group_app.command("set")
def group_set(
    group_id: str = typer.Argument(...),
    name: str = typer.Option(None, "--name"),
    spec: str = typer.Option(None, "--spec", metavar="JSON|@file"),
) -> None:
    """Update a run group's name and/or spec."""
    if name is None and spec is None:
        raise typer.BadParameter("pass at least one of --name/--spec")
    with _client() as c:
        result = c.update_group(group_id, name=name, spec=_json_value(spec))
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
        # NB: --help/--version/explicit typer.Exit don't raise here — click catches
        # Exit internally (standalone_mode=False) and RETURNS the code, so it flows
        # through the `return result` below. The except clauses catch what actually
        # propagates: usage errors (ClickException), Abort, model ValidationError.
    except typer.Exit as exc:  # defensive: a typer.Exit that does propagate
        return int(exc.exit_code)
    except typer.Abort:
        print("aborted", file=sys.stderr)
        return 1
    except ClickException as exc:  # usage / bad-parameter errors
        exc.show()
        return exc.exit_code or 2
    except ValidationError as exc:
        # A CLI string that fails the generated model's validation is a usage error,
        # not a crash: `--older-than 2026-07-01` is valid ISO 8601 but not an aware
        # datetime, and `--id abc` is not a UUID. Report it like one (exit 2).
        for err in exc.errors():
            field = ".".join(str(p) for p in err["loc"]) or exc.title
            print(f"error: invalid {field}: {err['msg']}", file=sys.stderr)
        return 2
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
