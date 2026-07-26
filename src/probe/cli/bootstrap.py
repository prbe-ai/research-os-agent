"""Make sure the wizard leaves a real `probe` behind.

`npx probe-research` resolves the CLI through `uv tool run` / `pipx run`, both
of which are EPHEMERAL: they fetch, execute, and leave nothing installed. That
is the right way to *launch* a wizard and completely wrong as an end state,
because everything the wizard sets up depends on a persistent binary afterwards:

  * the wizard's own closing line tells you to run `probe doctor`
  * the tracking plugin's SessionStart hook resolves PROBE_BIN from PATH or
    ~/.local/bin to do its version check
  * the MCP config's headersHelper shells out to fetch the stored token, so
    with no binary the MCP has no credential

Without this the only path that worked end to end was the one where `probe` was
already installed — the re-run case, not onboarding. Exactly backwards.

So: if we are running ephemerally, install ourselves properly before doing
anything else, and say so.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

DIST = "probe-research"
LEGACY_DIST = "probe-agent"
_INSTALL_TIMEOUT_S = 300.0


@dataclass(frozen=True)
class BootstrapResult:
    installed: bool
    """True only when THIS run performed a persistent install."""
    already_persistent: bool
    message: str


def _installed_binary() -> str | None:
    """A `probe` a future shell (or a plugin hook) could actually find.

    PATH alone is not enough: Claude Code launched from the dock sources no
    profile, so ~/.local/bin may be missing from its environment even though the
    binary is there. The plugin hook checks those same fallbacks, so we must
    agree with it or we would reinstall on every run.
    """
    found = shutil.which("probe")
    if found:
        return found
    home = os.path.expanduser("~")
    for candidate in (
        f"{home}/.local/bin/probe",
        f"{home}/.local/share/uv/tools/{DIST}/bin/probe",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _version_of(binary: str) -> str | None:
    try:
        completed = subprocess.run(  # noqa: S603 - resolved path, no shell
            [binary, "--version"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    import re

    match = re.search(r"(\d+\.\d+\.\d+(?:\.\d+)?)", completed.stdout)
    return match.group(1) if match else None


def _at_least(found: str, wanted: str) -> bool:
    fa = [int(x) for x in found.split(".") if x.isdigit()]
    wa = [int(x) for x in wanted.split(".") if x.isdigit()]
    for i in range(max(len(fa), len(wa))):
        a, b = (fa[i] if i < len(fa) else 0), (wa[i] if i < len(wa) else 0)
        if a != b:
            return a > b
    return True


def _resolves_on_path() -> bool:
    """Whether a GOOD ENOUGH `probe` is installed.

    Existence is not enough. A machine with an old `probe` -- every existing
    user -- would otherwise keep it forever: the wizard runs the new version
    ephemerally through uvx, decides nothing needs installing, and the user's
    shell stays on the old one indefinitely. Same trap the npx launcher fell
    into.
    """
    from probe import __version__

    binary = _installed_binary()
    if binary is None:
        return False
    found = _version_of(binary)
    return bool(found and _at_least(found, __version__))


def ensure_persistent_install(*, dry_run: bool = False) -> BootstrapResult:
    """Install the CLI for real if this process is an ephemeral one.

    Prefers uv, falls back to pipx. Deliberately never falls back to a bare
    `pip install`: on a researcher's machine that usually means a conda or
    system environment, and silently mutating it is how you break a training
    run three days later.
    """
    if _resolves_on_path():
        return BootstrapResult(
            installed=False,
            already_persistent=True,
            message="",
        )

    if dry_run:
        return BootstrapResult(
            installed=False,
            already_persistent=False,
            message=f"would install {DIST} persistently",
        )

    if shutil.which("uv"):
        # The legacy distribution owns the same `probe` executable, so it has to
        # go FIRST or the old build keeps answering. Removing it afterwards
        # deletes the shared binary out from under the new install.
        subprocess.run(  # noqa: S603 - fixed binary, no shell
            ["uv", "tool", "uninstall", LEGACY_DIST],
            capture_output=True,
            check=False,
            timeout=_INSTALL_TIMEOUT_S,
        )
        command = ["uv", "tool", "install", "--force", DIST]
    elif shutil.which("pipx"):
        command = ["pipx", "install", "--force", DIST]
    else:
        return BootstrapResult(
            installed=False,
            already_persistent=False,
            message=(
                "could not install `probe` persistently: neither uv nor pipx is "
                "available. The wizard will still finish, but `probe doctor` and "
                "the plugin's version check will not work until you install it."
            ),
        )

    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, no shell
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_INSTALL_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return BootstrapResult(False, False, f"could not install `probe`: {exc}")

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        return BootstrapResult(
            installed=False,
            already_persistent=False,
            message=f"could not install `probe`: {detail[-1] if detail else 'unknown error'}",
        )

    note = ""
    if not shutil.which("probe"):
        # Installed, but this shell will not see it. Saying so beats letting the
        # user discover it when `probe doctor` fails.
        note = " Add ~/.local/bin to your PATH to use it in this shell."
    return BootstrapResult(
        installed=True,
        already_persistent=False,
        message=f"Installed `probe` ({' '.join(command[:2])}).{note}",
    )
