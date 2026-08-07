"""Internals for `probe update`: install detection, CLI upgrade, plugin update.

Kept in its own module, imported at the TOP of cli/main.py, so the whole call
graph is loaded before `probe update` spawns the CLI upgrade (H8). `uv tool
upgrade` replaces the installed tree, and Python does not hold deferred `.py`
imports open — so anything imported lazily AFTER the upgrade would
`ModuleNotFoundError`. Everything used here is imported at module top.

Detection is by the RUNNING interpreter's own `probe` package path (resolved
through symlinks), never `which probe` (shadowing) and never the CWD (a lockfile
in the current directory says nothing about how `probe` itself was installed).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from probe.cli import claude_cli, plugin_cli
from probe.cli.capabilities import TAP_PLUGIN_ID

# Resolved once, at import (H8): `is_newer` runs AFTER the tree is replaced, and a
# deferred `import packaging` there could fail; loading it now keeps it in memory.
try:
    from packaging.version import Version as _Version  # type: ignore
except Exception:  # pragma: no cover - packaging is a transitive dep, usually present
    _Version = None

DIST = "probe-research"
LEGACY_DIST = "probe-agent"  # pre-2026-07-15 name; owns the same `probe` binary
PLUGIN_ID = "probe-research@research-os-agent"
# Imported, NOT redefined: capabilities.py already owns this identity and
# actions.py/capture.py use it from there. A second literal here would be a
# second source of truth for the same plugin — the exact thing that lets one
# copy drift after a rename. (The PLUGIN_ID literal above predates this and
# duplicates capabilities.PLUGIN_ID; left alone rather than silently widened.)
#
# The tap ships from the same marketplace as a SEPARATE plugin, so
# `claude plugin update probe-research@…` never touched it. Leaving it out made
# `probe update` a command that reports success while the component whose
# staleness is invisible (a stale tap captures nothing and says nothing) stays
# behind. Updated alongside, and NOT fatal when it fails: `probe capture off
# --uninstall` removes the tap while leaving the CLI and plugin in place, so
# "not installed" is a SUPPORTED state that `claude plugin update` reports as
# an error.
MARKETPLACE = "research-os-agent"

_UPGRADE_TIMEOUT_S = 300.0
_HTTP_TIMEOUT_S = 5.0

# `probe update --check` exit codes, distinct from main()'s 1=RosError / 2=usage.
# Scripts gate on BEHIND explicitly (`probe update --check; [ $? -eq 10 ] && …`), NOT
# `|| probe update` — a network error is 1 (nonzero) but must not read as "behind".
CHECK_CURRENT = 0
CHECK_BEHIND = 10
CHECK_ERROR = 1


# -- install detection ------------------------------------------------------
class Method:
    UV_TOOL = "uv-tool"
    UV_TOOL_LEGACY = "uv-tool-legacy"  # installed under the old probe-agent name (H3)
    PIPX = "pipx"
    PIP = "pip"
    EPHEMERAL = "ephemeral"  # uvx / pipx run cache -- nothing to upgrade, no pip
    EDITABLE = "editable"  # -e / source checkout (H5)
    MANAGED = "managed"  # pip dep in a project with a lockfile (H6)
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Install:
    method: str
    root: Path | None = None
    detail: str = ""


def _probe_pkg_dir() -> Path:
    """The directory the RUNNING `probe` package is imported from, symlinks resolved."""
    import probe  # already loaded; this is the running interpreter's copy

    return Path(probe.__file__).resolve().parent


def _venv_root(site_packages_pkg: Path) -> Path | None:
    """Walk up from .../site-packages/probe to the environment root.

    Handles both the Unix ``<venv>/lib/pythonX.Y/site-packages`` layout and the
    Windows ``<venv>/Lib/site-packages`` layout (one level shallower).
    """
    for parent in site_packages_pkg.parents:
        if parent.name in ("site-packages", "dist-packages"):
            # parent == .../site-packages. Its parent is `lib` directly on Windows
            # (`Lib/site-packages`, 2 up), or `pythonX.Y` on Unix (3 up).
            if parent.parent.name.lower() == "lib":
                return parent.parent.parent
            return parent.parent.parent.parent
    return None


_MANAGED_LOCKFILES = ("uv.lock", "poetry.lock", "Pipfile.lock", "pdm.lock")
# Poetry / Pipenv / PDM keep venvs OUTSIDE the project by default, so their lockfile
# is never adjacent to the venv; recognize their well-known cache dirs instead.
_MANAGED_VENV_MARKERS = ("/pypoetry/virtualenvs/", "/virtualenvs/", "/pdm/venvs/")


def _is_editable(pkg_dir: Path) -> bool:
    """True if `probe` is imported from a source checkout rather than an install."""
    s = str(pkg_dir).replace(os.sep, "/")
    if "/site-packages/" not in s and "/dist-packages/" not in s:
        return True  # imported straight from a source tree
    # editable installs can still land a finder in site-packages; look for markers
    for parent in pkg_dir.parents:
        if parent.name in ("site-packages", "dist-packages"):
            if list(parent.glob("__editable__.probe_research*")) or list(
                parent.glob("probe*.egg-link")
            ):
                return True
            break
    return False


def _is_managed_project(venv_root: Path | None) -> bool:
    """True if this venv belongs to a lockfile-managed project, where `pip install -U`
    would desync the lockfile (H6).

    Checked from the VENV, never the CWD. Covers uv's in-project ``.venv/`` (lockfile
    at venv.parent) AND out-of-project poetry/pipenv/pdm venvs (their lockfile is not
    adjacent to the venv, so they're matched by their cache dirs / active-env signals).
    """
    if venv_root is None:
        return False
    marker = str(venv_root).replace(os.sep, "/") + "/"
    if any(m in marker for m in _MANAGED_VENV_MARKERS):
        return True
    project = venv_root.parent
    if any((project / name).exists() for name in _MANAGED_LOCKFILES):
        return True
    return bool(os.environ.get("POETRY_ACTIVE") or os.environ.get("PIPENV_ACTIVE"))


def detect_install() -> Install:
    try:
        pkg = _probe_pkg_dir()
    except Exception:
        return Install(Method.UNKNOWN)
    s = str(pkg).replace(os.sep, "/")

    if "/uv/tools/probe-research/" in s:
        return Install(Method.UV_TOOL)
    if "/uv/tools/probe-agent/" in s:
        return Install(Method.UV_TOOL_LEGACY, detail="installed under the legacy probe-agent name")
    # `npx probe-research` runs us through `uv tool run`, whose environment
    # lives in the uv CACHE and has no pip. Without this it falls through to
    # Method.PIP and the upgrade dies with "No module named pip" -- while there
    # is nothing to upgrade anyway, because the env is thrown away on exit.
    if "/uv/archive-v0/" in s or "/.cache/uv/" in s or "/pipx/.cache/" in s:
        return Install(
            Method.EPHEMERAL,
            detail="running from a temporary uvx/pipx environment",
        )
    pipx_home = os.environ.get("PIPX_HOME")
    if "/pipx/venvs/" in s or (
        pipx_home and s.startswith(str(Path(pipx_home).resolve()).replace(os.sep, "/"))
    ):
        return Install(Method.PIPX)
    if _is_editable(pkg):
        return Install(Method.EDITABLE, root=pkg.parent, detail="editable / source checkout")

    venv = _venv_root(pkg)
    if _is_managed_project(venv):
        return Install(Method.MANAGED, root=venv, detail="project dependency (lockfile present)")
    if venv is not None:
        return Install(Method.PIP, root=venv)
    return Install(Method.UNKNOWN)


# -- version compare + manifest ---------------------------------------------
def _triplet(v: str):
    if not v:
        return None
    v = str(v).strip().split()[-1]
    for sep in ("+", "-"):
        v = v.split(sep, 1)[0]
    try:
        nums = [int(p) for p in v.split(".")]
    except ValueError:
        return None
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums[:3])


def is_newer(candidate: str | None, base: str | None) -> bool:
    """True iff `candidate` is strictly newer than `base`."""
    if not candidate or not base:
        return False
    if _Version is not None:
        try:
            return _Version(str(candidate)) > _Version(str(base))
        except Exception:
            pass
    c, b = _triplet(candidate), _triplet(base)
    return bool(c and b and c > b)


def fetch_latest(base_url: str) -> dict:
    """GET the public client-version manifest. Raises on network/HTTP error."""
    with httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=_HTTP_TIMEOUT_S,
    ) as client:
        resp = client.get(
            "/v1/client-version",
            headers={"Accept": "application/json"},
        )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise ValueError("manifest is not a JSON object")
    return data


def _latest(manifest: dict, key: str) -> str | None:
    info = manifest.get(key)
    return info.get("latest") if isinstance(info, dict) else None


def cli_latest(manifest: dict) -> str | None:
    return _latest(manifest, "cli")


def plugin_latest(manifest: dict) -> str | None:
    return _latest(manifest, "plugin")


def cli_update_available(manifest: dict, current: str) -> str | None:
    latest = _latest(manifest, "cli")
    return latest if is_newer(latest, current) else None


# -- plugin (installed) version, for the H1 post-condition ------------------
def installed_plugin_version() -> str | None:
    path = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(path.read_text())
        entries = (data.get("plugins") or {}).get(PLUGIN_ID) or []
        best: str | None = None
        for entry in entries:
            v = entry.get("version") if isinstance(entry, dict) else None
            if v and (best is None or is_newer(v, best)):
                best = v
        return best
    except Exception:
        return None


# -- upgrade actions --------------------------------------------------------
@dataclass
class CliResult:
    ran: bool
    ok: bool  # the CLI is at (or past) the target after the attempt — VERIFIED
    changed: bool  # the installed version actually moved
    before: str | None
    after: str | None
    message: str


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, timeout=timeout)  # inherit stdio: show progress
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=124)


def _installed_cli_version() -> str | None:
    """Version of the `probe` on PATH, read via a FRESH subprocess so it reflects a
    just-upgraded binary — the running process keeps its own stale __version__."""
    probe_bin = shutil.which("probe") or "probe"
    try:
        r = subprocess.run([probe_bin, "--version"], capture_output=True, text=True, timeout=15)
        if r.returncode == 0 and (r.stdout or "").strip():
            return r.stdout.strip().split()[-1]  # "probe 0.8.2" -> "0.8.2"
    except Exception:
        return None
    return None


def _finalize(before: str | None, target: str | None, tool: str) -> CliResult:
    """Post-condition — the CLI's H1: trust the observed version, not the upgrade's
    exit code. A no-op upgrade (e.g. a ``uv tool install ==X`` version pin) exits 0
    while changing nothing; only re-reading the version catches that."""
    after = _installed_cli_version()
    if is_newer(after, before):
        return CliResult(True, True, True, before, after, f"CLI upgraded {before} → {after}")
    if target and after and not is_newer(target, after):
        return CliResult(True, True, False, before, after, f"CLI already at the latest ({after})")
    return CliResult(
        True,
        False,
        False,
        before,
        after,
        f"`{tool}` reported success but the CLI is still {after or before} "
        "(it may be version-pinned, or that release was yanked)",
    )


def upgrade_cli(install: Install, current: str | None, target: str | None) -> CliResult:
    m = install.method
    if m == Method.EDITABLE:
        return CliResult(
            False,
            False,
            False,
            current,
            current,
            "editable/source install — update with git, not a package manager",
        )
    if m == Method.MANAGED:
        return CliResult(
            False,
            False,
            False,
            current,
            current,
            "probe-research is a dependency of this project — bump it with your "
            "dependency manager (e.g. `uv add probe-research@latest`) so the lockfile stays in sync",
        )
    if m == Method.UNKNOWN:
        return CliResult(
            False,
            False,
            False,
            current,
            current,
            "could not tell how probe was installed — update via your package manager",
        )
    if m == Method.UV_TOOL_LEGACY:
        # H3: the old probe-agent tool owns `probe`; uninstall it, install the new.
        if _run(["uv", "tool", "uninstall", LEGACY_DIST], _UPGRADE_TIMEOUT_S) is None:
            return CliResult(False, False, False, current, current, "`uv` not found on PATH")
        _run(["uv", "tool", "install", "--force", DIST], _UPGRADE_TIMEOUT_S)
        return _finalize(current, target, "uv")
    if m == Method.PIPX:
        if _run(["pipx", "upgrade", DIST], _UPGRADE_TIMEOUT_S) is None:
            return CliResult(False, False, False, current, current, "`pipx` not found on PATH")
        return _finalize(current, target, "pipx")
    if m == Method.PIP:
        _run([sys.executable, "-m", "pip", "install", "-U", DIST], _UPGRADE_TIMEOUT_S)
        return _finalize(current, target, "pip")

    # Method.UV_TOOL
    if _run(["uv", "tool", "upgrade", DIST], _UPGRADE_TIMEOUT_S) is None:
        return CliResult(False, False, False, current, current, "`uv` not found on PATH")
    res = _finalize(current, target, "uv")
    if res.ok:
        return res
    # `uv tool upgrade` no-ops on a version-pinned install (exits 0, changes nothing);
    # force a clean reinstall of the latest, then re-verify.
    _run(["uv", "tool", "install", "--force", f"{DIST}@latest"], _UPGRADE_TIMEOUT_S)
    return _finalize(current, target, "uv")


@dataclass
class PluginResult:
    attempted: bool
    confirmed: bool  # plugin is at/past the target — trusted, NOT from claude's exit code
    changed: bool  # the version actually moved this run (vs already-current)
    before: str | None
    after: str | None
    message: str


def update_plugin(target_latest: str | None) -> PluginResult:
    """Update the Claude Code plugin via `claude`, then VERIFY it actually advanced (H1).

    The child is spawned with no TTY and captured output (H2) so a raw-mode TUI
    crash writes to a pipe, never the parent terminal. A zero exit is NOT trusted;
    we confirm by re-reading the installed plugin version.
    """
    claude = shutil.which("claude")
    if not claude:
        return PluginResult(
            False, False, False, None, None, "`claude` not found on PATH (skipping plugin update)"
        )

    before = installed_plugin_version()
    completed = True
    for args in (["plugin", "marketplace", "update", MARKETPLACE], ["plugin", "update", PLUGIN_ID]):
        # claude_cli carries the DEVNULL this loop already knew it needed --
        # no TTY -> no raw-mode on the parent terminal. It is now the only
        # copy of that rule, rather than the one place out of four with it.
        if not claude_cli.run(args, timeout=claude_cli.CAPTURE_TIMEOUT_S).ok:
            completed = False
            break

    # The tap, best-effort and deliberately OUTSIDE `completed`: it is a
    # separate plugin that many users do not have, so `claude` failing here
    # means "not installed" far more often than "update broken". Letting that
    # flip `completed` would report the whole update as failed to everyone
    # without a tap. The marketplace refresh above already ran, so this is just
    # the install step.
    claude_cli.run(["plugin", "update", TAP_PLUGIN_ID], timeout=claude_cli.CAPTURE_TIMEOUT_S)

    after = installed_plugin_version()
    changed = is_newer(after, before)
    # H1: trust the observed version, not claude's exit code. "Confirmed" = the plugin
    # is at (or past) the target, or it strictly advanced this run.
    at_target = bool(target_latest and after and not is_newer(target_latest, after))
    if at_target or changed:
        msg = f"plugin updated to {after}" if changed else f"plugin already at the latest ({after})"
        return PluginResult(True, True, changed, before, after, msg)
    if completed:
        return PluginResult(
            True,
            False,
            False,
            before,
            after,
            "`claude` returned success but the plugin version did not advance "
            "(it may have run inside a Claude Code session, which no-ops)",
        )
    return PluginResult(
        True, False, False, before, after, "`claude plugin update` did not complete"
    )


def _codex_plugin_versions(_codex: str | None = None) -> dict[str, str]:
    result = plugin_cli.list_plugins(plugin_cli.CODEX)
    if not result.ok:
        return {}
    try:
        body = json.loads(result.detail)
    except ValueError:
        return {}
    return {
        row["name"]: row["version"]
        for row in body.get("installed") or []
        if isinstance(row, dict)
        and isinstance(row.get("name"), str)
        and isinstance(row.get("version"), str)
    }


def update_codex_plugins() -> PluginResult:
    """Refresh and re-add both Codex plugins, then verify the installed list.

    Codex has no separate ``plugin update`` command. Re-adding an installed
    plugin is its idempotent reinstall path and moves it to the refreshed
    marketplace snapshot without first removing the working copy.
    """
    codex = shutil.which("codex")
    if not codex:
        return PluginResult(
            False, False, False, None, None, "`codex` not found on PATH (skipping plugin update)"
        )
    names = ("probe-research", "probe-research-tap")
    before_versions = _codex_plugin_versions(codex)
    completed = plugin_cli.refresh_marketplace(plugin_cli.CODEX, MARKETPLACE).ok
    for name in names:
        if not completed:
            break
        completed = plugin_cli.install(plugin_cli.CODEX, f"{name}@{MARKETPLACE}").ok
    after_versions = _codex_plugin_versions(codex)
    confirmed = completed and all(after_versions.get(name) for name in names)
    changed = confirmed and any(
        before_versions.get(name) != after_versions.get(name) for name in names
    )
    before = (
        ", ".join(f"{name}={version}" for name, version in sorted(before_versions.items())) or None
    )
    after = ", ".join(f"{name}={after_versions.get(name)}" for name in names) if confirmed else None
    if confirmed:
        message = f"Codex plugins {'updated' if changed else 'verified'} ({after})"
        return PluginResult(True, True, changed, before, after, message)
    return PluginResult(
        True,
        False,
        False,
        before,
        after,
        "Codex marketplace refresh or plugin reinstall did not complete",
    )


def manual_plugin_commands() -> str:
    return (
        f"claude plugin marketplace update {MARKETPLACE}\n"
        f"claude plugin update {PLUGIN_ID}\n"
        f"claude plugin update {TAP_PLUGIN_ID}  # skip if you do not have the tap"
    )


def manual_codex_plugin_commands() -> str:
    return (
        f"codex plugin marketplace upgrade {MARKETPLACE}\n"
        f"codex plugin add probe-research@{MARKETPLACE}\n"
        f"codex plugin add probe-research-tap@{MARKETPLACE}"
    )
