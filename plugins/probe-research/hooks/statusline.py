#!/usr/bin/env python3
"""Probe Research status-line segment: the command that prints what the Probe service draws (service-only D14).

Reads the status-line payload on stdin, hands it to this machine's Probe service
(`statusline.render`, drawn by `probe.service.jobs.statusline_render`), prints the ONE line
it answers, exits 0. When no service answers it prints exactly `Probe: service down` (T39)
and asks for one (`probe.service.client ensure`, detached, at most every ENSURE_EVERY_S):
the next render finds it. Nothing here reads Probe's state or decides what to show.

CONTRACT — this runs on a RENDER PATH, once per status-line update:

  * NO NETWORK, and no `probe` subprocess on the way to an answer: one local socket
    round trip. `_service_paths.py` (copied beside this file) says where the socket is.
  * SILENT ON EVERY FAILURE. Exit 0; a traceback here is a broken prompt.
  * ONE LINE, NEVER A NEWLINE. Claude Code renders each stdout line as its own row.

KIMI CODE (`$KIMI_CODE_HOME/tui.toml` `[status_line] command`) runs this too, with
`KIMI_CODE_STATUS_LINE=1` set: the service then draws Kimi's whole footer line 1. Kimi
kills the command after 300 ms (TIMEOUT_S stays inside it).

THE CURSOR CLI (`cli-config.json` `statusLine`) runs this too, through Probe's chain script, which
exports `PROBE_STATUS_LINE=cursor_agent` (a `_STATUS_LINE` variable: it travels to the service).

STDIN MAY BE EMPTY (a chained command before this one drained it): the service then finds the
session in the environment this hands over (RENDER_ENV).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time

#: The whole exchange (connect, hello, render), at most: inside Kimi's 300 ms.
TIMEOUT_S = 0.25
#: What the command prints when no service answers (T39, decided string).
SERVICE_DOWN = "Probe: service down"
#: The variables the service's renderer reads (`statusline_render.py`): the prefix, colour, the home
#: folder, and the caller's own Probe credential and config file (D11: they travel with the request;
#: the service never reads its own environment's); by suffix, every agent's session or thread
#: variable for a drained stdin and an agent's status-line switch, named by no agent here.
RENDER_ENV = ("PROBE_SL_PREFIX", "NO_COLOR", "HOME", "PROBE_TOKEN", "PROBE_MCP_TOKEN", "PROBE_CONFIG_PATH")
RENDER_ENV_SUFFIX = ("_SESSION_ID", "_THREAD_ID", "_STATUS_LINE")
MAX_PAYLOAD = 256 * 1024
#: A detached `ensure` is started at most this often (its stamp in the service's folder).
ENSURE_EVERY_S = 30.0
ENSURE_STAMP = "statusline.ensured"


def _paths():
    """`_service_paths.py` beside this file, by EXPLICIT path only (never a bare import: a
    same-named module in the user's project would run on every render)."""
    import importlib.util  # noqa: PLC0415

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_service_paths.py")
    spec = importlib.util.spec_from_file_location("_probe_statusline_service_paths", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def render_env(environ) -> dict:
    return {k: v for k, v in environ.items()
            if (k in RENDER_ENV or k.endswith(RENDER_ENV_SUFFIX)) and len(v) <= 4096 and "\0" not in v}


def ask_service(paths, raw: str, deadline: float) -> "str | None":
    """`statusline.render`'s line, or None: no service answered."""
    path = paths.socket_path()
    paths.require_private(path.parent)
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "service.hello",
         "params": {"client": "cli", "client_version": "statusline", "api": 1}},
        {"jsonrpc": "2.0", "id": 2, "method": "statusline.render",
         "params": {"payload": raw[:MAX_PAYLOAD], "env": render_env(os.environ)}},
    ]
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(max(deadline - time.monotonic(), 0.01))
        sock.connect(str(path))
        sock.sendall(b"".join(json.dumps(line).encode("utf-8") + b"\n" for line in lines))
        buffer = b""
        while True:
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = json.loads(line.decode("utf-8"))
                if isinstance(message, dict) and message.get("id") == 2:
                    result = message.get("result")
                    text = result.get("text") if isinstance(result, dict) else None
                    return text if isinstance(text, str) else None
            sock.settimeout(max(deadline - time.monotonic(), 0.01))
            chunk = sock.recv(65536)
            if not chunk or len(buffer) > 1 << 20:
                return None
            buffer += chunk
    finally:
        sock.close()


def start_service(paths) -> None:
    """Ask for a service (`probe.service.client ensure`, the CLI's own way), detached and never
    waited on, at most every ENSURE_EVERY_S. Nothing when no CLI is installed here."""
    import subprocess  # noqa: PLC0415

    stamp = os.path.join(str(paths.service_dir()), ENSURE_STAMP)
    try:
        if time.time() - os.stat(stamp).st_mtime < ENSURE_EVERY_S:
            return
    except OSError:
        pass
    python = None
    try:
        with open(paths.runtime_path(), encoding="utf-8") as handle:
            python = json.load(handle).get("python")
    except (OSError, ValueError, AttributeError):
        pass
    if not (isinstance(python, str) and os.path.isabs(python) and os.access(python, os.X_OK)):
        fallback = str(paths.fallback_python())
        python = fallback if os.access(fallback, os.X_OK) else None
    if python is None:
        return
    try:
        with open(stamp, "a"):
            pass
        os.utime(stamp, None)
        subprocess.Popen(  # noqa: S603 -- the CLI's own interpreter and client
            [python, "-I", "-m", "probe.service.client", "ensure"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/", close_fds=True, start_new_session=True,
        )
    except OSError:
        pass


def main() -> int:
    deadline = time.monotonic() + TIMEOUT_S
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except (OSError, ValueError):
        raw = ""
    out = None
    paths = None
    try:
        paths = _paths()
        out = ask_service(paths, raw, deadline)
    except Exception:  # noqa: BLE001 -- a render path may never traceback
        out = None
    if out is None:
        out = SERVICE_DOWN
        try:
            if paths is not None:
                start_service(paths)
        except Exception:  # noqa: BLE001
            pass
    if out:
        # No newline: the caller joins segments, and a newline would become a row.
        sys.stdout.write(out.replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
