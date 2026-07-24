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

# Resolved once, at import (H8): `is_newer` runs AFTER the tree is replaced, and a
# deferred `import packaging` there could fail; loading it now keeps it in memory.
try:
    from packaging.version import Version as _Version  # type: ignore
except Exception:  # pragma: no cover - packaging is a transitive dep, usually present
    _Version = None

DIST = "probe-research"
LEGACY_DIST = "probe-agent"  # pre-2026-07-15 name; owns the same `probe` binary
PLUGIN_ID = "probe-research@research-os-agent"
MARKETPLACE = "research-os-agent"

_CLAUDE_TIMEOUT_S = 90.0
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
    EDITABLE = "editable"  # -e / source checkout (H5)
    MANAGED = "managed"    # pip dep in a project with a lockfile (H6)
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
    pipx_home = os.environ.get("PIPX_HOME")
    if "/pipx/venvs/" in s or (pipx_home and s.startswith(str(Path(pipx_home).resolve()).replace(os.sep, "/"))):
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
    url = base_url.rstrip("/") + "/v1/client-version"
    with httpx.Client(timeout=_HTTP_TIMEOUT_S) as client:
        resp = client.get(url, headers={"Accept": "application/json"})
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
    ok: bool
    message: str


def _run(cmd: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(cmd, timeout=timeout)  # inherit stdio: show progress
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=124)


def upgrade_cli(install: Install) -> CliResult:
    m = install.method
    if m == Method.UV_TOOL:
        r = _run(["uv", "tool", "upgrade", DIST], _UPGRADE_TIMEOUT_S)
        return _cli_result(r, "uv")
    if m == Method.UV_TOOL_LEGACY:
        # H3: the old probe-agent tool owns `probe`; `uv tool upgrade probe-research`
        # is a no-op. Uninstall the old, install the new (which relinks `probe`).
        r1 = _run(["uv", "tool", "uninstall", LEGACY_DIST], _UPGRADE_TIMEOUT_S)
        if r1 is None:
            return CliResult(False, False, "`uv` not found on PATH")
        r2 = _run(["uv", "tool", "install", "--force", DIST], _UPGRADE_TIMEOUT_S)
        return _cli_result(r2, "uv")
    if m == Method.PIPX:
        r = _run(["pipx", "upgrade", DIST], _UPGRADE_TIMEOUT_S)
        return _cli_result(r, "pipx")
    if m == Method.PIP:
        r = _run([sys.executable, "-m", "pip", "install", "-U", DIST], _UPGRADE_TIMEOUT_S)
        return _cli_result(r, "pip")
    if m == Method.EDITABLE:
        return CliResult(False, False, "editable/source install — update with git, not a package manager")
    if m == Method.MANAGED:
        return CliResult(
            False, False,
            "probe-research is a dependency of this project — bump it with your "
            "dependency manager (e.g. `uv add probe-research@latest`) so the lockfile stays in sync",
        )
    return CliResult(False, False, "could not tell how probe was installed — update via your package manager")


def _cli_result(r: subprocess.CompletedProcess | None, tool: str) -> CliResult:
    if r is None:
        return CliResult(False, False, f"`{tool}` not found on PATH")
    if r.returncode == 124:
        return CliResult(True, False, f"`{tool}` upgrade timed out")
    if r.returncode != 0:
        return CliResult(True, False, f"`{tool}` upgrade failed (exit {r.returncode})")
    return CliResult(True, True, "CLI upgraded")


@dataclass
class PluginResult:
    attempted: bool
    confirmed: bool  # plugin is at/past the target — trusted, NOT from claude's exit code
    changed: bool    # the version actually moved this run (vs already-current)
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
        return PluginResult(False, False, False, None, None, "`claude` not found on PATH (skipping plugin update)")

    before = installed_plugin_version()
    completed = True
    for args in (["plugin", "marketplace", "update", MARKETPLACE], ["plugin", "update", PLUGIN_ID]):
        try:
            r = subprocess.run(
                [claude, *args],
                stdin=subprocess.DEVNULL,          # no TTY -> no raw-mode on the parent terminal
                capture_output=True, text=True,
                timeout=_CLAUDE_TIMEOUT_S,
            )
            if r.returncode != 0:
                completed = False
                break
        except (subprocess.TimeoutExpired, OSError):
            completed = False
            break

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
            True, False, False, before, after,
            "`claude` returned success but the plugin version did not advance "
            "(it may have run inside a Claude Code session, which no-ops)",
        )
    return PluginResult(True, False, False, before, after, "`claude plugin update` did not complete")


def manual_plugin_commands() -> str:
    return (
        f"claude plugin marketplace update {MARKETPLACE}\n"
        f"claude plugin update {PLUGIN_ID}"
    )
