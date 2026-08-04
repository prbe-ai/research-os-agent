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

import dataclasses
import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from uuid import UUID, uuid4

import typer
from pydantic import ValidationError

from .. import __version__, errors
from ..client_headers import client_version_headers
from ..models import Scope
from ..sdk.client import _FILE_ANCHORS, Anchor, Client
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
from ..sdk.tags import canonical_tags
from ..sdk.device import DeviceLoginError, DevicePrompt, device_login, hostname
from ..sdk.hashing import reference_fields
from ..sdk.surface import Surface


# -- global connection state (set by the root callback) ---------------------
@dataclass
class Conn:
    base_url: str | None = None
    spool_dir: str | None = None
    async_mode: bool = False


_conn = Conn()


# -- enums (choices) --------------------------------------------------------
# `Scope` is not redefined here: it is imported from the generated contract models,
# so `make regen` picks up a new backend scope for free instead of drifting.
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


class Agg(str, Enum):
    mean = "mean"
    sum = "sum"
    min = "min"
    max = "max"
    count = "count"


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


def _new_client(**kwargs: Any) -> Client:
    """Construct a CLI-owned SDK client with bounded version telemetry."""

    return Client(
        surface=Surface.CLI.value,
        client_headers=client_version_headers("cli", __version__),
        **kwargs,
    )


def _client() -> Client:
    # `Client` is a module global so the CLI package can monkeypatch it in tests.
    return _new_client(
        base_url=_conn.base_url,
        spool_dir=_conn.spool_dir,
    )


def _journal():
    from ..sdk.journal import Journal

    return Journal(_conn.spool_dir)


def _async_client() -> Client:
    """A journaling client for the async write paths (9A). Errors early when no
    deliverable credentials exist -- queueing an op nothing can ever deliver
    fails hours later in the drainer, which is the worst place to learn it."""
    from ..sdk.config import resolve

    settings = resolve(base_url=_conn.base_url)
    if not settings.token and not settings.ingest_token:
        raise typer.BadParameter(
            "--async needs deliverable credentials: run `probe login` "
            "(or set PROBE_TOKEN) so the background drainer can authenticate"
        )
    return _new_client(
        base_url=_conn.base_url,
        spool_dir=_conn.spool_dir,
        async_writes=True,
    )


def _async_run(client: Client, run_ref: str):
    """A Run handle that does NOT read the run first: async enqueue must not
    block on (or fail without) the network; the server validates the ref at
    replay (eng review D20-1)."""
    from ..sdk.run import Run

    return Run(client, {"id": run_ref})


def _kick_drainer() -> None:
    from . import outbox_worker

    try:
        outbox_worker.maybe_spawn(_conn.spool_dir)
    except Exception:  # noqa: BLE001 -- delivery is best-effort; run end is the barrier
        pass


# Commands that must never trigger an upgrade, whatever the terminal looks like.
#
# These are the run-scoped writes a training loop makes. The TTY test alone does
# not cover them: `python train.py` started from a terminal passes its TTY down to
# every child, so `probe log` inside an interactive training run IS a TTY -- the
# check says "a human is present" in precisely the case where the hazard is worst.
#
# `exec` is here for the opposite reason: it does not run INSIDE a training loop,
# it OWNS one. It holds a run-lock flock for the wrapped process's whole life
# (see the exec command), so it is both barred from triggering and protected from
# being triggered on.
#
# Group name, not full path: Typer dispatches `probe run start` through the `run`
# group, and every subcommand under these groups is run-scoped.
_UPDATE_HOT_PATH_COMMANDS = frozenset(
    {"log", "span", "artifact", "run", "exec", "outbox"}
)


#: Root options that consume the NEXT argv token as their value. Skipping the
#: flag but not its value is how `probe --base-url URL log` resolves to "URL" --
#: which is not in the denylist, so `log` would sail through the gate it exists
#: to stop. Flags (--async, --version) take no value and must NOT be listed.
_ROOT_OPTIONS_WITH_VALUES = frozenset({"--base-url", "--spool-dir"})


def _invoked_command() -> str | None:
    """The top-level command name from argv, or None.

    Read from argv rather than a Typer context because the root callback runs
    before dispatch -- there is no command context yet.

    Conservative by construction: an unrecognized `--opt value` form returns the
    value, which is not a known command, so the denylist does not match and the
    gate falls through to the RUN LOCK. Getting this wrong costs a redundant
    lock scan, never an upgrade into a live run.
    """
    import sys as _sys

    skip_next = False
    for token in _sys.argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if token.startswith("--") and "=" in token:
            continue  # --base-url=URL carries its own value
        if token in _ROOT_OPTIONS_WITH_VALUES:
            skip_next = True
            continue
        if token.startswith("-"):
            continue
        return token
    return None


def _version_notice() -> None:
    """The every-command version check: nudge from cache, refresh in background.

    Same contract as _outbox_notice below -- cheap reads, no network, never
    raises -- because it runs before every command including `probe log` inside
    training loops.

    THE GATE ORDER IS LOAD-BEARING, cheapest and most-likely-to-exit first:

        enabled?          one small read; defaults False, so an install that
                          never opted in touches exactly one file and stops
        cache fresh?      one ~150-byte read; this is where ~99.9% of calls
                          exit, which is what makes the trigger free in a loop
        (spawn refresher) detached; this process NEVER fetches inline. A 3s
                          timeout on the hot path would put a periodic stall
                          inside training loops, attributed to anything but us
        update available? pure comparison, no I/O
        TTY?              cheap, and wrong on its own -- see the denylist above
        denylisted?       frozenset membership
        run in flight?    LAST, because it is the only O(N) check: it scans a
                          directory and probes locks. Never reached unless an
                          upgrade would otherwise be applied right now

    A skip at any of the last three is RECORDED, so `probe doctor` can tell a box
    that is correctly deferring from an auto-updater that has silently died.
    """
    try:
        from probe import version_policy

        if not version_policy.autoupdate_enabled():
            return

        manifest, fetched_at, ok = version_policy.read_cache()
        if not version_policy.cache_is_fresh(fetched_at, ok):
            _spawn_version_refresh()

        if not isinstance(manifest, dict):
            return  # cold cache: nothing to compare against until the refresh lands

        from . import updater

        latest = updater.cli_update_available(manifest, __version__)
        if not latest:
            return

        from . import autoupdate

        typer.echo(
            f"probe {__version__} -> {latest} available (`probe wizard --action update`)",
            err=True,
        )

        if not sys.stdout.isatty():
            autoupdate.record_skip(autoupdate.SKIP_NOT_A_TTY, available=latest)
            return
        if _invoked_command() in _UPDATE_HOT_PATH_COMMANDS:
            autoupdate.record_skip(autoupdate.SKIP_HOT_PATH_COMMAND, available=latest)
            return

        from . import run_lock

        if run_lock.any_live():
            autoupdate.record_skip(autoupdate.SKIP_RUN_IN_FLIGHT, available=latest)
            return

        _spawn_autoupdate()
    except Exception:  # noqa: BLE001 -- a version check must never break a command
        pass


def _spawn_detached(argv: list[str], env: dict[str, str]) -> None:
    """Launch `argv` fully detached, leaving NO child of ours behind.

    `start_new_session=True` detaches the session but does NOT reparent: the
    process stays our child, so if we never wait for it, its exit status sits in
    the table as a zombie until we exit. That is invisible for `probe ls` and
    real for `probe exec`, which can own a training process for hours.

    So the target daemonizes -- it forks and its first stage exits immediately
    (see version_refresh.main) -- and we reap that first stage right here. The
    real work is carried by the grandchild, which init adopts.
    """
    child = subprocess.Popen(  # noqa: S603 -- our own interpreter, our own module
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=env,
    )
    try:
        # The first stage forks and exits at once, so this returns immediately.
        # The timeout is a backstop, not a wait: if the fork ever fails and the
        # process runs its work inline, we abandon it rather than block a command.
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _spawn_version_refresh() -> None:
    """Refresh the manifest in a DETACHED child; never fetch inline.

    The invoking process must not make a network call. `PROBE_VERSION_TIMEOUT` is
    3s, so an inline fetch means one `probe log` in every TTL blocking for up to
    three seconds inside somebody's training loop -- a periodic step-time spike
    that correlates with nothing in their code. This repo has been here before:
    every run-anchored CLI write used to make a network call before the write,
    and it had to be removed.

    The cost is that this invocation compares against the OLD manifest and the
    nudge lands on the NEXT one. That is how update-notifier behaves, for the
    same reason.
    """
    from probe import version_policy

    if not version_policy.claim_refresh():
        return  # somebody is already fetching; a burst of 8 runs makes 1 request
    env = dict(os.environ)
    env[version_policy.REFRESH_OWNER_ENV] = str(os.getpid())
    try:
        _spawn_detached([sys.executable, "-m", "probe.cli.version_refresh"], env)
    except Exception:  # noqa: BLE001
        version_policy.release_refresh()


def _spawn_autoupdate() -> None:
    """Apply the upgrade in a DETACHED child that waits for US to exit first.

    The wait is the whole point. This runs in the root callback, BEFORE Typer
    dispatches, and the CLI imports lazily throughout -- so without it the child
    would be replacing the installed tree while this very command is still
    importing from it.
    """
    from . import autoupdate

    env = dict(os.environ)
    env[autoupdate.WAIT_FOR_PID_ENV] = str(os.getpid())
    try:
        _spawn_detached(
            [sys.executable, "-m", "probe.cli.version_refresh", "--apply"], env
        )
    except Exception:  # noqa: BLE001 -- fail-open: never block the command
        pass


def _outbox_notice() -> None:
    """The every-command outbox banner (2B) + drainer re-kick (3A).

    One stat/read of status.json, no locks, never raises: this runs before
    every command, including `probe log` inside training loops.
    """
    try:
        from ..sdk.journal import Journal

        status = Journal.read_status(_conn.spool_dir)
        if not status:
            return
        failed = status.get("failed") or 0
        pending = status.get("pending") or 0
        blocked = status.get("auth_blocked_since")
        parts: list[str] = []
        if blocked:
            parts.append(f"auth-blocked since {blocked} — run `probe login`")
        if failed:
            parts.append(f"{failed} dead-lettered")
        if parts or (pending and status.get("paused")):
            if pending:
                parts.append(f"{pending} pending")
            if status.get("paused"):
                parts.append("paused")
            typer.echo(
                f"outbox: {'; '.join(parts)} — see `probe outbox status`", err=True
            )
        if pending and not status.get("paused") and not blocked:
            _kick_drainer()
    except Exception:  # noqa: BLE001 -- a broken banner must never break a command
        pass


def _run_handle(client: Client, run_id: str):
    from ..sdk.run import Run

    # Every run-scoped CLI write funnels through here, which makes it the one
    # place a detached run's auto-update lease can be renewed without anyone
    # having to remember to. A bare `probe run start` ... `probe run end`
    # bracket has no process of ours alive in between, so its claim on the box
    # is a lease -- and this is the traffic that keeps it alive.
    #
    # No-ops for a run holding an flock (SDK, `probe exec`) and skips the write
    # until the lease is half spent, so `probe log` in a training loop does not
    # pay for it. See run_lock.renew_lease_if_stale.
    try:
        from . import run_lock

        run_lock.renew_lease_if_stale(run_id)
    except Exception:  # noqa: BLE001 -- a lock refresh must never break a write
        pass
    return Run(client, client.get_run(run_id))


def _apply_tag_ops(
    current: list[str],
    add: list[str],
    remove: list[str],
    replace: list[str] | None,
) -> list[str]:
    """Compute a ``tag`` verb's replacement list (read-modify-write over the
    server's whole-list-replace PATCH, CONTRACT.md "tags"). ``--set`` wins
    outright; otherwise positional adds append (canonical, deduped) and
    ``--remove`` drops. The same tag in add AND remove is a caller bug — error,
    never a silent tie-break."""
    if replace is not None:
        if add or remove:
            raise typer.BadParameter("--set replaces outright; don't combine it with adds/--remove")
        return canonical_tags(replace)
    add_c = canonical_tags(add)
    remove_c = set(canonical_tags(remove))
    both = [t for t in add_c if t in remove_c]
    if both:
        raise typer.BadParameter(f"tag(s) both added and removed: {', '.join(both)}")
    out = [t for t in canonical_tags(current) if t not in remove_c]
    out.extend(t for t in add_c if t not in out)
    return out


