"""`python -m tap status` — print local daemon state.

Auth is the probe CLI's ingest token (env PROBE_INGEST_TOKEN or
~/.config/probe/config.json); there is no pairing step. "Not configured"
therefore means "no ingest token", and the device_id is minted locally by
the daemon on first start rather than by a pairing exchange.
"""

from __future__ import annotations

import json
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


def _last_stop_event() -> dict | None:
    """Newest record in the killer-side stop journal, or None.

    Written by the probe CLI's `_stop_daemon()` (capture off / wizard / doctor
    flows) into <plugin_dir>/logs/stop-daemon.jsonl — the same state dir both
    sides resolve, env overrides and codex flavor included. Surfacing it here
    means a "transcripts missing" report carries its own cause: a daemon that
    died with no entry here was NOT stopped by the probe CLI.
    """
    path = cfg.plugin_dir() / "logs" / "stop-daemon.jsonl"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except ValueError:
            continue
        if isinstance(record, dict):
            return record
    return None


def _describe_stop_event(event: dict) -> str:
    ts = event.get("ts")
    when = _relative(str(int(ts))) if isinstance(ts, (int, float)) else "unknown time"
    argv = event.get("argv")
    cmd = " ".join(str(a) for a in argv) if isinstance(argv, list) and argv else "unknown command"
    return f"{when} by `{cmd}` (pid {event.get('pid')})"


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
        stop_event = _last_stop_event()
        if stop_event is not None:
            print(f"  last stop:     {_describe_stop_event(stop_event)}")
        return 0
    finally:
        storage.close()


def main(_argv: list[str] | None = None) -> int:
    return run()


if __name__ == "__main__":
    sys.exit(main())
