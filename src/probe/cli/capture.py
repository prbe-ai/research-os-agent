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
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum

from probe.cli import plugin_cli
from probe.cli.capabilities import (
    ENV_CODEX_INGEST_TOKEN,
    ENV_INGEST_TOKEN,
    MARKETPLACE,
    TokenSource,
    agent_source,
    capture_plugin_name,
    capture_token_sources,
    probe_config_path,
    tap_plugin_dir,
)


class OffMode(StrEnum):
    DISABLE = "disable"
    """Stop capturing; leave the plugin installed so it is one keystroke back."""

    UNINSTALL = "uninstall"
    """Stop capturing and remove the plugin entirely."""


@dataclass
class TurnOffResult:
    """What actually happened, so the caller can tell the truth about it."""

    cleared: list[TokenSource] = field(default_factory=list)
    remaining: list[TokenSource] = field(default_factory=list)
    killswitch_set: bool = False
    daemon_stopped: bool = False
    plugin_removed: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        """Off means ALL THREE, not just the credentials.

        Capture needs a credential, a live daemon, and no killswitch. Checking
        only the credential would report "off" while a surviving uploader keeps
        draining its queue with a bearer it already holds in memory, or while a
        failed killswitch write lets the next session respawn it.
        """
        return not self.remaining and self.killswitch_set and self.daemon_stopped

    def summary(self) -> str:
        if self.verified:
            return "Session capture is off. No credential resolves on this device."
        blockers: list[str] = [source.value for source in self.remaining]
        if not self.killswitch_set:
            blockers.append("killswitch not set")
        if not self.daemon_stopped:
            blockers.append("uploader still running")
        return f"Session capture is NOT fully off — {', '.join(blockers)}"


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
        (directory / ".disabled").write_text("disabled by the Probe Research wizard\n")
    except OSError:
        return False
    return True


def clear_killswitch() -> None:
    """Remove `.disabled` so capture can be turned back on from the menu."""
    try:
        (tap_plugin_dir() / ".disabled").unlink(missing_ok=True)
    except OSError:
        pass


def _looks_like_the_uploader(pid: int) -> bool:
    """Confirm a PID really is the tap before signalling it.

    /tmp is world-writable and the PID files live there, so an unprivileged
    local process can plant `probe-research-tap-watcher-<anything>.pid`
    containing any number it likes. Signalling on the strength of a filename
    would let it aim SIGTERM at an arbitrary process running as this user. PID
    reuse causes the same accident without anyone being malicious.

    So: ask the OS what the process actually is, and only proceed if it names
    the tap.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, no shell
            ["/bin/ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return capture_plugin_name() in completed.stdout or "tap" in completed.stdout.split()


def _stop_daemon() -> tuple[bool, list[str]]:
    """Ask the running uploader to stop, and confirm it did.

    Returns (stopped, warnings). `stopped` is False if any uploader is still
    alive afterwards, which must stop the caller claiming capture is off: a
    daemon that ignored SIGTERM still holds its bearer in memory and still has
    a queue to drain.
    """
    warnings: list[str] = []
    import glob
    import signal
    import time

    survivors: list[int] = []
    prefix = "prbe-codex-tap" if agent_source() == "codex" else "probe-research-tap"
    for pid_file in glob.glob(f"/tmp/{prefix}-watcher-*.pid"):
        try:
            stat = os.stat(pid_file)
            if stat.st_uid != os.getuid():
                warnings.append(f"ignoring {pid_file}: not owned by this user")
                continue
            pid = int(open(pid_file).read().strip())  # noqa: SIM115
        except (OSError, ValueError):
            continue
        if pid <= 1 or not _looks_like_the_uploader(pid):
            # Stale file or an impostor. Do not signal it.
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass  # already gone: the desired state
        except OSError as exc:
            warnings.append(f"could not stop uploader pid {pid}: {exc}")
            survivors.append(pid)
            continue
        # Wait for it, rather than assuming. An uploader mid-flush can take a
        # moment, and reporting "off" while it is still draining is the lie.
        for _ in range(20):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            except OSError:
                break
            time.sleep(0.1)
        else:
            survivors.append(pid)
            warnings.append(f"uploader pid {pid} is still running after SIGTERM")
            continue
        try:
            os.unlink(pid_file)
        except OSError:
            pass
    return (not survivors), warnings


def _uninstall_plugin() -> tuple[bool, list[str]]:
    selected = agent_source()
    result = plugin_cli.uninstall(selected, f"{capture_plugin_name(selected)}@{MARKETPLACE}")
    if not result.reachable:
        return False, [
            f"`{plugin_cli.binary_name(selected)}` not found, so the plugin was left installed"
        ]
    if not result.ok:
        return False, [f"plugin uninstall failed: {result.detail}"]
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

    stopped, stop_warnings = _stop_daemon()
    result.daemon_stopped = stopped
    result.warnings.extend(stop_warnings)

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
        token_env = ENV_CODEX_INGEST_TOKEN if agent_source() == "codex" else ENV_INGEST_TOKEN
        result.warnings.append(
            f"{token_env} is set in your shell environment. This process "
            f"cannot unset it for you. Run `unset {token_env}` and remove "
            "it from your shell profile, or capture will resume in new sessions."
        )
    return result
    (agent_source,)
    (capture_plugin_name,)