def _tag_verb_flow(entity_id, current, add, remove, replace, write) -> dict:
    """Shared flow for the three ``tag`` verbs: bare invocation lists, anything
    else is read-modify-write. The changed-check compares against the RAW
    stored list (not its canonical form) so re-tagging a pre-0066 row with its
    own canonical name still writes once and heals the stored form; the write
    callbacks verify the server actually persisted tags (0066 guard)."""
    current = list(current or [])
    if not add and not remove and replace is None:
        return {"id": entity_id, "tags": current}
    wanted = _apply_tag_ops(current, add or [], remove or [], replace)
    if wanted == current:
        return {"id": entity_id, "tags": current}
    result = write(wanted) or {}
    return {"id": result.get("id", entity_id), "tags": result.get("tags", wanted)}


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
    spool_dir: str = typer.Option(
        None, "--spool-dir", help="outbox journal directory (or PROBE_OUTBOX_DIR)"
    ),
    async_mode: bool = typer.Option(
        False,
        "--async",
        help="queue data writes to the local outbox and return immediately "
        "(or PROBE_ASYNC=1). Read by `log`, `span add`, `note add`, "
        "`artifact add`, `run end`; other commands ignore it.",
    ),
    version: bool = typer.Option(
        False, "--version", callback=_version_cb, is_eager=True, help="show version"
    ),
) -> None:
    # Credentials come from named contexts (`probe login`) or the PROBE_TOKEN /
    # PROBE_INGEST_TOKEN / PROBE_BASE_URL env vars. The old --token/--ingest-token/
    # --hmac-secret overrides were removed (v0.23.0): a secret in argv leaks into
    # shell history and `ps`, and a detached outbox drain could never resolve it.
    _conn.base_url = base_url
    _conn.spool_dir = spool_dir
    _conn.async_mode = async_mode or os.environ.get("PROBE_ASYNC", "").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    _outbox_notice()
    # The CLI's own auto-update trigger. Before this existed, auto-update fired
    # from exactly one place -- the Claude Code plugin's SessionStart hook -- so a
    # user who never started a session never updated, and a headless box never
    # updated at all. Both of these are read-only-and-spawn on the fast path; see
    # _version_notice for why its gate order is what it is.
    _version_notice()


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
    resolved_token = token
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
        ingest_token=ingest_token,
        hmac_secret=hmac_secret,
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
        with _new_client(settings=settings) as c:
            who = c.me()
        print(f"logged in to {settings.base_url} as {who.get('email', who)}")
    else:
        print(f"saved endpoint {settings.base_url} (no user token set)")
    if context:
        use_context(context)
    path = save_context(updates, name=context)
    print(f"config: {path} (context: {context or current_context_name()})")
    if settings.token:
        # Fresh credentials un-block the outbox: forget any recorded 401/403
        # and wake the drainer so queued writes deliver without further steps.
        try:
            journal = _journal()
            journal.clear_auth_block()
            _kick_drainer()
        except Exception:  # noqa: BLE001 -- login must not fail on outbox hygiene
            pass


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


@app.command()
def backfill(
    folder: str = typer.Argument(None, help="folder to import; omit to pick one interactively"),
    agent: str = typer.Option(
        None, "--agent", help="claude | codex — omit to use whichever is installed, or be asked"
    ),
    project: str = typer.Option(
        None, "--project", help="import into this project; omit to use one named for the folder"
    ),
) -> None:
    """Import work you have already done: point an agent at a folder.

    It uploads what it finds, describes each artifact, and reports how much of
    the folder it accounted for. Large files (checkpoints, datasets) are
    recorded as references, not copied.

    This is the command the dashboard hands you: `npx probe-research backfill`
    forwards straight here. The same thing is in `probe wizard` under
    `Import existing work`.
    """
    from pathlib import Path

    from probe.cli import backfill as backfill_impl
    from probe.cli import setup as wizard
    from probe.cli import tui

    # BEFORE anything else, and for a stronger reason than the wizard has:
    # `npx probe-research backfill` reaches us through an EPHEMERAL `uv tool
    # run` / `pipx run`, and the agent we are about to launch does its work by
    # shelling out to `probe artifact add`. With no persistent binary on PATH
    # every one of those calls fails, so the agent reads the whole folder and
    # lands nothing.
    from probe.cli.bootstrap import ensure_persistent_install

    try:
        chosen = backfill_impl.Agent(agent) if agent else None
    except ValueError:
        raise typer.BadParameter(
            f"unknown agent {agent!r}; expected one of: "
            f"{', '.join(a.value for a in backfill_impl.Agent)}"
        ) from None

    boot = ensure_persistent_install()
    if boot.message:
        print(boot.message)

    lines = backfill_impl.run(
        client_factory=_client,
        folder=Path(folder) if folder else None,
        project=project,
        interactive=wizard.interactive(),
        agent=chosen,
    )
    if lines:
        tui.page(lines) if wizard.interactive() else print("\n".join(lines))


# `probe update` is HIDDEN, not deleted. The plugin's SessionStart hook spawns
# it, and plugins update on the USER's schedule -- deleting it would silently
# break auto-update on every machine whose plugin has not been refreshed yet.
# New plugin versions call `probe wizard --action update` instead.
@app.command(name="update", hidden=True)
def update_compat(
    check: bool = typer.Option(False, "--check"),
    yes: bool = typer.Option(False, "--yes", "-y"),
    plugin: bool = typer.Option(True, "--plugin/--no-plugin"),
    channel: str = typer.Option(None, "--channel", hidden=True),  # noqa: ARG001 - ignored
) -> None:
    """Deprecated: use `probe wizard` and pick Update."""
    from probe.cli import updater
    from probe.cli.upgrading import perform_update

    base = resolve(base_url=_conn.base_url).base_url
    if check:
        try:
            manifest = updater.fetch_latest(base)
        except Exception as exc:  # noqa: BLE001
            print(f"update check failed: {exc}", file=sys.stderr)
            raise typer.Exit(updater.CHECK_ERROR) from exc
        latest = updater.cli_update_available(manifest, __version__)
        if latest:
            print(f"update available: CLI {__version__} → {latest}")
            raise typer.Exit(updater.CHECK_BEHIND)
        print(f"up to date: CLI {__version__}")
        raise typer.Exit(updater.CHECK_CURRENT)

    outcome = perform_update(base_url=base, include_plugin=plugin)
    for line in outcome.lines:
        print(line)
    if outcome.restart_needed:
        print("\nRestart Claude Code to apply the plugin update.")
    raise typer.Exit(0 if outcome.ok else 1)


@app.command()
def doctor() -> None:
    """Read-only diagnostic: what is installed, on, and whether auto-update works.

    Prints the LAST UPDATE ATTEMPT, which is the only way to notice a detached
    auto-updater that has been silently failing.
    """
    # Imported inside the body, not at module scope: cli/__init__ eagerly loads
    # this module, and `probe log` runs inside training loops.
    from probe.cli import doctor as doctor_impl

    print(doctor_impl.render(doctor_impl.collect()))


@app.command(name="wizard")
def wizard(
    tracking: Optional[bool] = typer.Option(  # noqa: UP007 - typer needs Optional
        None,
        "--tracking/--no-tracking",
        help="experiment tracking skills + read-only MCP search",
    ),
    capture: Optional[bool] = typer.Option(  # noqa: UP007
        None,
        "--capture/--no-capture",
        help="stream this device's Claude Code sessions to the knowledgebase",
    ),
    auto_update: Optional[bool] = typer.Option(  # noqa: UP007
        None, "--auto-update/--no-auto-update", help="keep the CLI and plugins current"
    ),
    agent_rules: Optional[bool] = typer.Option(  # noqa: UP007
        None,
        "--agent-rules/--no-agent-rules",
        help="managed Probe block in your global CLAUDE.md",
    ),
    channel: str = typer.Option(  # noqa: ARG001 - compat, see below
        None,
        "--channel",
        hidden=True,
        help="accepted and ignored; there is only one channel",
    ),
    uninstall: bool = typer.Option(
        False,
        "--uninstall",
        help="when turning capture off, also remove the plugin (default: keep it)",
    ),
    action: Optional[str] = typer.Option(  # noqa: UP007
        None,
        "--action",
        help="skip the menu: configure | diagnose | update | manual | remove | backfill",
    ),
    folder: str = typer.Option(
        None,
        "--folder",
        help="backfill only: the folder to import, skipping the picker (needed headless)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip the menu and prompts"),
) -> None:
    """Setup wizard: install and configure Probe Research on this device.

    Interactive by default. Every capability is also a flag, and THE FLAGS ARE
    THE CONTRACT -- the menu is a front end over them. An omitted flag preserves
    whatever is already configured, so `--yes` in CI can never silently revoke
    someone's capture pairing.
    """
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui

    # `--channel` is accepted and ignored. Plugins update on the USER's schedule,
    # so a machine whose plugin has not been refreshed still spawns
    # `probe wizard --action update --yes --channel latest`; rejecting the flag
    # would break auto-update on exactly the machines that are behind.
    del channel

    # FIRST, before anything else. `npx probe-research` launches us through an
    # EPHEMERAL `uv tool run` / `pipx run`, which leaves no binary behind — and
    # everything below assumes one exists afterwards (`probe doctor`, the
    # plugin's version-check hook, the MCP headers helper).
    from probe.cli.bootstrap import ensure_persistent_install

    boot = ensure_persistent_install()
    if boot.message:
        print(boot.message)

    caps = doctor_impl.collect()
    configured = caps.configured
    explicit_flags = any(
        f is not None for f in (tracking, capture, auto_update, agent_rules)
    )

    from probe.cli import actions as actions_mod

    # Everything the dashboard used to hide in collapsed sections lives here,
    # next to the state it acts on. The action menu only appears on a RE-RUN: a
    # fresh machine has nothing to diagnose, update or remove.
    chosen_action = actions_mod.Action.CONFIGURE
    if action is not None:
        try:
            chosen_action = actions_mod.Action(action)
        except ValueError:
            print(
                f"unknown action {action!r}; expected one of: "
                f"{', '.join(a.value for a in actions_mod.Action)}",
                file=sys.stderr,
            )
            raise typer.Exit(2) from None
    elif configured and not yes and not explicit_flags and wizard.interactive():
        tui.clear()
        picked = wizard.run_action_menu(caps)
        if picked is None or picked is tui.BACK:
            raise typer.Exit(0)
        chosen_action = picked

    base_now = resolve(base_url=_conn.base_url).base_url

    # The menu comes BACK after each action. Dropping the user to a shell once
    # one task finishes is the same "go do it yourself" failure as printing a
    # command: after an update you want doctor, after a removal you often want
    # to turn something else on.
    looping = (
        action is None
        and configured
        and not yes
        and not explicit_flags
        and wizard.interactive()
    )

    while True:
        if chosen_action is actions_mod.Action.EXIT:
            raise typer.Exit(0)

        lines = _run_wizard_action(
            chosen_action,
            caps=caps,
            base_now=base_now,
            yes=yes,
            tracking=tracking,
            capture=capture,
            auto_update=auto_update,
            agent_rules=agent_rules,
            uninstall=uninstall,
            configured=configured,
            folder=folder,
        )

        # Inside the menu loop this is a PAGE of the wizard, so it gets the
        # same centred treatment as every prompt. A one-shot `--action` run is
        # command output: printing it plainly leaves the user's scrollback
        # alone, which clearing the screen for a single result would not.
        paged = bool(lines) and looping and wizard.interactive()
        if paged:
            tui.page(lines, prompt="Press enter to return to the menu…")
        elif lines:
            print("\n".join(lines))

        if not looping:
            raise typer.Exit(0)

        # Re-read state: the action just changed it, and the next choice should
        # be made against what is true now, not what was true on entry.
        if not paged and wizard.interactive():
            tui.say()
            input(tui.indent("Press enter to return to the menu…"))
        caps = doctor_impl.collect()
        tui.clear()
        picked = wizard.run_action_menu(caps)
        if picked is None or picked is tui.BACK:
            raise typer.Exit(0)
        chosen_action = picked


