"""the small command line over the client (`python -m probe.service.client ...`, `python -m tap service ...`); no `__main__` block: only the pinned old path runs it."""

from __future__ import annotations

import argparse
import json
import os
import sys

from .calls import check_now, end, locations, owner_of, register, wait_for_exit
from .env import report_consent
from .service_api import ServiceError, call
from .start import ensure
from .vocabularies import CHECK_TRIGGERS, Client, Mode
from .wire import Method

# -- `python -m probe.service.client ...` / `python -m tap service ...` ----------


def main(argv: list[str] | None = None, *, client: str = Client.CLI, client_version: str = "unknown") -> int:
    """A small command line over this module (pi's extension spawns it through
    the tap). Prints one JSON object; exit 0 on success, 1 when no service
    answers, 2 for usage, 3 for an error answer."""
    parser = argparse.ArgumentParser(prog="probe-service-client")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_ensure = sub.add_parser("ensure", help="start the service when none runs")
    p_ensure.add_argument("--wait", type=float, default=0.0)
    p_reg = sub.add_parser("register", help="session.register")
    p_reg.add_argument("--session-id", required=True)
    p_reg.add_argument("--harness", required=True)
    p_reg.add_argument("--mode", required=True, choices=(Mode.DAEMON, Mode.INLINE, Mode.OFF))
    p_reg.add_argument("--owner-pid", type=int, default=None)
    p_reg.add_argument("--generation", type=int, required=True)
    p_reg.add_argument("--ensure", action="store_true", help="start the service first, waiting up to 3 s")
    p_end = sub.add_parser("end", help="session.end")
    p_end.add_argument("--session-id", required=True)
    p_end.add_argument("--generation", type=int, required=True)
    sub.add_parser("status", help="service.status")
    # Where this machine's service listens, for a client that speaks API v1
    # itself (pi's tool guard, `hooks.run`; the desktop app): the machine key
    # needs the OS's machine id, which they would otherwise work out again.
    # `socket` first, then `legacy_socket`; the rest is `locations()`.
    sub.add_parser("socket", help="print this machine's service socket paths (JSON)")
    p_check = sub.add_parser("check-now", help="update.check_now, starting the service when none runs")
    p_check.add_argument("--trigger", default="cli", choices=sorted(CHECK_TRIGGERS))
    p_check.add_argument(
        "--wait-for-pid", type=int, default=None, help="first wait for this process (the asking command) to exit"
    )
    p_call = sub.add_parser("call", help="any method with JSON params")
    p_call.add_argument("method")
    p_call.add_argument("params", nargs="?", default="{}")
    args = parser.parse_args(argv)

    if args.cmd == "ensure":
        print(json.dumps({"ensured": ensure(wait=args.wait)}))
        return 0
    if args.cmd == "socket":
        print(json.dumps(locations()))
        return 0
    try:
        if args.cmd == "register":
            if args.ensure:
                ensure(wait=3.0)
            owner = owner_of(args.owner_pid) if args.owner_pid else None
            answer = register(
                args.session_id, args.harness, args.mode, owner, args.generation,
                client=client, client_version=client_version,
                # Whether this registrant's environment allows automatic reports.
                report_env=report_consent(os.environ),
            )
        elif args.cmd == "end":
            answer = end(args.session_id, args.generation, client=client, client_version=client_version)
        elif args.cmd == "status":
            answer = call(Method.STATUS, client=client, client_version=client_version)
        elif args.cmd == "check-now":
            if args.wait_for_pid:
                wait_for_exit(args.wait_for_pid)
            answer = check_now(args.trigger, client=client, client_version=client_version)
        else:
            try:
                params = json.loads(args.params)
            except ValueError:
                print("params must be a JSON object", file=sys.stderr)
                return 2
            answer = call(args.method, params, client=client, client_version=client_version)
    except ServiceError as exc:
        print(json.dumps({"error": {"code": exc.code, "message": exc.message, "data": exc.data}}))
        return 3
    except OSError as exc:
        print(json.dumps({"error": {"code": None, "message": f"no service: {exc}"}}))
        return 1
    print(json.dumps(answer))
    return 0

