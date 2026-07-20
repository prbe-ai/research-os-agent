"""CLI dispatch: `python -m tap <subcommand>`.

Install + registration are owned by Claude Code's plugin system — users
install via `claude plugin install probe-research-tap@research-os-agent`.
Auth is owned by the probe CLI (`probe login` writes the ingest token +
base URL to ~/.config/probe/config.json) — there is no pairing step. This
CLI only covers the plugin's runtime behavior (the daemon and status).
"""

from __future__ import annotations

import sys


def _print_help() -> int:
    print("Usage: python -m tap <subcommand> [args]")
    print()
    print("Subcommands:")
    print("  watch    spawn the daemon (used by SessionStart hook)")
    print("  status   print local state")
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
    if cmd == "status":
        from tap.status import main as status_main
        return status_main(rest)

    print(f"unknown subcommand {cmd!r}; try `python -m tap help`", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
