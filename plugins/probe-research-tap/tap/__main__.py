"""CLI dispatch: `python -m tap <subcommand>`.

Install + registration are owned by Claude Code's plugin system — users
install via `claude plugin install probe-research-tap@research-os-agent`.
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
    print("  pair     exchange pairing token for a device token")
    print("  status   print local state")
    print("  revoke   revoke device + wipe local state")
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
    if cmd == "pair":
        from tap.pair import main as pair_main
        return pair_main(rest)
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