def _run_wizard_action(
    chosen_action,
    *,
    caps,
    base_now: str,
    yes: bool,
    tracking,
    capture,
    auto_update,
    agent_rules,
    uninstall: bool,
    configured: bool,
    folder: str | None = None,
) -> list[str]:
    """Perform ONE action and RETURN its output.

    Returned, not printed, so the caller can decide whether this is a centred
    page of the wizard or plain command output. An empty list means the action
    already streamed (the configure path has to, because a browser approval
    prints a URL you are meant to read while it waits).
    """
    from probe.cli import actions as actions_mod
    from probe.cli import doctor as doctor_impl
    from probe.cli import setup as wizard
    from probe.cli import tui
    from probe.cli.capture import OffMode

    if chosen_action is actions_mod.Action.DIAGNOSE:
        lines = doctor_impl.render(caps).splitlines()
        notes = actions_mod.troubleshooting(caps)
        if notes:
            lines += ["", "If something is not working:"]
            lines += [f"  - {note}" for note in notes]
        return lines

    if chosen_action is actions_mod.Action.UPDATE:
        from probe.cli.upgrading import perform_update

        # No "Upgrade the CLI now?" gate: picking "Update to the latest
        # version" from the menu IS the answer to that question, and asking it
        # again dropped a bare uncentred prompt into the middle of the wizard.
        outcome = perform_update(base_url=base_now, include_plugin=True)
        lines = list(outcome.lines)
        if outcome.restart_needed:
            lines += ["", "Restart Claude Code to apply the plugin update."]
        lines += _register_local_capabilities(
            doctor_impl.collect(),
            settings=resolve(base_url=base_now),
        )
        return lines

    if chosen_action is actions_mod.Action.MANUAL:
        return [
            *actions_mod.manual_steps(base_url=base_now).splitlines(),
            "",
            *actions_mod.self_host_notes(
                base_url=base_now, mcp_endpoint="https://mcp.research.prbe.ai/mcp"
            ).splitlines(),
        ]

    if chosen_action is actions_mod.Action.UNINSTALL:
        if not yes and wizard.interactive():
            tui.clear()
            if wizard.confirm_removal() is not True:
                return []
        # Preserve the credential in memory long enough to report the actual
        # post-removal state; remove_everything clears it from disk.
        settings_before_removal = resolve(base_url=base_now)
        lines = list(wizard.remove_everything(caps))
        lines += _register_local_capabilities(
            doctor_impl.collect(),
            settings=settings_before_removal,
        )
        return lines

    if chosen_action is actions_mod.Action.BACKFILL:
        from pathlib import Path

        from probe.cli import backfill as backfill_impl

        return backfill_impl.run(
            client_factory=_client,
            start=Path.cwd(),
            folder=Path(folder) if folder else None,
            interactive=wizard.interactive(),
        )

    # CONFIGURE
    selection = wizard.resolve_selection(
        caps,
        tracking=tracking,
        capture=capture,
        auto_update=auto_update,
        agent_rules=agent_rules,
        configured=configured,
    )
    explicit_flags = any(
        f is not None for f in (tracking, capture, auto_update, agent_rules)
    )
    if not yes and not explicit_flags and wizard.interactive():
        tui.clear()
        chosen = wizard.run_menu(selection.as_map())
        if chosen is None or chosen is tui.BACK:
            return []  # Escape / Ctrl-C: back to the action menu, nothing applied.
        selection = chosen

        # Auto-update is asked SEPARATELY, after the capabilities: it is a
        # policy about them, not one of them.
        tui.clear()
        wants_updates = wizard.ask_auto_update(selection.auto_update)
        if wants_updates is None or wants_updates is tui.BACK:
            return []
        # `replace`, never a hand-rolled `Selection(...)`. Re-listing the fields
        # here is a copy of the dataclass that nothing keeps in sync: adding
        # `agent_rules` to Selection left this call one argument short, and
        # since every test stubs `interactive()` to False, the TypeError only
        # ever fired for a real user answering the auto-update question.
        selection = dataclasses.replace(selection, auto_update=bool(wants_updates))

    steps = wizard.plan(caps, selection)
    if not steps:
        return [
            "Already set up the way you asked. Nothing to change.",
            *_register_local_capabilities(
                caps,
                settings=resolve(base_url=base_now),
            ),
        ]

    # From here it STREAMS. Installing a plugin can take a minute and a browser
    # approval prints a URL you are meant to act on while it waits, so this is
    # the one page that cannot be buffered and centred as a block -- it is
    # written while it happens. Centred left-to-right, at least, so it stays in
    # the same column as the prompt that led here.
    tui.clear()
    tui.say("This run will:")
    for step in steps:
        tui.say(f"  - {step}")
    tui.say()

    messages: list[str] = []
    if selection.tracking != caps.tracking_on:
        messages.extend(wizard.apply_tracking(selection.tracking))
    if selection.capture != caps.capture_on:
        messages.extend(
            wizard.apply_capture(
                caps,
                selection.capture,
                mode=OffMode.UNINSTALL if uninstall else OffMode.DISABLE,
            )
        )
    if selection.auto_update != caps.auto_update_enabled:
        messages.extend(wizard.apply_auto_update(selection.auto_update))
    if selection.agent_rules != caps.agent_rules_installed or caps.agent_rules_stale:
        messages.extend(
            wizard.apply_agent_rules(selection.agent_rules, stale=caps.agent_rules_stale)
        )
    for message in messages:
        tui.say(message)

    needs = wizard.needs_authorization(caps, selection)
    granted: dict = {}
    if needs:
        tui.say()
        tui.say(f"One browser approval covers everything you ticked ({', '.join(needs)}).")
        granted, auth_messages = wizard.authorize(
            needs,
            base_url=base_now,
            # The wizard's own printer: the approval URL is the one line the
            # user has to act on, and leaving it at column 0 while everything
            # around it is centred reads as a rendering fault.
            on_prompt=lambda prompt: (
                tui.say(f"  visit: {prompt.verification_uri_complete}"),
                tui.say(f"  code:  {prompt.user_code}"),
            ),
            open_browser=True,
        )
        for message in auth_messages:
            tui.say(message)

    missing = [grant for grant in needs if grant not in granted]
    if missing:
        # "Restart Claude Code to finish" after a FAILED approval reads as
        # success: the user restarts, finds the capability off, and has no idea
        # why. Say what actually happened instead.
        tui.say()
        tui.say(
            f"Not finished — no credential for: {', '.join(missing)}. "
            "Run the wizard again once you can approve in a browser."
        )
    else:
        notice = wizard.restart_notice(caps, selection)
        if notice:
            tui.say()
            tui.say(notice)
    for message in _register_local_capabilities(
        doctor_impl.collect(),
        settings=resolve(base_url=base_now),
    ):
        tui.say(message)

    return []


def _register_local_capabilities(caps, *, settings=None) -> list[str]:
    """Lazy wrapper so ordinary CLI startup never imports setup-only SDK work."""
    from probe.cli.client_installation import register

    return register(caps, settings=settings)


# `probe setup` must keep working: it is printed on the live connect page, in
# shipped plugin copy, and in every PR description written before the rename.
# Registering the SAME function under both names means the alias can never drift
# from the real command's options.
app.command(name="setup", hidden=True)(wizard)


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
        with _new_client(base_url=base_url, token=token, fail_open=False) as client:
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
    """Enough of a token to recognize, never enough to use.

    Shows the TAIL, not the head: the head is a shared prefix (`probe_pat_`,
    `ros_pat_`) that identifies nothing, so leading characters spend secret entropy
    to say what every token already says. `context list` output ends up in bug
    reports and CI logs, so this matches `_fingerprint`'s last-4 convention rather
    than inventing a second, weaker redaction rule.
    """
    if not value:
        return "-"
    return f"…{value[-4:]}" if len(value) > 8 else "set"


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


def _project_id(client: Client, ref: str) -> str:
    """Accept a project id OR a slug, and return the id.

    Every ``/v1/projects/{project_id}`` route types the path param as a UUID, so a
    slug reaches the server as a 422 about UUID parsing rather than a lookup. Slugs
    are the handle people actually remember (they are what `--project` takes on
    `run start`), so resolve them here instead of making the id the only way in.
    """
    try:
        UUID(ref)
        return ref
    except ValueError:
        pass
    for row in client.list_projects(limit=200).items:
        if row.get("slug") == ref:
            return str(row["id"])
    raise typer.BadParameter(f"no project with id or slug {ref!r}")


def _project_slug(client: Client, ref: str | None) -> str | None:
    """The inverse of :func:`_project_id`, for the slug-resolving paths.

    ``Client.run`` resolves a project by *slug* and raises when it is absent, so
    handing it an id makes the lookup miss a project that genuinely exists rather than resolving the
    one you meant. The ambient anchor stores an id (stable across renames), so it has
    to be translated on the way in — as does an explicit ``--project <uuid>``.

    A non-UUID passes through untouched: it is already a slug, and an absent one is
    now an error from ``run start`` rather than a silent create.
    """
    if ref is None:
        return None
    try:
        UUID(ref)
    except ValueError:
        return ref
    return client.get_project(ref).get("slug", ref)


def _confirm_delete(yes: bool, subject: str, *, cascade: str | None = None) -> None:
    """The one confirmation path for every irreversible delete verb.

    This was spelled by hand in four places and forgotten entirely in a fifth:
    ``artifact delete`` took no ``--yes`` and prompted for nothing, so the flag
    that guards the *cascading* deletes was rejected by the one that deletes
    silently. Passing ``--yes`` there — the habit every other delete teaches —
    failed the whole batch, which is the good outcome; had it been accepted and
    ignored, the artifacts would have been gone before anyone read them.

    A verb that forgets to prompt is indistinguishable from one that decided not
    to, so the decision belongs here rather than being re-made per verb.

    ``cascade`` names what else goes; omit it when nothing else does.
    """
    if yes:
        return
    question = f"permanently delete {subject}?"
    if cascade:
        question += f" {cascade}, and this cannot be undone"
    typer.confirm(question, abort=True)


@project_app.command("create")
def project_create(
    slug: str = typer.Argument(..., help="url-safe identifier, unique per tenant"),
    name: str = typer.Option(None, "--name", help="display name (defaults to the slug)"),
    description: str = typer.Option(None, "--description"),
    tag: list[str] = typer.Option(None, "--tag", help="tag at creation (repeatable)"),
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
                tags=tag or None,
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
    tag: list[str] = typer.Option(None, "--tag", help="filter: project must carry ALL (repeatable)"),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    cursor: str = typer.Option(None, "--cursor", help="keyset cursor from a previous page"),
) -> None:
    """List projects in a workspace, or across all of them with --all."""
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    # Omitting workspace_id IS "all workspaces" — the server has no all-sentinel, so
    # --all means "send no filter" rather than some magic value.
    workspace_id = None if all_workspaces else _resolve_workspace(workspace)
    with _client() as c:
        page = c.list_projects(workspace_id=workspace_id, tags=tag or None, **params)
    _print_json({"items": page.items, "next_cursor": page.next_cursor})


@project_app.command("get")
def project_get(project_id: str = typer.Argument(..., help="project id or slug")) -> None:
    """Show one project."""
    with _client() as c:
        _print_json(c.get_project(_project_id(c, project_id)))


@project_app.command("use")
def project_use(
    project_id: str = typer.Argument(..., help="project id or slug to make active"),
) -> None:
    """Set the active project for this context, so `run start` and friends default to it."""
    with _client() as c:
        proj = c.get_project(_project_id(c, project_id))
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
        _print_json(
            c.update_project(_project_id(c, project_id), name=name, description=description)
        )


@project_app.command("tag")
def project_tag(
    project: str = typer.Argument(..., help="project id or slug"),
    add: list[str] = typer.Argument(None, help="tags to add"),
    remove: list[str] = typer.Option(None, "--remove", help="tag to remove; repeatable, ONE tag per flag (a bare word after options is an ADD)"),
    replace: list[str] = typer.Option(None, "--set", help="replace the whole list (repeatable; --set '' clears all)"),
) -> None:
    """Tag a project: positional args add, --remove drops, --set replaces; bare lists."""
    with _client() as c:
        pid = _project_id(c, project)
        _print_json(
            _tag_verb_flow(
                pid,
                c.get_project(pid).get("tags"),
                add,
                remove,
                replace,
                lambda wanted: c.update_project(pid, tags=wanted),
            )
        )


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
        _print_json(c.move_project(_project_id(c, project_id), workspace))


@project_app.command("delete")
def project_delete(
    project_id: str = typer.Argument(..., help="project id or slug"),
    yes: bool = typer.Option(False, "--yes", help="skip the confirmation prompt"),
) -> None:
    """PERMANENTLY delete a project and everything in it. Irreversible."""
    _confirm_delete(
        yes,
        f"project {project_id}",
        cascade="every experiment, run, metric and file inside it goes too",
    )
    with _client() as c:
        c.delete_project(_project_id(c, project_id))
    print(f"{project_id} deleted")


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
    experiment: str = typer.Option(
        None,
        "--experiment",
        help="slug of an EXISTING experiment; omit for a PROJECT-DIRECT run (W&B shape)",
    ),
    name: str = typer.Option(None, "--name", help="defaults to a timestamped name (+ server petname short_id)"),
    description: str = typer.Option(None, "--description", help="human context for this run"),
    project: str = typer.Option(
        None, "--project", help="project slug/id; defaults to the active one (`probe project use`)"
    ),
    group: str = typer.Option(None, "--group", help="run group id (see `probe group create`)"),
    source: str = typer.Option("api", "--source"),
    external_id: str = typer.Option(None, "--external-id"),
    config: list[str] = typer.Option(None, "--config", metavar="k=v"),
    tag: list[str] = typer.Option(None, "--tag"),
) -> None:
    """Open a run inside an EXISTING experiment, or directly under a project.

    This no longer creates the experiment or project. Create them first with
    `probe project create` / `probe experiment create`; an unknown slug now errors
    and names the closest existing ones instead of minting a second identity.
    Without --experiment the run attaches straight to the project (--project or
    the active one) with no experiment at all — the W&B model.
    """
    # This is what makes `probe project use` mean something: without it the ambient
    # project would be stored and displayed but never actually applied to a write.
    # Explicit flag still wins, so scripts never depend on a developer's context.
    resolved_project = resolve(project=project).project
    if not experiment and not resolved_project:
        # Fail in CLI vocabulary before the SDK's run()-phrased error can leak.
        raise errors.ValidationError(
            "pass --experiment for an experiment run, or --project for a "
            "project-direct one (or set an active project with `probe project use`)"
        )
    with _client() as c:
        run = c.run(
            experiment=experiment,
            name=name,
            description=description,
            # run() resolves by SLUG, so an id has to be translated first or the
            # lookup misses and reports a real project as absent.
            project=_project_slug(c, resolved_project),
            group_id=group,
            source=source,
            external_id=external_id,
            config=_kv_pairs(config) if config else None,
            tags=tag or None,
            # A CLI-opened run is detached: this process exits immediately and the
            # run is closed later by `probe run end`. Beating here would stop the
            # moment we exit and get the run reaped mid-flight (see heartbeat_run).
            heartbeat=False,
        )
    print(run.id)


