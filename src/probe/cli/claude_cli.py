"""One way to run the `claude` binary, for every module that needs to.

Four modules grew their own copy of this — `setup.py`, `capabilities.py`,
`capture.py` and `updater.py` — each with a module-level `_CLAUDE_TIMEOUT_S`
and a near-identical `subprocess.run` around it. The copies had drifted to
20s / 90s / 180s / 90s, and only ONE of them (updater's) passed
`stdin=subprocess.DEVNULL`.

THE STDIN ARGUMENT IS THE POINT. `capture_output=True` redirects stdout and
stderr but NOT stdin, so a `claude` subcommand that decides to prompt inherits
the user's terminal: its question goes into the captured (invisible) stdout
while it blocks on a TTY nobody knows is being read, until the timeout fires.
A child that puts that TTY in raw mode eats the user's keystrokes meanwhile.
Handing it DEVNULL turns "hang for the full timeout" into "fail immediately on
EOF", which is a diagnosable failure instead of a mystery.

The timeouts stay DIFFERENT and stay at the call site: listing plugins is a
directory read and installing one is a network fetch, so a single shared
constant would be wrong for both. What is shared is the mechanism, not the
number.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

#: Reading local plugin state. No network, so a slow one is a wedged process.
LIST_TIMEOUT_S = 20.0

#: `marketplace add` + `update`. Claude bounds its own cache refresh at 120s
#: ("Refreshing marketplace cache (timeout: 120s)"), so this sits just above it
#: as a backstop rather than a second, shorter deadline that would cut Claude's
#: own retry off mid-flight.
REFRESH_TIMEOUT_S = 150.0

#: `plugin install`. Measured at well under a second against a warm marketplace;
#: 60s is the allowance for a cold fetch on a bad network.
INSTALL_TIMEOUT_S = 60.0

#: Turning capture off. Kept at the value capture.py used before this module.
CAPTURE_TIMEOUT_S = 90.0


@dataclass(frozen=True)
class Result:
    """The outcome of one `claude` invocation.

    `ok` is "the command did what was asked". `reachable` is the weaker "we got
    to run it at all" — False when the binary is missing or the process never
    produced an exit status. The two differ exactly where it matters: a machine
    with no Claude Code installed must not be reported as one where a plugin
    failed to install.
    """

    ok: bool
    detail: str = ""
    reachable: bool = True

    def __bool__(self) -> bool:
        return self.ok


def available() -> bool:
    """Whether the binary resolves at all. Absent is NORMAL on a GPU pod."""
    return shutil.which("claude") is not None


def run(args: list[str], *, timeout: float) -> Result:
    """Run `claude <args>` with output captured and stdin closed."""
    binary = shutil.which("claude")
    if not binary:
        return Result(ok=False, detail="`claude` not found on PATH", reachable=False)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, no shell
            [binary, *args],
            capture_output=True,
            text=True,
            # See the module docstring: without this the child inherits the
            # user's terminal and an invisible prompt costs the full timeout.
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Distinct from a non-zero exit: nothing ran to completion, so the
        # caller cannot conclude anything about the state on disk.
        return Result(ok=False, detail=f"timed out after {timeout:.0f}s", reachable=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return Result(ok=False, detail=str(exc), reachable=False)
    # `stdout`/`stderr` are None whenever the streams were not captured. That
    # never happens on the path above, but a caller (or a test double) handing
    # back a CompletedProcess built some other way must not turn into an
    # AttributeError inside the one helper every `claude` call goes through.
    out = (completed.stdout or "").strip()
    err = (completed.stderr or "").strip()
    if completed.returncode != 0:
        return Result(ok=False, detail=err or out)
    return Result(ok=True, detail=out)
