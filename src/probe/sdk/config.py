"""SDK credential + endpoint resolution.

Precedence (highest first): explicit argument -> environment -> config file.

Env vars:
  PROBE_BASE_URL      e.g. https://api.research.prbe.ai
  PROBE_TOKEN         a user API token (probe_pat_...) for /v1
  PROBE_MCP_TOKEN     a read-only token for the MCP surface (see mcp_token below)
  PROBE_INGEST_TOKEN  an ingest token (ros_ing_...) for /ingest
  PROBE_HMAC_SECRET   optional shared secret for the X-Signature body HMAC on /ingest
  PROBE_WORKSPACE     the active workspace id, overriding the context file
  PROBE_PROJECT       the active project id/slug, overriding the context file
  PROBE_HEARTBEAT_SECONDS  run auto-heartbeat interval (default 60; <=0 disables —
                      see Run.start_heartbeat; env only, not read from this file)

Config file: $XDG_CONFIG_HOME/probe/config.json (default ~/.config/probe/config.json),
written by ``probe login``. ``probe login --device`` captures the token via the browser
handoff; ``probe login --token`` is the air-gap-friendly paste path.

``mcp_token`` is deliberately a separate credential from ``token``: the MCP surface is
read-only, so it holds a ``scopes:['read']`` token that cannot write even if it leaks
(it is handed to an MCP client, which is a wider blast radius than the CLI). Nothing
falls back from one to the other — ``probe mcp token set`` writes it.

Shape (v2)
----------
The file holds *named contexts*, kubectl-style, so one machine can address several
endpoints or tenants without re-running ``login``::

    {
      "version": 2,
      "current_context": "default",
      "contexts": {
        "default": {
          "base_url": "...", "token": "...", "mcp_token": "...",
          "workspace": {"id": "<uuid>", "project": "<uuid-or-slug>"}
        }
      }
    }

**The active project nests *inside* ``workspace`` rather than sitting beside it.** That
makes "a project from workspace A while workspace B is active" unrepresentable instead of
merely invalid: ``workspace use`` replaces the whole object, so a project cannot outlive
the workspace it belongs to. No validation code, because the bad state has no encoding.
Two workspace+project pairs at once? That is what a second *context* is for.

v1 (a flat ``{base_url, token, ...}``) is migrated **in memory on read**. Reads never
write the file back: a read-only command must not rewrite a config that may be symlinked
into a dotfiles repo (``save_file`` resolves symlinks for the same reason). The file is
rewritten in v2 the next time something genuinely saves.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

DEFAULT_BASE_URL = "https://api.research.prbe.ai"

CONFIG_VERSION = 2
DEFAULT_CONTEXT = "default"

# Per-context credential/endpoint keys. Used to migrate a v1 file and to decide what
# `clear_context` strips — anything outside this set is somebody else's key and is left
# alone rather than silently dropped on the floor.
_CONTEXT_KEYS = (
    "base_url",
    "token",
    "mcp_token",
    "ingest_token",
    "hmac_secret",
    "workspace",
)


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "probe" / "config.json"


def _migrate(data: dict) -> dict:
    """Return ``data`` in v2 shape. Pure — never touches disk."""
    if not data:
        return {}
    if isinstance(data.get("contexts"), dict):
        data.setdefault("version", CONFIG_VERSION)
        data.setdefault("current_context", DEFAULT_CONTEXT)
        return data
    # v1: a flat credential blob. Everything in it belongs to one context. Carry
    # unrecognized keys across too — a key we do not know about is more likely a newer
    # client's than junk, and dropping credentials on read would be unrecoverable.
    return {
        "version": CONFIG_VERSION,
        "current_context": DEFAULT_CONTEXT,
        "contexts": {DEFAULT_CONTEXT: dict(data)},
    }


class ConfigUnreadable(Exception):
    """The config file exists but could not be parsed.

    Distinct from "no config yet" on purpose: the two look identical to a reader but
    must never look identical to a WRITER. See :func:`load_file`.
    """


def load_file(*, strict: bool = False) -> dict:
    """The raw config file, migrated to v2 in memory. ``{}`` when absent.

    Readers get ``{}`` for an unreadable file too — degrading to "unconfigured" is
    the right call for a read, and ``mcp/server.py`` calls ``.get()`` on this
    unguarded. Writers must pass ``strict=True``: they read-modify-write the whole
    file, so treating a corrupt file as empty would REPLACE every stored context
    with whatever is being saved. One truncated byte would otherwise take every
    token for every endpoint on the machine, and exit 0 doing it.
    """
    path = config_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        if strict:
            raise ConfigUnreadable(
                f"{path} exists but could not be read ({exc}). Refusing to overwrite it "
                "— move it aside if you meant to start fresh."
            ) from exc
        return {}
    if not isinstance(data, dict):
        if strict:
            raise ConfigUnreadable(
                f"{path} is not a JSON object. Refusing to overwrite it — move it aside "
                "if you meant to start fresh."
            )
        return {}
    return _migrate(data)


def current_context_name(data: dict | None = None) -> str:
    data = load_file() if data is None else data
    return data.get("current_context") or DEFAULT_CONTEXT


def load_context(name: str | None = None) -> dict:
    """The flat credential dict for one context. ``{}`` when it does not exist.

    This is what callers that used to read ``load_file()`` for a credential want;
    ``load_file()`` now means "the whole file, every context".
    """
    data = load_file()
    contexts = data.get("contexts")
    if not isinstance(contexts, dict):
        return {}
    ctx = contexts.get(name or current_context_name(data))
    return ctx if isinstance(ctx, dict) else {}


@contextmanager
def _config_lock():
    """Serialize read-modify-write of the config across processes.

    ``os.replace`` gives atomicity, not isolation: two ``probe`` processes can both
    read the file, each add their own context, and the second write silently drops
    the first — while both report success. That is reachable from ordinary use (a CI
    matrix, two worktree sessions, `project use` racing an auto-login), and losing a
    context means losing its token.

    An O_EXCL lockfile, best-effort: a stale lock from a killed process must never
    brick the CLI, so an old one is broken rather than waited on forever.
    """
    lock = config_path().with_suffix(".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    acquired = False
    for _ in range(50):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 30:
                    lock.unlink(missing_ok=True)  # stale: holder died
                    continue
            except OSError:
                pass
            time.sleep(0.1)
    try:
        yield
    finally:
        if acquired:
            lock.unlink(missing_ok=True)


def save_file(data: dict) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # Write a complete file then swap it in: a crash mid-write would otherwise leave
    # truncated JSON, which load_file() reads as {} — silently losing every credential.
    # Follow a symlink first: os.replace would swap the *link* for a regular file, so a
    # config symlinked into a dotfiles repo would silently stop tracking. The temp file
    # is created 0600 and must share the target's directory for os.replace to be atomic.
    target = path.resolve() if path.is_symlink() else path
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".config-", suffix=".json")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    # tokens live here; keep it user-only.
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def save_context(updates: dict, *, name: str | None = None) -> Path:
    """Merge ``updates`` into one context and persist the whole file.

    Read-modify-write of the *file*, so saving one context never drops another. A key
    set to None is removed, which is how a credential gets cleared without clearing
    its neighbours.
    """
    with _config_lock():
        data = load_file(strict=True) or {
            "version": CONFIG_VERSION,
            "current_context": DEFAULT_CONTEXT,
            "contexts": {},
        }
        data.setdefault("contexts", {})
        target = name or current_context_name(data)
        ctx = dict(data["contexts"].get(target) or {})
        for key, value in updates.items():
            if value is None:
                ctx.pop(key, None)
            else:
                ctx[key] = value
        data["contexts"][target] = ctx
        data["current_context"] = data.get("current_context") or target
        data["version"] = CONFIG_VERSION
        return save_file(data)


def use_context(name: str) -> Path:
    """Make ``name`` active, creating it empty if it does not exist yet."""
    with _config_lock():
        data = load_file(strict=True) or {"version": CONFIG_VERSION, "contexts": {}}
        data.setdefault("contexts", {}).setdefault(name, {})
        data["current_context"] = name
        data["version"] = CONFIG_VERSION
        return save_file(data)


def delete_context(name: str) -> Path:
    """Drop a context. Clearing the active one leaves ``current_context`` dangling,
    so fall back to whatever remains (or the default name) to keep the file coherent."""
    with _config_lock():
        data = load_file(strict=True)
        contexts = data.get("contexts") or {}
        contexts.pop(name, None)
        data["contexts"] = contexts
        if data.get("current_context") == name:
            data["current_context"] = next(iter(contexts), DEFAULT_CONTEXT)
        return save_file(data)


def clear_context(name: str | None = None) -> Path:
    """Strip credentials from one context, leaving other contexts untouched.

    ``probe logout`` used to delete the whole file. With named contexts that is wrong:
    logging out of staging would silently sign you out of prod too.
    """
    with _config_lock():
        data = load_file(strict=True)
        contexts = data.get("contexts")
        if not isinstance(contexts, dict):
            return config_path()
        target = name or current_context_name(data)
        if target in contexts:
            # Wipe, do not subtract known keys. Removing only what `_CONTEXT_KEYS` lists
            # would fail OPEN: `_migrate` deliberately carries unrecognized keys across
            # (they are more likely a newer client's than junk), so a credential this
            # version has never heard of would survive every logout.
            contexts[target] = {}
        data["contexts"] = contexts
        # Strip stray TOP-LEVEL credentials too. `_migrate` returns a v2 file untouched,
        # so a hybrid written by an OLDER probe (which read v2, saw no top-level keys,
        # and wrote `token` at the root) keeps that key forever — and it would outlive
        # every logout while still authenticating an old client.
        for key in _CONTEXT_KEYS:
            data.pop(key, None)
        return save_file(data)


def clear_file() -> None:
    path = config_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass


@dataclass
class Settings:
    base_url: str
    token: str | None = None
    mcp_token: str | None = None
    ingest_token: str | None = None
    hmac_secret: str | None = None
    workspace: str | None = None
    project: str | None = None


def resolve(
    *,
    base_url: str | None = None,
    token: str | None = None,
    mcp_token: str | None = None,
    ingest_token: str | None = None,
    hmac_secret: str | None = None,
    workspace: str | None = None,
    project: str | None = None,
    context: str | None = None,
) -> Settings:
    """Merge explicit args, env, and the config file into one Settings object."""
    file = load_context(context)
    anchor = file.get("workspace") if isinstance(file.get("workspace"), dict) else {}
    return Settings(
        # PROBE_BASE_URL outranks the context file and must keep doing so: the hosted
        # MCP pods set it (deploy/mcp/k8s.yaml) to the in-cluster service, and a context
        # that could outrank it would point production at the wrong API — while /healthz
        # still returned 200. tests/test_config_contexts.py guards this ordering.
        base_url=(
            base_url
            or os.environ.get("PROBE_BASE_URL")
            or file.get("base_url")
            or DEFAULT_BASE_URL
        ).rstrip("/"),
        token=token or os.environ.get("PROBE_TOKEN") or file.get("token"),
        # Env first keeps every shell that already exports PROBE_MCP_TOKEN working
        # unchanged. Never falls back to `token`: that one can write.
        mcp_token=mcp_token or os.environ.get("PROBE_MCP_TOKEN") or file.get("mcp_token"),
        ingest_token=(
            ingest_token or os.environ.get("PROBE_INGEST_TOKEN") or file.get("ingest_token")
        ),
        hmac_secret=(
            hmac_secret or os.environ.get("PROBE_HMAC_SECRET") or file.get("hmac_secret")
        ),
        # Ambient anchors are a convenience, never a requirement: an explicit flag or an
        # env var always wins, so scripts and CI never depend on a developer's context.
        workspace=workspace or os.environ.get("PROBE_WORKSPACE") or anchor.get("id"),
        project=project or os.environ.get("PROBE_PROJECT") or anchor.get("project"),
    )
