"""`python -m tap status` — print local daemon state.

Auth is the probe CLI's ingest token (env PROBE_INGEST_TOKEN or
~/.config/probe/config.json); there is no pairing step. "Not configured"
therefore means "no ingest token", and the device_id is minted locally by
the daemon on first start rather than by a pairing exchange.
"""

from __future__ import annotations

import sys
import time

from tap import config as cfg
from tap.outbox import token_fingerprint
from tap.storage import Storage


def _relative(unix_str: str) -> str:
    if not unix_str:
        return "never"
    try:
        n = int(unix_str)
    except ValueError:
        return unix_str
    delta = max(0, int(time.time()) - n)
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{delta // 60} minutes ago"
    if delta < 86400:
        return f"{delta // 3600} hours ago"
    return f"{delta // 86400} days ago"


def run() -> int:
    token = cfg.load_token()
    if not token:
        print(
            "probe-research-tap: not configured — run `probe login` with an "
            "ingest token, or set PROBE_INGEST_TOKEN"
        )
        return 1

    try:
        base_url = cfg.api_base_url()
    except cfg.APIBaseURLUnset:
        print("probe-research-tap: no backend base URL configured")
        print(
            "  Run `probe login` (writes base_url to "
            f"{cfg.probe_config_path()}) or set PROBE_BASE_URL."
        )
        return 1

    storage = Storage(cfg.state_db_path())
    try:
        last_401 = storage.get_meta("last_401_at")
        if last_401:
            # The halt self-clears on daemon start once the token changes;
            # only report "halted" while the rejected credential is still
            # the configured one.
            rejected_fp = storage.get_meta("last_401_token_sha256")
            if not rejected_fp or rejected_fp == token_fingerprint(token):
                print(
                    "probe-research-tap: halted "
                    f"(ingest token rejected {_relative(last_401)})"
                )
                print(
                    "  Fix PROBE_INGEST_TOKEN or run `probe login` with a "
                    "valid ingest token to resume."
                )
                return 1

        device_id = storage.get_meta("device_id")
        print("probe-research-tap: configured")
        print(f"  backend:       {base_url}")
        print(f"  device:        {device_id or '(assigned on first daemon start)'}")
        print(f"  last shipped:  {_relative(storage.get_meta('last_successful_post_at'))}")
        print(f"  outbox:        {storage.outbox_row_count()} rows, {storage.outbox_byte_size()} bytes")
        active_s, idle_s = cfg.intervals()
        if active_s == idle_s:
            print(f"  interval:      {active_s}s (flat)")
        else:
            print(f"  interval:      {active_s}s active / {idle_s}s idle")
        return 0
    finally:
        storage.close()


def main(_argv: list[str] | None = None) -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