@run_app.command("child")
def run_child(
    run: str = typer.Argument(...),
    name: str = typer.Option(..., "--name"),
    description: str = typer.Option(None, "--description"),
    relation: Relation = typer.Option(Relation.fork, "--relation"),
    source: str = typer.Option("api", "--source"),
    external_id: str = typer.Option(None, "--external-id"),
) -> None:
    """Open a sub-run under an existing run.

    The child inherits the parent's attachment: its experiment when it has one,
    else its project (a project-direct parent begets a project-direct child).
    """
    with _client() as c:
        parent = c.get_run(run)
        common = dict(
            parent_run_id=run,
            parent_relation=relation.value,
            description=description,
            source=source,
            external_id=external_id,
            heartbeat=False,  # detached, same as `run start`
        )
        if parent.get("experiment_id"):
            child = c.create_run(parent["experiment_id"], name, **common)
        elif parent.get("project_id"):
            child = c.create_project_run(parent["project_id"], name, **common)
        else:
            # A pre-0054 backend's run rows carry no project_id field at all;
            # a KeyError traceback here would say nothing actionable.
            raise errors.ValidationError(
                f"run {run} reports neither an experiment nor a project — this "
                "research-os backend predates project-direct runs (0054). "
                "Upgrade the backend, or open the child under an experiment "
                "with `probe run start --experiment`."
            )
    print(child.id)


@run_app.command("set")
def run_set(
    run: str = typer.Argument(..., help="run id"),
    name: str = typer.Option(None, "--name"),
    description: str = typer.Option(None, "--description"),
) -> None:
    """Update a run's human title or description."""
    if name is None and description is None:
        raise typer.BadParameter("pass at least one of --name/--description")
    with _client() as c:
        _print_json(c.update_run(run, name=name, description=description))


@run_app.command("list")
def run_list(
    experiment: str = typer.Option(None, "--experiment", help="experiment id"),
    project: str = typer.Option(None, "--project", help="project id or slug"),
    direct: bool = typer.Option(False, "--direct", help="only project-direct runs"),
    tag: list[str] = typer.Option(None, "--tag", help="filter: run must carry ALL (repeatable)"),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    cursor: str = typer.Option(None, "--cursor", help="keyset cursor from a previous page"),
) -> None:
    """List runs, filterable by experiment, project, and tags (AND semantics)."""
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    with _client() as c:
        page = c.list_runs(
            experiment_id=experiment,
            project_id=_project_id(c, project) if project else None,
            direct=direct,
            tags=tag or None,
            **params,
        )
    _print_json({"items": page.items, "next_cursor": page.next_cursor})


@run_app.command("tag")
def run_tag(
    run: str = typer.Argument(..., help="run id"),
    add: list[str] = typer.Argument(None, help="tags to add"),
    remove: list[str] = typer.Option(None, "--remove", help="tag to remove; repeatable, ONE tag per flag (a bare word after options is an ADD)"),
    replace: list[str] = typer.Option(None, "--set", help="replace the whole list (repeatable; --set '' clears all)"),
) -> None:
    """Tag a run: positional args add, --remove drops, --set replaces; bare lists.

    Read-modify-write over PATCH's whole-list replace (the server normalizes to
    lowercase-kebab and 422s past the caps). Retro-tag runs the SDK is DONE
    with: a still-live run's next push replaces out-of-band edits (last writer
    wins — CONTRACT.md "tags")."""
    with _client() as c:
        handle = _run_handle(c, run)
        # strict=True: an interactive tag edit must fail loudly, never spool a
        # stale whole-list replace for delayed replay (review 2026-07-30).
        _print_json(
            _tag_verb_flow(
                handle.id,
                handle.tags,
                add,
                remove,
                replace,
                lambda wanted: handle.set_tags(wanted, strict=True),
            )
        )


@run_app.command("end")
def run_end(
    run: str = typer.Argument(...),
    status: EndStatus = typer.Option(EndStatus.completed, "--status"),
) -> None:
    """Close a run.

    Synchronous mode is a RUN-SCOPED barrier (T3-A): this run's queued outbox
    ops are delivered first, and the run is not closed while any of them cannot
    be -- unrelated runs' stuck items never block it. Async mode enqueues the
    close as a journal op ORDERED BEHIND everything the run already queued, so
    the run only closes after its data lands; nothing blocks.
    """
    if _conn.async_mode:
        from ..sdk.durable import now_iso

        with _async_client() as c:
            # set_status, not finish(): finish() flushes synchronously, which is
            # exactly what async mode must not do. Ordering is the barrier here.
            _async_run(c, run).set_status(status.value, ended_at=now_iso())
        _kick_drainer()
        print(f"queued end for {run} -> {status.value} (async)")
        return
    from ..sdk.journal import drain

    journal = _journal()
    report = drain(journal, run_ref=run)
    dead = [op for _, op in journal.failed() if op.get("run_ref") == run]
    # Anything of this run's still queued after the drain (paused journal, a
    # skipped pass, any future skip condition) also blocks the close -- the
    # barrier promise is about the RESULT, not about which flag tripped.
    still_queued = [op for _, op in journal.pending() if op.get("run_ref") == run]
    if report.auth_blocked or report.stopped_transient or dead or still_queued:
        detail = "; ".join(
            report.errors[-3:] or [op.get("last_error") or "?" for op in dead[:3]]
        )
        typer.echo(
            f"run {run} NOT closed: its outbox items could not all be delivered "
            f"({detail}). Fix (see `probe outbox status`), retry dead letters "
            "with `probe outbox retry`, then re-run `probe run end`.",
            err=True,
        )
        raise typer.Exit(2)
    from ..sdk.durable import now_iso as _now_iso

    with _client() as c:
        # set_status, not finish(): the run-scoped barrier above already
        # delivered THIS run's ops; finish() would foreground-drain the whole
        # machine-wide journal — other runs' queued gigabytes — inside this
        # command (red team: 'unrelated runs never block it' held for
        # correctness but not for time).
        _run_handle(c, run).set_status(status.value, ended_at=_now_iso())
    # The run is terminal, so release its claim on the box immediately rather
    # than waiting out the lease. Done AFTER set_status and outside the client
    # block: a lease that outlives its run only delays an upgrade, while one
    # dropped before the run actually closed would let an upgrade land on it.
    try:
        from . import run_lock

        run_lock.clear_lease(run)
    except Exception:  # noqa: BLE001 -- an expiring lease is the backstop
        pass
    print(f"{run} -> {status.value}")


@run_app.command("check")
def run_check(
    run: str = typer.Argument(...),
    verify: bool = typer.Option(False, "--verify"),
) -> None:
    """Assess capture completeness (exit 2 if incomplete).

    Default is `unverified`: nothing is obviously absent, which is NOT the same
    as "this run can be rebuilt". `--verify` resolves the recorded code commit
    against its remote and is the only way to earn `complete`.
    """
    with _client() as c:
        result = c.check_run(run, verify=verify)
    _print_json(result)
    if result.get("state") == "incomplete":
        raise typer.Exit(2)


@run_app.command("delete")
def run_delete(
    run: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="skip the confirmation prompt"),
) -> None:
    """PERMANENTLY delete a run and its telemetry. Irreversible."""
    _confirm_delete(yes, f"run {run}", cascade="its spans, metrics and files go too")
    with _client() as c:
        c.delete_run(run)
    print(f"{run} deleted")


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
    # `exec` is the one CLI command that owns a long-running child for the whole
    # life of a run, which makes it the flock tier rather than the lease tier:
    # this process is alive throughout, so the kernel can release the claim if it
    # dies. Without this, an upgrade triggered from any other shell would land in
    # the middle of the very process this command exists to wrap.
    from . import run_lock

    claim = run_lock.acquire(run)
    try:
        with _client() as c:
            result = _run_handle(c, run).execute(argv, cwd=cwd)
    finally:
        if claim is not None:
            claim.release()
    raise typer.Exit(result.returncode)


# -- metrics ----------------------------------------------------------------
@app.command()
def log(
    run: str = typer.Argument(...),
    metric: list[str] = typer.Argument(..., metavar="key=value..."),
    step: int = typer.Option(None, "--step"),
    kind: str = typer.Option("model", "--kind"),
    dim: list[str] = typer.Option(None, "--dim", metavar="k=v"),
    agg: Agg = typer.Option(
        None, "--agg", help="declare the key's reduce fn for grouped reads (0062)"
    ),
    derived: bool = typer.Option(
        False,
        "--derived",
        help="these values were COMPUTED after the fact, not measured live; needs --producer",
    ),
    producer: str = typer.Option(
        None, "--producer", help="what computed them, e.g. claude-code or score_auc.py"
    ),
    note: str = typer.Option(None, "--note", help="why/how, free text"),
    input_key: list[str] = typer.Option(
        None, "--input", metavar="KEY", help="repeatable; stored series the computation read"
    ),
    code_ref: str = typer.Option(None, "--code-ref", help="repo path / commit / script"),
) -> None:
    """Append metric points. --dim adds series dimensions (fold #9).

    --derived marks the batch as computed rather than measured — the door for
    backfilling a metric onto a run that has already finished. It requires
    --producer and a --step, and records provenance beside the points so a
    reader can always tell a computed curve from a captured one.
    """
    metrics = _kv_pairs(metric, cast_float=True)
    dims = _kv_pairs(dim) if dim else None

    if producer and not derived:
        raise typer.BadParameter("--producer only applies to --derived")
    if derived:
        if not producer:
            raise typer.BadParameter(
                "--derived needs --producer: a computed series without provenance is "
                "indistinguishable from a logged one"
            )
        if step is None:
            raise typer.BadParameter(
                "--derived needs an explicit --step: a backfill lands on steps that "
                "already exist, so there is no counter to auto-increment"
            )
        # Deliberately synchronous: the async outbox carries `log` bodies, and a
        # derived batch is a different shape. A backfill is not on the hot path.
        with _client() as c:
            _run_handle(c, run).log_derived(
                metrics,
                step=step,
                producer=producer,
                note=note,
                inputs=list(input_key) or None,
                code_ref=code_ref,
                kind=kind,
                dimensions=dims,
                agg=agg.value if agg else None,
            )
        print(f"logged {len(metrics)} derived metric(s) to {run} (producer={producer})")
        return

    if _conn.async_mode:
        with _async_client() as c:
            _async_run(c, run).log(
                metrics, step=step, kind=kind, dimensions=dims,
                agg=agg.value if agg else None,
            )
        _kick_drainer()
        print(f"queued {len(metrics)} metric(s) for {run} (async)")
        return
    with _client() as c:
        _run_handle(c, run).log(
            metrics, step=step, kind=kind, dimensions=dims, agg=agg.value if agg else None
        )
    print(f"logged {len(metrics)} metric(s) to {run}")


# -- coordinate reads (below-run coordinates, research-os 0059-0062) ---------
metrics_app = typer.Typer(no_args_is_help=True, help="coordinate-aware metric reads")
app.add_typer(metrics_app, name="metrics")


@metrics_app.command("grouped")
def metrics_grouped(
    run: str = typer.Argument(...),
    key: str = typer.Option(..., "--key"),
    kind: str = typer.Option(None, "--kind"),
    agg: Agg = typer.Option(
        None, "--agg", help="omit for the key's declared reduce fn (else mean)"
    ),
    by: list[str] = typer.Option(
        None, "--by", help="repeatable; one cell per combination of these coordinate axes"
    ),
    where: str = typer.Option(
        None, "--where", metavar="JSON", help='coord filter, e.g. \'{"split": "train"}\''
    ),
    step_bucket: int = typer.Option(None, "--step-bucket"),
    step_from: int = typer.Option(None, "--step-from"),
    step_to: int = typer.Option(None, "--step-to"),
    max_rows: int = typer.Option(None, "--max-rows"),
) -> None:
    """Server-side reduce/group over one metric's stepped points (paging followed)."""
    with _client() as c:
        _print_json(
            c.get_metrics_grouped(
                run,
                key,
                kind=kind,
                agg=agg.value if agg else None,
                by=by or None,
                where=_json_value(where),
                step_bucket=step_bucket,
                step_from=step_from,
                step_to=step_to,
                max_rows=max_rows,
            )
        )


