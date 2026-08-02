"""The detached half of the CLI's version check.

Two jobs, one entry point, because both must happen OUT of the invoking process:

    (no args)   refresh the cached manifest. The invoking CLI must never make a
                network call -- a 3s timeout on the every-command path would put
                a periodic stall inside training loops.

    --apply     wait for the spawning command to exit, then upgrade. The wait is
                what stops `uv tool upgrade` from replacing the tree while the
                command that triggered it is still lazily importing from it.

Run as `python -m probe.cli.version_refresh`, detached via start_new_session so it
outlives the CLI that spawned it. Nothing here prints anywhere a human will see;
the audit record in `autoupdate` is how a detached run reports what happened.
"""

from __future__ import annotations

import sys


def _refresh() -> None:
    """Fetch and cache the manifest, then release the single-flight claim.

    The claim was taken by the SPAWNER, before this process existed -- otherwise
    eight concurrent invocations would each spawn a refresher and only then
    discover they were racing. Releasing it here, in a finally, is what keeps
    that handoff honest.
    """
    from probe import version_policy

    try:
        version_policy.refresh()
    finally:
        version_policy.release_refresh()


def _apply() -> None:
    """Wait out the parent, then upgrade.

    `perform_update` does the waiting and re-checks the run lock afterwards; both
    live there because the wizard's own Update action shares this path and must
    behave identically when it is not spawned (no parent pid in the environment,
    so no wait).
    """
    from probe import version_policy
    from probe.cli import autoupdate
    from probe.cli.upgrading import perform_update

    # Single-writer across concurrent sessions. Without it, several Claude Code
    # windows starting at once each run `uv tool upgrade` against one install.
    if not autoupdate.acquire_lock():
        return
    try:
        perform_update(base_url=version_policy.base_url())
    finally:
        autoupdate.release_lock()


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        if "--apply" in args:
            _apply()
        else:
            _refresh()
    except Exception:  # noqa: BLE001
        # Detached: there is no terminal to raise into, and a traceback on stderr
        # goes to /dev/null. Failures that matter are in the audit record.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
