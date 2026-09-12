"""CLI dispatch: `python -m tap <subcommand>`.

Install + registration are owned by the Claude Code or Codex plugin system.
Auth is device pairing: `python -m tap pair <token>` exchanges a
dashboard-minted pairing token for a device token (the manual/self-host
alternative is the probe CLI's `probe login`). This CLI covers the plugin's
runtime behavior (the daemon, pairing, and status).
"""

from __future__ import annotations

import sys


def _print_help() -> int:
    print("Usage: python -m tap <subcommand> [args]")
    print()
    print("Subcommands:")
    print("  watch    spawn the daemon (used by SessionStart hook)")
    print("  start    spawn the daemon idempotently, with gates (used by every caller)")
    print("  pair     exchange pairing token for a device token")
    print("  status   print local state")
    print("  revoke   revoke device + wipe local state")
    print("  redaction-notice  print (and clear) what was redacted last session")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 2:
        return _print_help()

    cmd = argv[1]
    rest = argv[2:]

    if cmd in ("-h", "--help", "help"):
        return _print_help()

    if cmd == "watch":
        from tap.main import main as watch_main

        return watch_main(rest)
    if cmd == "start":
        from tap.start import main as start_main

        return start_main(rest)
    if cmd == "pair":
        from tap.pair import main as pair_main

        return pair_main(rest)
    if cmd == "redaction-notice":
        # Reads and CLEARS the pending report, so the researcher is told once.
        # Silent and exit 0 when there is nothing pending or no local state
        # yet: this runs on the SessionStart path and must never be a reason a
        # session fails to start.
        try:
            from tap import config as cfg
            from tap.outbox import redaction_notice
            from tap.storage import Storage

            storage = Storage(cfg.state_db_path())
            try:
                notice = redaction_notice(storage)
            finally:
                storage.close()
        except Exception:  # noqa: BLE001
            return 0
        if notice:
            print(notice)
        return 0
    if cmd == "status":
        from tap.status import main as status_main

        return status_main(rest)
    if cmd == "revoke":
        from tap.revoke import main as revoke_main

        return revoke_main(rest)

    print(f"unknown subcommand {cmd!r}; try `python -m tap help`", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