@metrics_app.command("wide")
def metrics_wide(
    run: str = typer.Argument(...),
    key: list[str] = typer.Option(None, "--key", help="repeatable; narrow to these keys"),
    kind: str = typer.Option(None, "--kind"),
    step_from: int = typer.Option(None, "--step-from"),
    step_to: int = typer.Option(None, "--step-to"),
    max_rows: int = typer.Option(None, "--max-rows"),
) -> None:
    """Step x metric table for a run (the DataFrame pivot; paging followed)."""
    with _client() as c:
        _print_json(
            c.get_metrics_wide(
                run,
                key=key or None,
                kind=kind,
                step_from=step_from,
                step_to=step_to,
                max_rows=max_rows,
            )
        )


@metrics_app.command("export")
def metrics_export(
    run: str = typer.Argument(...),
    key: str = typer.Option(None, "--key"),
    kind: str = typer.Option(None, "--kind"),
    step_from: int = typer.Option(None, "--step-from"),
    step_to: int = typer.Option(None, "--step-to"),
    limit: int = typer.Option(None, "--limit", help="page size of the keyset walk"),
) -> None:
    """Lossless raw-point export, one JSON point per line.

    NDJSON rather than one array on purpose: the export is the unbounded read,
    and a stream that prints as it pages can be piped without buffering the run.
    """
    with _client() as c:
        for point in c.export_metric_points(
            run, key=key, kind=kind, step_from=step_from, step_to=step_to, limit=limit
        ):
            print(json.dumps(point, default=str))


@metrics_app.command("delete")
def metrics_delete(
    run: str = typer.Argument(...),
    key: str = typer.Option(..., "--key"),
    kind: str = typer.Option("model", "--kind"),
    dim: list[str] = typer.Option(
        None, "--dim", metavar="k=v", help="pin ONE dimension variant"
    ),
    yes: bool = typer.Option(False, "--yes", help="skip the confirmation"),
) -> None:
    """Delete a DERIVED series and its points.

    The undo for a computed metric that was wrong. Derived only — a logged
    series is the run's captured record and the server refuses (409).

    Omitting --dim addresses the dimension-less series, NOT every variant of the
    key: a key logged per-rank needs the pin.
    """
    dims = _kv_pairs(dim) if dim else None
    label = f"{kind}/{key}" + (f" {dims}" if dims else "")
    _confirm_delete(yes, f"derived series {label} on run {run}")
    with _client() as c:
        c.delete_series(run, key=key, kind=kind, dimensions=dims)
    print(f"deleted derived series {label}")


@app.command()
def coordinates(run: str = typer.Argument(...)) -> None:
    """The run's coordinate catalog: every coordinate any fact landed on."""
    with _client() as c:
        _print_json(c.list_run_coordinates(run))


# -- expression views (read-time computed panels, research-os 0088) ---------
# The AUTHORING surface. The dashboard renders, renames and deletes views but
# never composes one: an expression comes from a researcher's agent session or
# their own script, so the door it comes through is here and the SDK's
# `probe.expr`. --spec-file is literally that path — a script writes JSON, this
# uploads it.
views_app = typer.Typer(
    no_args_is_help=True, help="expression views: formulas over a run's logged series"
)
app.add_typer(views_app, name="views")


def _read_spec(spec: str | None, spec_file: str | None) -> dict:
    """Resolve --spec / --spec-file into a validated view spec.

    ``--spec-file -`` reads stdin, so a generator script can pipe straight in
    without a temp file. Validation happens here rather than at the server, so a
    typo names its own field instead of coming back as a 422.
    """
    if (spec is None) == (spec_file is None):
        raise typer.BadParameter("pass exactly one of --spec or --spec-file")
    if spec_file is not None:
        raw = sys.stdin.read() if spec_file == "-" else Path(spec_file).read_text()
    else:
        raw = spec
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"spec is not valid JSON: {exc}") from exc

    from probe import expr as expr_module

    try:
        return expr_module.spec(parsed)
    except Exception as exc:  # noqa: BLE001 -- pydantic error text is the message
        raise typer.BadParameter(f"invalid expression spec: {exc}") from exc


@views_app.command("list")
def views_list(run: str = typer.Argument(...)) -> None:
    """Every expression view on a run, with its spec and provenance."""
    with _client() as c:
        _print_json(c.list_views(run))


@views_app.command("show")
def views_show(
    run: str = typer.Argument(...),
    view: str = typer.Argument(..., metavar="VIEW", help="view id or name"),
) -> None:
    """One view, by id or by name."""
    with _client() as c:
        views = c.list_views(run)
    for row in views:
        if view in (str(row.get("id")), row.get("name")):
            _print_json(row)
            return
    names = ", ".join(sorted(str(r.get("name")) for r in views)) or "none"
    raise typer.BadParameter(f"no view {view!r} on run {run}; run has: {names}")


@views_app.command("create")
def views_create(
    run: str = typer.Argument(...),
    name: str = typer.Argument(..., help="panel title; unique per run"),
    spec: str = typer.Option(None, "--spec", metavar="JSON", help="inline spec"),
    spec_file: str = typer.Option(
        None, "--spec-file", metavar="PATH", help="spec JSON from a file, or - for stdin"
    ),
) -> None:
    """Save a view. Works on a completed run — a view reads, it never appends."""
    body = _read_spec(spec, spec_file)
    with _client() as c:
        _print_json(c.create_view(run, name, body))


@views_app.command("preview")
def views_preview(
    run: str = typer.Argument(...),
    spec: str = typer.Option(None, "--spec", metavar="JSON"),
    spec_file: str = typer.Option(None, "--spec-file", metavar="PATH", help="or - for stdin"),
    step_from: int = typer.Option(None, "--step-from"),
    step_to: int = typer.Option(None, "--step-to"),
    max_points: int = typer.Option(None, "--max-points"),
) -> None:
    """Evaluate a spec WITHOUT saving it.

    Run this before `create`: a spec naming a series the run never logged comes
    back here with `missing_inputs`, instead of being saved as a panel that
    renders empty for everyone who opens the run.
    """
    body = _read_spec(spec, spec_file)
    with _client() as c:
        _print_json(
            c.preview_view(
                run, body, step_from=step_from, step_to=step_to, max_points=max_points
            )
        )


@views_app.command("data")
def views_data(
    run: str = typer.Argument(...),
    view_id: str = typer.Argument(...),
    step_from: int = typer.Option(None, "--step-from"),
    step_to: int = typer.Option(None, "--step-to"),
    max_points: int = typer.Option(None, "--max-points"),
) -> None:
    """Evaluate a saved view and print its curve.

    Read the whole envelope, not just `points`: `missing_inputs` names series the
    expression referenced that the run has none of, `dropped_nonfinite` counts
    NaN/inf results, and `truncated` says the input scan hit its bound.
    """
    with _client() as c:
        _print_json(
            c.view_data(
                run, view_id, step_from=step_from, step_to=step_to, max_points=max_points
            )
        )


@views_app.command("rename")
def views_rename(
    view_id: str = typer.Argument(...),
    name: str = typer.Argument(...),
) -> None:
    """Rename a view. The expression is untouched."""
    with _client() as c:
        _print_json(c.update_view(view_id, name=name))


@views_app.command("delete")
def views_delete(view_id: str = typer.Argument(...)) -> None:
    """Delete a view. The series it read are untouched."""
    with _client() as c:
        c.delete_view(view_id)
    print(f"deleted view {view_id}")


series_app = typer.Typer(no_args_is_help=True, help="cross-run series reads")
app.add_typer(series_app, name="series")


@series_app.command("latest")
def series_latest(
    runs: list[str] = typer.Argument(..., metavar="RUN..."),
    key: list[str] = typer.Option(None, "--key", help="repeatable; narrow to these keys"),
    kind: str = typer.Option(None, "--kind"),
) -> None:
    """Cross-run scalar summary (last/min/max per series) from the catalog."""
    with _client() as c:
        _print_json(c.latest_scalars(runs, keys=key or None, kind=kind))


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
    if _conn.async_mode:
        with _async_client() as c:
            handle = _async_run(c, run).span(
                span_type,
                name=name,
                step_index=step,
                provider=provider,
                external_key=external_key,
                parent_span_id=parent,
                status=status,
                attributes=_kv_pairs(attr) if attr else None,
            )
        _kick_drainer()
        # The span id is minted client-side, so async still hands it back.
        print(handle)
        return
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


def _anchor_id_for(client: Client, anchor: Anchor, anchor_id: str | None) -> str | None:
    """Let a project anchor be named by SLUG, not only by id.

    Every ``/v1/projects/{project_id}`` route types the path param as a UUID, so
    a slug reaches the server as a 422 about UUID parsing. That is survivable
    for a human who can look the id up once; it is not survivable for an agent
    filing a few thousand artifacts, which would have to thread a uuid through
    every command and only has to get it wrong once.

    Slugs are the handle people actually remember -- the same reason
    :func:`_project_id` exists for the project verbs.

    ADDITIVE, never a new gate. An id passes straight through, and so does
    anything that does not resolve: this route already answers a bad anchor with
    a 422, and turning that into a local hard error would reject values that
    were previously fine (an id this caller cannot enumerate, a project outside
    the page `_project_id` walks). The exact `?slug=` lookup is used rather than
    listing, so it is one request and correct past 200 projects.
    """
    if anchor is not Anchor.PROJECT or not anchor_id:
        return anchor_id
    try:
        UUID(anchor_id)
        return anchor_id
    except ValueError:
        pass
    try:
        found = client.resolve_project(anchor_id)
    except Exception:  # noqa: BLE001 - resolution is a convenience, never a gate
        return anchor_id
    return str(found["id"]) if found else anchor_id


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


def _ping_presign(
    anchor: Anchor,
    anchor_id: str | None,
    name: str,
    *,
    digest: str,
    size: int,
    content_type: str | None,
    kind: str | None,
    meta: dict | None,
    span_id: str | None,
    step_index: int | None,
    context: dict | None = None,
) -> str | None:
    """The 1A intent ping: a capped, best-effort presign at enqueue so the
    server's pending row (and its reaper) know this upload is coming. Never
    raises and never waits past the cap -- a dead network degrades to
    local-only enqueue, and the drainer re-presigns on every attempt anyway.
    """
    try:
        from ..sdk.transport import Transport

        if context is not None:
            # Same principal as the drain will use (red team: an ambient env
            # token could register the intent row under a different tenant
            # than the one the op's pinned context delivers to).
            from ..sdk.journal import _settings_for

            settings = _settings_for(context)
        else:
            from ..sdk.config import resolve

            settings = resolve(base_url=_conn.base_url)
        if not settings.token:
            return None
        transport = Transport(
            settings,
            timeout=_PING_TIMEOUT_SECONDS,
            max_retries=_PING_MAX_RETRIES,
            surface=Surface.CLI.value,
            client_headers=client_version_headers("cli", __version__),
        )
        with Client(settings=settings, transport=transport) as ping:
            presign = ping.presign_upload(
                anchor,
                anchor_id,
                name,
                digest=digest,
                size=size,
                content_type=content_type,
                kind=kind,
                meta=meta,
                span_id=span_id,
                step_index=step_index,
            )
        return presign.get("artifact_id")
    except Exception:  # noqa: BLE001 -- intent registration is best-effort by design
        return None


_REFERENCE_ROUTES = {
    Anchor.EXPERIMENT: "/v1/experiments/{id}/artifacts",
    Anchor.PROJECT: "/v1/projects/{id}/artifacts",
}

#: The 1A intent-ping cap. Load-bearing product behavior (CHANGELOG: "a
#: ~2s-capped presign ping") -- named so the documented cap and the code
#: cannot silently drift.
_PING_TIMEOUT_SECONDS = 2.0
_PING_MAX_RETRIES = 0


def _artifact_add_async(
    anchor: Anchor,
    anchor_id: str | None,
    name: str,
    *,
    path: str | None,
    uri: str | None,
    reference: bool,
    hash_content: bool,
    allow_missing: bool,
    kind: str,
    step: int | None,
    span: str | None,
    content_type: str | None,
    meta: dict | None,
) -> None:
    """Queue an artifact operation and return immediately (--async).

    Reference/uri forms are pure JSON writes and journal as http ops -- zero
    staging, the fastest path. Byte uploads snapshot into the blob store;
    files at or under the 11A threshold fingerprint inline and fire the capped
    presign ping so the server registers intent, bigger files defer hashing to
    the drainer so return time stays flat.
    """
    run_ref = anchor_id if anchor is Anchor.RUN else None
    if reference or uri is not None:
        with _async_client() as c:
            if anchor is Anchor.RUN:
                _async_run(c, anchor_id).log_artifact(
                    name, path=path, uri=uri, reference=reference,
                    hash_content=hash_content, allow_missing=allow_missing,
                    kind=kind, step_index=step, span_id=span,
                    content_type=content_type, meta=meta,
                )
            else:
                if reference:
                    fields = reference_fields(
                        path, hash_content=hash_content, allow_missing=allow_missing
                    )
                    body = {"name": name, "is_reference": True, **fields}
                else:
                    body = {"name": name, "uri": uri, "is_reference": True}
                if content_type:
                    body["content_type"] = content_type
                c.journal.append_http(
                    "POST", _REFERENCE_ROUTES[anchor].format(id=anchor_id), body
                )
        _kick_drainer()
        print(f"queued reference {name!r} (async)")
        return

    if not path:
        raise typer.BadParameter("needs a file path (--reference, or --uri)")
    if not os.path.isfile(path):
        # A FIFO, device, or procfs stream would block fingerprint/snapshot
        # forever -- async promises bounded enqueue time (codex).
        raise typer.BadParameter(f"{path} is not a regular file")
    from ..sdk.journal import INLINE_HASH_MAX_BYTES

    run_only = anchor is Anchor.RUN
    with _async_client() as c:
        # Snapshot first, hash the snapshot (inside append_upload): hashing
        # the live file and copying it later would let a same-size rewrite
        # in between poison the content address (codex TOCTOU).
        queued = c.journal.append_upload(
            anchor=anchor.value,
            anchor_id=anchor_id,
            name=name,
            src_path=path,
            inline_hash=os.path.getsize(path) <= INLINE_HASH_MAX_BYTES,
            content_type=content_type,
            kind=kind if run_only else None,
            meta=meta if run_only else None,
            span_id=span,
            step_index=step,
            run_ref=run_ref,
        )
    pinged = False
    if queued["blob"] is not None:
        pinged = (
            _ping_presign(
                anchor, anchor_id, name,
                digest=queued["blob"], size=queued["size_bytes"],
                content_type=content_type,
                kind=kind if run_only else None, meta=meta if run_only else None,
                span_id=span, step_index=step,
                context=c.journal.context,
            )
            is not None
        )
    _kick_drainer()
    registered = "intent registered" if pinged else "intent deferred to drain"
    print(f"queued upload {name!r} op={queued['op_id']} ({registered})")


