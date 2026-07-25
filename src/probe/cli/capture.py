"""Turning session capture OFF, as a verified postcondition.

"Off" is not "delete the paired token file". Two independent things keep capture
alive after a naive revoke:

1. THE CREDENTIAL. The uploader resolves its token from three places -- the
   paired `.token`, `PROBE_INGEST_TOKEN`, and the probe CLI config's
   `ingest_token` (which `probe login --ingest-token` writes). Clearing only the
   first lets capture silently resume at the next session start while the menu
   reports it as off.

2. THE PROCESS. The uploader is a detached daemon spawned at session start. It
   already holds its bearer in memory and has a queue to drain, so deleting
   files does not stop it.

For a feature whose entire justification is honest consent, "we told you it was
off and it wasn't" is the worst available bug. So off is defined as a
postcondition and VERIFIED before it is reported.

The wizard offers two shapes of off, mirroring a distinction the product already
draws (the pairing modal separates `tap revoke`, which keeps the plugin, from
`/plugin uninstall`). Both run the full teardown; UNINSTALL additionally removes
the plugin.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum

from probe.cli.capabilities import (
    ENV_INGEST_TOKEN,
    MARKETPLACE,
    TAP_PLUGIN_NAME,
    TokenSource,
    capture_token_sources,
    probe_config_path,
    tap_plugin_dir,
)

_CLAUDE_TIMEOUT_S = 90.0


class OffMode(StrEnum):
    DISABLE = "disable"
    """Stop capturing; leave the plugin installed so it is one keystroke back."""

    UNINSTALL = "uninstall"
    """Stop capturing and remove the plugin entirely."""


@dataclass
class TurnOffResult:
    """What actually happened, so the caller can tell the truth about it."""

    verified: bool = False
    cleared: list[TokenSource] = field(default_factory=list)
    remaining: list[TokenSource] = field(default_factory=list)
    killswitch_set: bool = False
    plugin_removed: bool = False
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.verified:
            return "Session capture is off. No credential resolves on this device."
        blockers = ", ".join(source.value for source in self.remaining)
        return f"Session capture is NOT fully off — still resolvable from: {blockers}"


def _clear_paired_token() -> bool:
    path = tap_plugin_dir() / ".token"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _clear_probe_config_token() -> bool:
    """Drop `ingest_token` from the CLI config, preserving everything else."""
    import json

    path = probe_config_path()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return True  # nothing there to clear
    if not isinstance(raw, dict):
        return True
    changed = raw.pop("ingest_token", None) is not None
    for context in (raw.get("contexts") or {}).values():
        if isinstance(context, dict) and context.pop("ingest_token", None) is not None:
            changed = True
    if not changed:
        return True
    try:
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(raw, indent=2, sort_keys=True))
        tmp.replace(path)
    except OSError:
        return False
    return True


def _set_killswitch() -> bool:
    """Write `.disabled`, which `hooks/session-start.sh` already checks BEFORE
    doing any work. This is what stops the next session from respawning the
    daemon, and it is why we do not need to invent a new mechanism."""
    try:
        directory = tap_plugin_dir()
        directory.mkdir(parents=True, exist_ok=True)
        (directory / ".disabled").write_text("disabled by `probe setup`\n")
    except OSError:
        return False
    return True


def clear_killswitch() -> None:
    """Remove `.disabled` so capture can be turned back on from the menu."""
    try:
        (tap_plugin_dir() / ".disabled").unlink(missing_ok=True)
    except OSError:
        pass


def _stop_daemon() -> list[str]:
    """Ask the running uploader to stop.

    The SessionStart hook records its watcher PID in
    /tmp/probe-research-tap-watcher-<sid>.pid. Terminating those stops the
    supervisor loop; the killswitch stops it coming back. Best-effort by design:
    a PID we cannot signal is reported, never silently ignored.
    """
    warnings: list[str] = []
    import glob
    import signal

    for pid_file in glob.glob("/tmp/probe-research-tap-watcher-*.pid"):
        try:
            pid = int(open(pid_file).read().strip())  # noqa: SIM115
        except (OSError, ValueError):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already gone: the desired state
        except OSError as exc:
            warnings.append(f"could not stop uploader pid {pid}: {exc}")
            continue
        try:
            os.unlink(pid_file)
        except OSError:
            pass
    return warnings


def _uninstall_plugin() -> tuple[bool, list[str]]:
    binary = shutil.which("claude")
    if not binary:
        return False, ["`claude` not found, so the plugin was left installed"]
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, no shell
            [binary, "plugin", "uninstall", f"{TAP_PLUGIN_NAME}@{MARKETPLACE}"],
            capture_output=True,
            text=True,
            timeout=_CLAUDE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, [f"plugin uninstall failed: {exc}"]
    if completed.returncode != 0:
        return False, [f"plugin uninstall failed: {completed.stderr.strip()}"]
    return True, []


def turn_off(mode: OffMode = OffMode.DISABLE) -> TurnOffResult:
    """Turn capture off and PROVE it, rather than assuming.

    Order matters: set the killswitch before clearing credentials, so a session
    starting mid-teardown finds the daemon already disabled rather than racing a
    half-cleared credential set.
    """
    result = TurnOffResult()
    before = capture_token_sources()

    result.killswitch_set = _set_killswitch()
    if not result.killswitch_set:
        result.warnings.append("could not write the killswitch marker")

    if TokenSource.PAIRED_FILE in before and _clear_paired_token():
        result.cleared.append(TokenSource.PAIRED_FILE)
    if TokenSource.PROBE_CONFIG in before and _clear_probe_config_token():
        result.cleared.append(TokenSource.PROBE_CONFIG)

    result.warnings.extend(_stop_daemon())

    if mode is OffMode.UNINSTALL:
        removed, warnings = _uninstall_plugin()
        result.plugin_removed = removed
        result.warnings.extend(warnings)

    # THE VERIFICATION. Re-resolve from scratch rather than trusting the
    # bookkeeping above.
    result.remaining = list(capture_token_sources())
    if TokenSource.ENVIRONMENT in result.remaining:
        # The one source the wizard genuinely cannot fix: it cannot unset a
        # variable in the parent shell. Saying so is the only honest option --
        # reporting "off" here would be the exact lie this module exists to
        # prevent.
        result.warnings.append(
            f"{ENV_INGEST_TOKEN} is set in your shell environment. This process "
            f"cannot unset it for you. Run `unset {ENV_INGEST_TOKEN}` and remove "
            "it from your shell profile, or capture will resume in new sessions."
        )
    result.verified = not result.remaining
    return result