@artifact_app.command("add")
def artifact_add(
    run: str = typer.Argument(None, help="run id — omit when using an anchor flag"),
    path: str = typer.Argument(None, help="local file to upload"),
    uri: str = typer.Option(None, "--uri", help="record a reference to an existing object"),
    name: str = typer.Option(None, "--name"),
    kind: str = typer.Option("file", "--kind", help="run anchor only"),
    step: int = typer.Option(None, "--step", help="run anchor only"),
    span: str = typer.Option(None, "--span", help="associate with a run span UUID"),
    content_type: str = typer.Option(None, "--content-type"),
    meta: list[str] = typer.Option(None, "--meta", metavar="k=v", help="run anchor only"),
    project: str = typer.Option(None, "--project", help="anchor to a project"),
    experiment: str = typer.Option(None, "--experiment", help="anchor to an experiment"),
    workspace: str = typer.Option(
        None, "--workspace", help="anchor to a workspace (a file, not an artifact)"
    ),
    shared: bool = typer.Option(False, "--shared", help="put it in the team Shared folder"),
    reference: bool = typer.Option(
        False,
        "--reference",
        help="record the file's PATH as a reference (file://) instead of uploading its "
        "bytes — for large files on a shared volume the agent resolves locally",
    ),
    hash_content: bool = typer.Option(
        False,
        "--hash",
        help="also fingerprint a --reference (reads the whole file; enables dedup)",
    ),
    allow_missing: bool = typer.Option(
        False,
        "--allow-missing",
        help="record a --reference even if the path is not visible from this host",
    ),
) -> None:
    """Record an artifact against a run, project, experiment, workspace, or Shared.

    With a path and no --uri/--reference the real upload runs (fingerprint -> presign ->
    PUT -> confirm). With --reference the file's PATH is recorded (file://, bytes NOT
    uploaded) — for a 16GB checkpoint or a shared-volume file an agent resolves locally.
    With --uri it records a reference to an object already in a bucket. References are
    run/project/experiment only — a workspace/Shared file *is* its bytes.
    """
    anchored = project or experiment or workspace or shared
    if anchored:
        # With an anchor flag there is no RUN, so the single positional is the path.
        # Shifting here (rather than guessing from the value) keeps `add ./f.bin` and
        # `add RUN ./f.bin` both unambiguous.
        if path is not None:
            # Two positionals means the caller passed RUN *and* an anchor flag. Catch
            # it here: after the shift below `run` is always None, so _pick_anchor's
            # two-anchor check can no longer see the RUN and the id would be silently
            # reinterpreted as a file path (an unhandled FileNotFoundError).
            raise typer.BadParameter(
                "an artifact anchors to exactly one thing; got RUN and an anchor flag. "
                "Drop the RUN argument, or drop the flag."
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
    if reference and uri is not None:
        raise typer.BadParameter(
            "--reference derives a file:// pointer from the path; pass --reference OR "
            "--uri, not both"
        )
    if reference and not path:
        raise typer.BadParameter("--reference needs a local file path")
    if (hash_content or allow_missing) and not reference:
        raise typer.BadParameter("--hash and --allow-missing only apply to --reference")
    if anchor is not Anchor.RUN:
        if step is not None or kind != "file" or span is not None or meta:
            raise typer.BadParameter(
                f"--kind/--step/--span/--meta are run-only; "
                f"the {anchor.value} upload contract rejects them"
            )
        if (uri is not None or reference) and anchor in _FILE_ANCHORS:
            # Caught here rather than in the SDK so it reads as a usage error instead
            # of an unhandled ValueError traceback: a file IS its bytes, so there is
            # no reference-without-bytes form and the backend declares no such route.
            raise typer.BadParameter(
                f"a {anchor.value} file cannot be a reference (it IS its bytes). "
                "Pass a local path to upload the bytes instead."
            )

    if _conn.async_mode:
        _artifact_add_async(
            anchor, anchor_id, resolved,
            path=path, uri=uri, reference=reference, hash_content=hash_content,
            allow_missing=allow_missing, kind=kind, step=step, span=span,
            content_type=content_type, meta=_kv_pairs(meta) if meta else None,
        )
        return

    if anchor is Anchor.RUN:
        with _client() as c:
            _run_handle(c, anchor_id).log_artifact(
                resolved, path=path, uri=uri, reference=reference,
                hash_content=hash_content, allow_missing=allow_missing,
                kind=kind, step_index=step, span_id=span, content_type=content_type,
                meta=_kv_pairs(meta) if meta else None,
            )
        print(f"artifact {resolved!r} recorded on {anchor_id}")
        return
    with _client() as c:
        anchor_id = _anchor_id_for(c, anchor, anchor_id)
        if reference:
            fields = reference_fields(
                path, hash_content=hash_content, allow_missing=allow_missing
            )
            body = {"name": resolved, "is_reference": True, **fields}
            if content_type:
                body["content_type"] = content_type
            _print_json(c.create_anchored_reference(anchor, anchor_id, body))
        elif uri is not None:
            body = {"name": resolved, "uri": uri, "is_reference": True}
            if content_type:
                body["content_type"] = content_type
            _print_json(c.create_anchored_reference(anchor, anchor_id, body))
        else:
            if not path:
                raise typer.BadParameter("needs a file path (--reference, or --uri)")
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
        _print_json(c.list_anchored(anchor, _anchor_id_for(c, anchor, anchor_id)))


@artifact_app.command("download")
def artifact_download(
    artifact_id: str = typer.Argument(..., help="artifact id (from `probe artifact list`)"),
    output: str = typer.Option(
        None, "--output", "-o", "--to", metavar="PATH",
        help="write the bytes here; '-' forces stdout. Omit to write to stdout "
             "(refused at a terminal).",
    ),
    sha256: str = typer.Option(
        None, "--sha256", metavar="HEX",
        help="expected content_hash; fail (deleting a written file) if the bytes differ",
    ),
    version: int = typer.Option(
        None, "--version", metavar="N",
        help="fetch this version's bytes instead of the artifact's live content "
             "(from `probe artifact versions`)",
    ),
) -> None:
    """Download an artifact's bytes through a presigned GET.

    To a PATH it streams straight to the file -- never buffering the whole blob,
    which can be model weights -- and prints {dest, size_bytes, sha256}. To stdout it
    buffers in memory so the hash can be checked before a byte is emitted. Pass
    --sha256 to verify the round trip against the content_hash from
    `probe artifact list`; a metadata match alone never proves the blob exists.

    --version resolves a pin: it fetches that exact version's bytes, which is what a
    reproduction needs, rather than whatever the name points at today."""
    to_stdout = output is None or output == "-"
    if to_stdout and output is None and sys.stdout.isatty():
        raise typer.BadParameter(
            "refusing to write binary to a terminal; pass -o PATH, or '-o -' / a "
            "redirect to force stdout"
        )
    with _client() as c:
        try:
            if to_stdout:
                data = (
                    c.download_artifact_version(artifact_id, version)
                    if version is not None
                    else c.download_artifact(artifact_id)
                )
                digest = hashlib.sha256(data).hexdigest()
                if sha256 and digest != sha256:
                    typer.echo(
                        f"sha256 mismatch: expected {sha256}, got {digest} ({len(data)} bytes)",
                        err=True,
                    )
                    raise typer.Exit(1)
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
                typer.echo(f"{artifact_id}  {len(data)} bytes  sha256={digest}", err=True)
                return
            # download_artifact*_to removes its own partial file on a mid-stream failure.
            result = (
                c.download_artifact_version_to(artifact_id, version, output)
                if version is not None
                else c.download_artifact_to(artifact_id, output)
            )
            if sha256 and result["sha256"] != sha256:
                Path(output).unlink(missing_ok=True)
                typer.echo(
                    f"sha256 mismatch: expected {sha256}, got {result['sha256']}; deleted {output}",
                    err=True,
                )
                raise typer.Exit(1)
            _print_json(result)
        except errors.RosError as exc:
            # A reference has no managed blob to download; the server 409s with the
            # pointer so we can show the path instead of the raw error. Read it from
            # where it lives (e.g. the shared volume) -- Probe stores no bytes for it.
            detail = exc.detail if isinstance(exc.detail, dict) else {}
            status = getattr(exc, "status", None)
            # A force-deleted version answers 410 WITH who/when, so a reproduction can
            # tell "deliberately destroyed" apart from "never existed". Say which.
            if status == 410 and version is not None:
                when = detail.get("deleted_at") or "(unknown time)"
                who = detail.get("deleted_by")
                typer.echo(
                    f"artifact {artifact_id} version {version} was deleted at {when}"
                    + (f" by {who}" if who else "")
                    + "\nThe version record survives; its bytes do not.",
                    err=True,
                )
                raise typer.Exit(2)
            if status != 409 or detail.get("reason") != "reference":
                raise
            where = detail.get("local_path") or detail.get("uri") or "(unknown location)"
            host = detail.get("host")
            what = f"artifact {artifact_id}" + (f" version {version}" if version is not None else "")
            typer.echo(
                f"{what} is a reference -> {where}"
                + (f" on {host}" if host else "")
                + "\nProbe stores no bytes for it; read it from that path.",
                err=True,
            )
            raise typer.Exit(2)


@artifact_app.command("versions")
def artifact_versions(
    artifact_id: str = typer.Argument(..., help="artifact id (from `probe artifact list`)"),
) -> None:
    """List an artifact's version chain.

    An artifact is a named thing in a container; this is the content history behind
    that name. `origin` says how each version got its bytes: `uploaded` (pushed
    directly) or `pinned` (promoted zero-copy from another artifact). Immutability is
    a property of the version, not the artifact -- renaming or moving the artifact
    never breaks a pin."""
    with _client() as c:
        _print_json(c.list_artifact_versions(artifact_id))


@artifact_app.command("pin-impact")
def artifact_pin_impact(
    artifact_id: str = typer.Argument(..., help="artifact id (from `probe artifact list`)"),
) -> None:
    """Show which projects and experiments pin this artifact's versions.

    Run this before deleting anything: it reports the actual work that would break,
    not a count. `pinned: false` means nothing published depends on it."""
    with _client() as c:
        _print_json(c.artifact_pin_impact(artifact_id))


@artifact_app.command("version-add")
def artifact_version_add(
    artifact_id: str = typer.Argument(..., help="the artifact to append a version to"),
    from_artifact: str = typer.Option(
        None, "--from-artifact", metavar="ID",
        help="promote another artifact's content ZERO-COPY: pins its hash, uri and "
             "size; the stored object is shared, never re-uploaded",
    ),
    uri: str = typer.Option(
        None, "--uri", help="name the pointer directly instead of promoting an artifact"
    ),
    sha256: str = typer.Option(
        None, "--sha256", metavar="HEX", help="content_hash for --uri"
    ),
    size_bytes: int = typer.Option(None, "--size-bytes"),
    content_type: str = typer.Option(None, "--content-type"),
    label: str = typer.Option(
        None, "--label", help="an alternate selector for this version (e.g. 'prod')"
    ),
) -> None:
    """Append the next version of an artifact.

    Exactly one source: --from-artifact (zero-copy promotion) or --uri. Appending
    content identical to the artifact's current live content is a no-op that returns
    the existing version, so this is safe to retry."""
    if bool(from_artifact) == bool(uri):
        raise typer.BadParameter("pass exactly one of --from-artifact or --uri")
    with _client() as c:
        _print_json(
            c.create_artifact_version(
                artifact_id,
                from_artifact_id=from_artifact,
                uri=uri,
                content_hash=sha256,
                size_bytes=size_bytes,
                content_type=content_type,
                label=label,
            )
        )


@artifact_app.command("delete")
def artifact_delete(
    artifact_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="skip the confirmation prompt"),
) -> None:
    """PERMANENTLY delete an artifact. Irreversible."""
    _confirm_delete(
        yes,
        f"artifact {artifact_id}",
        cascade="its stored bytes and version history go with it",
    )
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
    replace: bool = typer.Option(
        False, "--replace", help="supersede a same-named file already in Shared"
    ),
) -> None:
    """Move one of your workspace files into the team's Shared folder.

    A MOVE, not a copy: the file leaves your workspace listing. Ownership transfers
    and the search index is re-keyed in the same transaction.

    A name already taken in Shared is a 409 — the server never silently supersedes
    someone else's file. Pass --replace to do it deliberately.
    """
    with _client() as c:
        _print_json(c.share_workspace_file(artifact_id, replace=replace))


@shared_app.command("unshare")
def shared_unshare(
    artifact_id: str = typer.Argument(..., help="a shared file id"),
    replace: bool = typer.Option(
        False, "--replace", help="supersede a same-named file already in your workspace"
    ),
) -> None:
    """Move a Shared file back into your personal workspace."""
    with _client() as c:
        _print_json(c.unshare_file(artifact_id, replace=replace))


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


def _trial_result_summary(result: dict) -> dict:
    manifest = result.get("manifest") or {}
    return {
        "trial": result["trial"],
        "span_id": result["span_id"],
        "reward": result["reward"],
        "manifest_artifact_id": manifest.get("id") if isinstance(manifest, dict) else None,
        "files": len(result["files"]),
        "uploaded": sum(1 for item in result["files"] if item.get("uploaded")),
        "trajectory": result.get("trajectory"),
        "capture": result.get("capture"),
    }


@trial_app.command("stage")
def trial_stage(
    trial_dir: str = typer.Argument(..., help="live Harbor trial output directory"),
    destination: str = typer.Option(
        ..., "--to", help="durable destination outside the sandbox (for example a shared PVC)"
    ),
    expect: list[str] = typer.Option(
        None, "--expect", metavar="RELATIVE_PATH", help="repeatable required producer output"
    ),
) -> None:
    """Copy + checksum Harbor's host trial output; performs no network writes."""
    from ..connectors.harbor import stage_trial

    staged = stage_trial(trial_dir, destination, expected_paths=expect or ())
    _print_json(
        {
            "trial_dir": str(staged.trial_dir),
            "ledger": str(staged.ledger.path),
            "durable_collection_complete": staged.durable_collection_complete,
            "completeness": staged.ledger.report(),
        }
    )
    if not staged.durable_collection_complete:
        raise typer.Exit(2)


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
    _print_json(_trial_result_summary(result))


@trial_app.command("reconcile")
def trial_reconcile(
    run: str = typer.Argument(...),
    trial_dir: str = typer.Argument(..., help="a directory created by `probe trial stage`"),
    step: int = typer.Option(None, "--step", help="override the step recorded in the ledger"),
    env_type: str = typer.Option(None, "--env-type", help="opaque environment label"),
) -> None:
    """Retry unconfirmed staged bytes and publish the latest completeness manifest."""
    from ..connectors.harbor import reconcile_staged_trial

    kwargs: dict[str, Any] = {
        "environment": {"type": env_type} if env_type else None,
        "source_mode": "cli-reconcile",
    }
    if step is not None:
        kwargs["step_index"] = step
    with _client() as client:
        result = reconcile_staged_trial(_run_handle(client, run), trial_dir, **kwargs)
    _print_json(_trial_result_summary(result))


@trial_app.command("export")
def trial_export(
    request: str = typer.Argument(..., help="a probe-harbor-export/1 export-request.json"),
    run: Optional[str] = typer.Option(None, "--run", help="later-resolved Probe run ID"),
) -> None:
    """Consume one durable Miles/Harbor export request; retry safe."""
    from ..connectors.harbor_export import consume_export_request

    try:
        with _client() as client:
            result = consume_export_request(client, request, run_id=run)
    except Exception as exc:
        typer.echo(f"export failed (staged bytes retained): {type(exc).__name__}: {exc}", err=True)
        raise typer.Exit(1) from exc
    _print_json(result)


@trial_app.command("drain")
def trial_drain(
    capture_root: str = typer.Argument(..., help="root containing export-request.json files"),
    run: Optional[str] = typer.Option(None, "--run", help="later-resolved Probe run ID"),
) -> None:
    """Retry every non-completed Miles/Harbor export request below a capture root."""
    from ..connectors.harbor_export import drain_export_requests

    with _client() as client:
        result = drain_export_requests(client, capture_root, run_id=run)
    _print_json(result)
    if result["failed"]:
        raise typer.Exit(2)


@trial_app.command("watch")
def trial_watch(
    capture_root: str = typer.Argument(..., help="root containing export-request.json files"),
    interval: float = typer.Option(5.0, "--interval", min=0.1, help="poll interval in seconds"),
    once: bool = typer.Option(False, "--once", help="drain once and exit (deployment smoke check)"),
    run: Optional[str] = typer.Option(None, "--run", help="later-resolved Probe run ID"),
) -> None:
    """Continuously export newly staged Harbor trials from a durable capture root."""
    from ..connectors.harbor_export import drain_export_requests
    from ._watch import watch

    with _client() as client:
        watch(
            lambda: drain_export_requests(client, capture_root, run_id=run),
            interval=interval,
            once=once,
            report=_print_json,
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
        doc = json.loads(c.transport.get_url(c.presign_download(traj_entry["artifact_id"])))
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
    venv: str = typer.Option(
        None, "--venv", help="virtualenv to record; auto-detected from --cwd otherwise"
    ),
    no_env: bool = typer.Option(False, "--no-env"),
    no_gpu: bool = typer.Option(False, "--no-gpu"),
    no_upload: bool = typer.Option(
        False, "--no-upload", help="do NOT store the bytes git cannot supply"
    ),
    max_upload_mb: int = typer.Option(
        256, "--max-upload-mb", help="refuse (never truncate) above this size"
    ),
    include: list[str] = typer.Option(
        None,
        "--include",
        metavar="GLOB",
        help="also capture paths git ignores (datasets, checkpoints, out-of-tree configs)",
    ),
    reference_over_mb: int = typer.Option(
        100,
        "--reference-over-mb",
        help="above this, record where a file lives instead of copying it",
    ),
) -> None:
    """Non-disruptive code + env capture.

    Files git can already supply from a pushed remote are recorded as references.
    Everything else — edited, untracked, unpushed, or no remote at all — has its
    BYTES uploaded as a `code-bytes` artifact, because a sha256 verifies a file
    you already have rather than producing one you do not. `--no-upload` skips
    that, and the run is then not reproducible from the record alone.

    The dependency set comes from the PROJECT's virtualenv, not this CLI's. `probe`
    is normally a uv-tool install with its own interpreter, so recording the
    packages of the process you are reading this from would describe typer and rich
    rather than torch and transformers.
    """
    with _client() as c:
        snap = _run_handle(c, run).snapshot(
            cwd=cwd,
            include_env=not no_env,
            include_gpu=not no_gpu,
            venv=venv,
            # The CLI is never the process that runs the code.
            detect_venv=True,
            upload=not no_upload,
            max_upload_bytes=max_upload_mb * 1024 * 1024,
            include=list(include) if include else None,
            reference_over_bytes=reference_over_mb * 1024 * 1024,
        )
    m = snap["manifest"]
    cb = snap.get("code_bytes") or {}
    g = snap.get("git")
    if g:
        print(f"snapshot {g['commit'][:12]} -> {g['ref']}")
    else:
        skipped = m.get("skipped") or []
        # A COUNT, never a path or a value -- naming it `secrets` made CodeQL
        # read the printed integer as the credential itself, and it read that
        # way to humans too.
        n_credential_shaped = sum(1 for s in skipped if s["reason"] == "secret")
        print(f"snapshot (no git repo) — every file uploaded, {len(m['entries'])} files")
        if skipped:
            note = f", {n_credential_shaped} credential-shaped" if n_credential_shaped else ""
            print(f"      skipped {len(skipped)} paths (generated{note})")
    base = m["base_commit"][:12] if m["base_commit"] else "no pushed base"
    print(f"code: {m['n_git_referenced']} referenced from {base}")
    if cb.get("uploaded"):
        print(
            f"      {cb['n_files']} uploaded ({cb['size_bytes'] / 1e6:.2f} MB, "
            f"{cb['archive_sha256'][:12]})"
        )
    elif cb.get("pending_upload"):
        print(
            f"      {cb['pending_upload']} NOT stored ({cb.get('reason')}) — "
            "this run is not reproducible from the record alone"
        )
    for e in (m.get("entries") or []):
        if e.get("source") == "reference":
            print(
                f"      referenced (too large to copy): {e['path']}  "
                f"{e['size'] / 1e6:.0f} MB on {e.get('host')}"
            )
    deps = snap.get("deps") or {}
    prov = snap.get("env_provenance") or {}
    if deps.get("packages") is not None:
        source = prov.get("venv") or prov.get("python_executable")
        print(
            f"env: {deps['package_count']} packages, python {deps['python']}"
            f" ({prov.get('resolved_via')}: {source})"
        )


@app.command("snapshot-show")
def snapshot_show(
    run: str = typer.Argument(...),
    pending_only: bool = typer.Option(False, "--pending-only"),
) -> None:
    """Print a run's captured code manifest, one file per line.

    `git` files are retrievable with `git cat-file blob <blob>` from the recorded
    remote; `blob` files need their bytes fetched from the artifact store.
    """
    with _client() as c:
        record = c.transport.get(f"/v1/execution-records/{_run_handle(c, run).data['env_ref']}")
    manifest = ((record or {}).get("code") or {}).get("manifest") or {}
    entries = manifest.get("entries") or []
    print(f"base_commit {manifest.get('base_commit')}  remote {manifest.get('remote')}")
    print(f"tree_sha256 {manifest.get('tree_sha256')}")
    for e in entries:
        if pending_only and e.get("source") != "blob":
            continue
        ref = e.get("blob", "-") if e.get("source") == "git" else "needs-upload"
        print(f"  {e.get('source'):<5} {ref:<40} {e.get('sha256', '')[:12]} {e.get('path')}")
    print(
        f"{manifest.get('n_git_referenced', 0)} referenced, "
        f"{manifest.get('n_pending_upload', 0)} pending upload"
    )


@app.command("snapshot-restore")
def snapshot_restore(
    run: str = typer.Argument(...),
    dest: str = typer.Argument(None, help="directory to rebuild into; omit with --verify-only"),
    verify_only: bool = typer.Option(
        False, "--verify-only", help="resolve and hash everything, write nothing"
    ),
) -> None:
    """Rebuild a run's captured working tree, or say exactly what is missing.

    Files git can supply are fetched from the recorded remote; the rest come from
    the uploaded `code-bytes` archive. Every file is verified against the sha256
    the manifest recorded — a mismatch is reported UNAVAILABLE and never written,
    so this cannot hand back a tree that only looks right.

    Exits non-zero if any file could not be produced.
    """
    from ..sdk.restore import restore_snapshot

    if not verify_only and not dest:
        raise typer.BadParameter("DEST is required unless --verify-only is passed")

    with _client() as c:
        handle = _run_handle(c, run)
        env_ref = handle.data.get("env_ref")
        if not env_ref:
            print(f"error: run {run} has no execution record to restore", file=sys.stderr)
            raise typer.Exit(1)
        record = c.transport.get(f"/v1/execution-records/{env_ref}")
        manifest = ((record or {}).get("code") or {}).get("manifest") or {}

        archive = None
        tmp = None
        rows = c.list_run_artifacts(handle.id, kind="code_bytes") or []
        rows = rows.get("items", rows) if isinstance(rows, dict) else rows
        if rows:
            tmp = tempfile.NamedTemporaryFile(prefix="probe-code-", suffix=".tar.gz", delete=False)
            tmp.close()
            try:
                c.download_artifact_to(rows[0]["id"], tmp.name)
                archive = tmp.name
            except Exception as exc:  # noqa: BLE001 -- reported per file below
                print(f"warning: could not download code-bytes: {exc}", file=sys.stderr)

    try:
        result = restore_snapshot(
            manifest, dest or ".", archive_path=archive, verify_only=verify_only
        )
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass

    for f in result["files"]:
        if f["status"] == "unavailable":
            print(f"  MISSING {f['path']}  ({f['reason']})")
    for r in result.get("referenced") or []:
        print(f"  OFF-PLATFORM {r['path']}  -> {r['uri']} on {r['host']}")
    verb = "verified" if verify_only else "restored"
    ref = f", {result['n_referenced']} referenced off-platform" if result.get("n_referenced") else ""
    print(f"{result['n_restored']} {verb}, {result['n_unavailable']} unavailable{ref}")
    print(f"tree_sha256 {result['tree_sha256']} matches={result['tree_matches']}")
    if result["n_unavailable"]:
        raise typer.Exit(1)


# -- outbox (the async write journal) ---------------------------------------
outbox_app = typer.Typer(no_args_is_help=True, help="the durable async write outbox")
app.add_typer(outbox_app, name="outbox")


def _drain_foreground(run_ref: str | None = None) -> None:
    """Shared body of `probe outbox drain` and its `probe flush` alias."""
    from ..sdk.journal import drain

    report = drain(_journal(), run_ref=run_ref)
    scope = f" for run {run_ref}" if run_ref else ""
    print(
        f"delivered {report.delivered}{scope}; "
        f"{report.dead_lettered} dead-lettered; {report.remaining} remaining"
    )
    if report.auth_blocked:
        typer.echo(
            f"auth-blocked: {report.errors[-1] if report.errors else '401/403'} "
            "— run `probe login`; queued items were kept",
            err=True,
        )
    elif report.stopped_transient and report.errors:
        typer.echo(f"stopped on transient failure: {report.errors[-1]}", err=True)
    elif report.errors:
        # Dead-letter-only failures: every cause still reaches stderr.
        typer.echo(f"dead-lettered: {report.errors[-1]}", err=True)
    for message in report.errors[:-1] if report.errors else []:
        typer.echo(f"  {message}", err=True)
    if not report.clean:
        raise typer.Exit(2)


@outbox_app.command("status")
def outbox_status(
    verbose: bool = typer.Option(False, "--verbose", help="list every queued/failed op"),
) -> None:
    """Outbox summary. Exit 0 when everything is delivered, 2 otherwise."""
    journal = _journal()
    pending = journal.pending()
    failed = journal.failed()
    from ..sdk.journal import Journal

    status = Journal.read_status(journal.dir) or {}
    summary = {
        "dir": str(journal.dir),
        "pending": len(pending),
        "failed": len(failed),
        "paused": journal.paused,
        "auth_blocked_since": status.get("auth_blocked_since"),
        "oldest_pending": pending[0][1].get("enqueued_at") if pending else None,
        "last_error": status.get("last_error"),
    }
    if verbose:
        def row(op: dict, state: str) -> dict:
            return {
                "op_id": op.get("op_id"),
                "state": state,
                "kind": op.get("kind"),
                "run_ref": op.get("run_ref"),
                "detail": (
                    f"{op.get('method')} {op.get('path')}"
                    if op.get("kind") == "http"
                    else (op.get("upload") or {}).get("name")
                ),
                "attempts": op.get("attempts"),
                "last_error": op.get("last_error"),
            }

        summary["ops"] = [row(op, "pending") for _, op in pending] + [
            row(op, "failed") for _, op in failed
        ]
    _print_json(summary)
    if pending or failed:
        raise typer.Exit(2)


@outbox_app.command("drain")
def outbox_drain(
    run: Optional[str] = typer.Option(None, "--run", help="drain only this run's ops"),
) -> None:
    """Deliver everything queued, in order, and wait for it (the sync barrier)."""
    _drain_foreground(run)


@outbox_app.command("watch")
def outbox_watch(
    interval: float = typer.Option(5.0, "--interval", min=0.1),
    once: bool = typer.Option(False, "--once", help="drain once and exit"),
) -> None:
    """Continuously drain the outbox in the foreground."""
    from ..sdk.journal import drain
    from ._watch import watch

    journal = _journal()

    def one_pass() -> dict:
        report = drain(journal)
        # Adapt to the shared watch contract: {"counts": {...}, "failed": [...]}.
        return {
            "counts": {"completed": report.delivered, "failed": report.dead_lettered},
            "failed": report.errors if not report.clean else [],
            "remaining": report.remaining,
            "auth_blocked": report.auth_blocked,
        }

    watch(one_pass, interval=interval, once=once, report=_print_json)


@outbox_app.command("retry")
def outbox_retry(
    op_id: Optional[str] = typer.Argument(None, help="a dead-lettered op id (omit for all)"),
) -> None:
    """Requeue dead-lettered op(s) and kick the drainer."""
    journal = _journal()
    moved = journal.retry_failed(op_id)
    # An explicit retry is a statement that the blocker (often credentials)
    # was dealt with -- forget the auth block so the drainer spawns again.
    journal.clear_auth_block()
    _kick_drainer()
    print(f"requeued {moved} op(s)")
    if op_id is not None and moved == 0:
        raise typer.Exit(1)


@outbox_app.command("discard")
def outbox_discard(
    op_id: Optional[str] = typer.Argument(
        None, help="a dead-lettered op id (omit to discard ALL dead letters)"
    ),
) -> None:
    """Tombstone dead letters into discarded/ (covers quarantined-corrupt
    files retry can never requeue). Their staged bytes are freed."""
    moved = _journal().discard_failed(op_id)
    print(f"discarded {moved} op(s)")
    if op_id is not None and moved == 0:
        raise typer.Exit(1)


@outbox_app.command("pause")
def outbox_pause() -> None:
    """Suspend background delivery (the outbox's own switch, not the capture
    killswitch). Enqueues still work; nothing drains until `resume`."""
    _journal().pause()
    print("outbox paused")


@outbox_app.command("resume")
def outbox_resume() -> None:
    """Resume background delivery and kick the drainer."""
    journal = _journal()
    journal.resume()
    journal.clear_auth_block()
    _kick_drainer()
    print("outbox resumed")


@app.command()
def flush() -> None:
    """Deliver everything queued and wait (alias of `probe outbox drain`)."""
    _drain_foreground()


@app.command()
def get(run: str = typer.Argument(...)) -> None:
    """Print a run."""
    with _client() as c:
        _print_json(c.get_run(run))


@app.command()
def bundle(run: str = typer.Argument(...)) -> None:
    """Print a run bundle (run + series + artifacts)."""
    with _client() as c:
        _print_json(c.run_bundle(run))


# -- the project's notes file -------------------------------------------------
# One markdown file per project, read and written as text. This replaced a
# structured `probe note` command group whose kind vocabulary
# (intent/decision/observation/...), supersession and authority field were never
# validated or grouped by anything server-side -- eight kinds bought one list filter.
# Prose is what people write; a fixed filename is the only thing that needed inventing.
notes_app = typer.Typer(no_args_is_help=True, help="the project's notes (free-text markdown)")
app.add_typer(notes_app, name="notes")


def _notes_project(client: Client, project: str | None) -> tuple[str, str]:
    """(project_id, label) for a notes read/write. Explicit beats the active one."""
    resolved = resolve(project=project, base_url=_conn.base_url).project
    if not resolved:
        raise typer.BadParameter(
            "pass --project, or set an active project with `probe project use <slug>`"
        )
    return _project_id(client, resolved), _project_slug(client, resolved) or resolved


@notes_app.command("show")
def notes_show(
    project: str = typer.Option(None, "--project", help="project id or slug"),
) -> None:
    """Print the project's notes. Empty output means none were ever written."""
    with _client() as c:
        project_id, label = _notes_project(c, project)
        text = c.get_project_notes(project_id)
    if text is None:
        print(f"{label} has no notes yet", file=sys.stderr)
        return
    sys.stdout.write(text if text.endswith("\n") else text + "\n")


@notes_app.command("write")
def notes_write(
    file: str = typer.Argument(None, help="file to upload; omit or '-' to read stdin"),
    project: str = typer.Option(None, "--project", help="project id or slug"),
    append: bool = typer.Option(
        False,
        "--append",
        help="append to the current text instead of replacing it",
    ),
) -> None:
    """Write the project's notes.

    Replaces by default. `--append` reads the current text first and adds to it,
    which is what you want when two agents are working the same project -- a plain
    write is last-one-wins and the other's paragraph is gone.
    """
    text = sys.stdin.read() if file in (None, "-") else Path(file).read_text()
    with _client() as c:
        project_id, label = _notes_project(c, project)
        if append:
            current = c.get_project_notes(project_id)
            if current:
                text = current.rstrip("\n") + "\n\n" + text.lstrip("\n")
        stored = c.set_project_notes(project_id, text)
    print(f"wrote notes on project {label}", file=sys.stderr)
    # The stored length, not the document. Echoing the whole thing back on stdout
    # made `notes write` unpipeable and buried the one fact a caller wants, which is
    # that the server holds what was sent -- `notes show` is how you read it.
    _print_json({"project": label, "chars": len(stored)})


@app.command()
def events(run: str = typer.Argument(...)) -> None:
    """Read the backend lifecycle events for a run (fold #10, read-only)."""
    with _client() as c:
        _print_json(c.events.for_run(run))


# -- experiment maintenance ---------------------------------------------------
experiment_app = typer.Typer(no_args_is_help=True, help="experiment maintenance")
app.add_typer(experiment_app, name="experiment")


@experiment_app.command("create")
def experiment_create(
    slug: str = typer.Argument(...),
    hypothesis: str = typer.Option(..., "--hypothesis", help="what you expect this to show"),
    name: str = typer.Option(None, "--name", help="defaults to the slug"),
    project: str = typer.Option(
        None, "--project", help="project slug; defaults to the active one (`probe project use`)"
    ),
    description: str = typer.Option(None, "--description"),
    tag: list[str] = typer.Option(None, "--tag", help="tag at creation (repeatable)"),
) -> None:
    """Create an experiment.

    The counterpart to `probe project create`. Both exist because `probe run start`
    no longer creates its parents: it used to get-or-create the whole chain, so a
    typo'd slug minted a second identity instead of erroring, and an omitted
    hypothesis minted a permanent `[auto]` placeholder.

    `--hypothesis` is required here for that reason — this is the moment you know
    what you are testing, and nothing later goes back to fill it in.
    """
    resolved_project = resolve(project=project).project
    if not resolved_project:
        raise errors.ValidationError(
            "pass --project, or set an active project with `probe project use`"
        )
    with _client() as c:
        # Same resolver as `run start`, so "no such project" is one error with
        # one exit code, and the message names the SLUG that was looked up rather
        # than the raw ambient value (which is an id).
        project_id = c.resolve_or_raise(
            "project", _project_slug(c, resolved_project)
        )["id"]
        _print_json(
            c.create_experiment(slug, name, hypothesis=hypothesis,
                project_id=project_id,
                description=description,
                tags=tag or None,
            )
        )


@experiment_app.command("set")
def experiment_set(
    experiment_id: str = typer.Argument(...),
    hypothesis: str = typer.Option(None, "--hypothesis", help="replace the hypothesis"),
    name: str = typer.Option(None, "--name"),
    description: str = typer.Option(None, "--description"),
) -> None:
    """Amend an experiment's hypothesis, name, or description after creation."""
    if hypothesis is None and name is None and description is None:
        raise typer.BadParameter("pass at least one of --hypothesis/--name/--description")
    with _client() as c:
        result = c.update_experiment(
            experiment_id, hypothesis=hypothesis, name=name, description=description
        )
    _print_json(result)


@experiment_app.command("list")
def experiment_list(
    project: str = typer.Option(None, "--project", help="project id or slug"),
    tag: list[str] = typer.Option(None, "--tag", help="filter: experiment must carry ALL (repeatable)"),
    limit: int = typer.Option(50, "--limit", min=1, max=200),
    cursor: str = typer.Option(None, "--cursor", help="keyset cursor from a previous page"),
) -> None:
    """List experiments, filterable by project and tags (AND semantics)."""
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    with _client() as c:
        page = c.list_experiments(
            project_id=_project_id(c, project) if project else None,
            tags=tag or None,
            **params,
        )
    _print_json({"items": page.items, "next_cursor": page.next_cursor})


@experiment_app.command("tag")
def experiment_tag(
    experiment_id: str = typer.Argument(...),
    add: list[str] = typer.Argument(None, help="tags to add"),
    remove: list[str] = typer.Option(None, "--remove", help="tag to remove; repeatable, ONE tag per flag (a bare word after options is an ADD)"),
    replace: list[str] = typer.Option(None, "--set", help="replace the whole list (repeatable; --set '' clears all)"),
) -> None:
    """Tag an experiment: positional args add, --remove drops, --set replaces; bare lists."""
    with _client() as c:
        _print_json(
            _tag_verb_flow(
                experiment_id,
                c.get_experiment(experiment_id).get("tags"),
                add,
                remove,
                replace,
                lambda wanted: c.update_experiment(experiment_id, tags=wanted),
            )
        )


@experiment_app.command("delete")
def experiment_delete(
    experiment_id: str = typer.Argument(...),
    yes: bool = typer.Option(False, "--yes", help="skip the confirmation prompt"),
) -> None:
    """PERMANENTLY delete an experiment and its runs. Irreversible."""
    _confirm_delete(
        yes,
        f"experiment {experiment_id}",
        cascade="every run, metric and file inside it goes too",
    )
    with _client() as c:
        c.delete_experiment(experiment_id)
    print(f"{experiment_id} deleted")


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
    except OSError as exc:
        # A bad local path is a usage error, not a crash. The upload commands take a
        # path and hash it before any request, and the anchored path is strict (no
        # fail-open spool to absorb it), so a typo would otherwise print a traceback.
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
