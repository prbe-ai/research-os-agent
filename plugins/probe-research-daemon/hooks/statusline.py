#!/usr/bin/env python3
"""Probe Research status-line segment: the command that prints what the Probe service draws (service-only D14).

Reads the status-line payload on stdin, hands it to this machine's Probe service
(`statusline.render`, drawn by `probe.service.jobs.statusline_render`), prints the ONE line
it answers, exits 0. When no service answers it asks for one (`probe.service.client ensure`,
detached, at most every ENSURE_EVERY_S) and, under Claude Code, waits for it (START_WAIT_S); still
no answer, it prints exactly `Probe: service down` (T39). Nothing here reads Probe's state or
decides what to show.

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

THE WAIT (Claude Code only): Claude Code draws the line once when a session opens and again only on
its next event or `refreshInterval`, so a first draw that gave up at TIMEOUT_S showed "service down"
for seconds while the service came up (1-2 s cold) or answered late. A draw with no answer:

  * nothing listening: asks again every RETRY_EVERY_S until START_WAIT_S after the last start was
    asked (a dead service costs one wait per ENSURE_EVERY_S, not one per draw);
  * the socket took the connection and is slow (a service busy, or launchd starting it): holds that
    connection, one draw per ENSURE_EVERY_S on this machine (HOLD_STAMP; a draw during a hold joins
    it). launchd takes every connection for a service that cannot start, and Claude Code cancels a
    draw on each update: a hold on every draw froze the line on its last text, never "service down";
  * the connection closed unanswered (a service restarting): asks again, backing off to RETRY_MAX_S,
    under the same one-per-ENSURE_EVERY_S hold (a service that crashes on every start closes them all).

None waits past START_WAIT_S after its own start. Kimi (300 ms kill) and the Cursor chain (its own
`timeoutMs`) never wait.

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
#: A detached `ensure` is started at most this often (its stamp beside the service's folder: `_ensure_stamp`).
ENSURE_EVERY_S = 30.0
ENSURE_STAMP = "statusline.ensured"
#: Under Claude Code, how long a draw waits for a service that is starting or busy (THE WAIT).
START_WAIT_S = 5.0
RETRY_EVERY_S = 0.1
RETRY_MAX_S = 1.0
#: When a draw last held a connection the service was slow to answer; gone once one is answered.
HOLD_STAMP = "statusline.held"
#: The callers that never wait: Kimi's footer and a Probe-written chain (`statusline_render.py`
#: KIMI_STATUS_LINE_ENV and AGENT_ENV hold the same names).
KIMI_ENV = "KIMI_CODE_STATUS_LINE"
AGENT_ENV = "PROBE_STATUS_LINE"


class Unanswered(ConnectionError):
    """The socket took the connection and gave no answer: it closed it first (`closed`: a service
    restarting or stopping), or nothing came in time (a service starting, busy or stuck)."""

    def __init__(self, closed: bool):
        super().__init__("closed before answering" if closed else "no answer in time")
        self.closed = closed


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


def ask_service(paths, raw: str, deadline: float, hold=None) -> "str | None":
    """`statusline.render`'s line ("" when Probe draws nothing for this caller), or None: a reply
    with no line, or one this cannot read. OSError: no answer (Unanswered once the socket took the
    connection). `hold`, asked once when nothing has come by `deadline`, says how much longer to wait
    (Hold.until)."""
    path = paths.socket_path()
    paths.require_private(path.parent)
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "service.hello",
         "params": {"client": "cli", "client_version": "statusline", "api": 1}},
        {"jsonrpc": "2.0", "id": 2, "method": "statusline.render",
         "params": {"payload": raw[:MAX_PAYLOAD], "env": render_env(os.environ)}},
    ]
    unsent = memoryview(b"".join(json.dumps(line).encode("utf-8") + b"\n" for line in lines))
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(max(deadline - time.monotonic(), 0.01))
        sock.connect(str(path))
        try:
            buffer = b""
            while True:
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    try:
                        message = json.loads(line.decode("utf-8"))
                    except ValueError:  # not JSON or not UTF-8: a reply all the same, with no line
                        return None
                    if isinstance(message, dict) and message.get("id") == 2:
                        result = message.get("result")
                        text = result.get("text") if isinstance(result, dict) else None
                        return text if isinstance(text, str) else None
                if len(buffer) > 1 << 20:
                    return None
                sock.settimeout(max(deadline - time.monotonic(), 0.01))
                try:
                    if unsent:  # a request past the socket's buffer waits for a service that reads
                        unsent = unsent[sock.send(unsent):]
                        continue
                    chunk = sock.recv(65536)
                except socket.timeout:
                    later = hold() if hold is not None else None
                    if later is None or later <= time.monotonic():
                        raise Unanswered(closed=False) from None
                    deadline, hold = later, None
                    continue
                if not chunk:
                    raise Unanswered(closed=True)
                buffer += chunk
        except Unanswered:
            raise
        except OSError as exc:  # reset or broken pipe: the service went away mid-request
            raise Unanswered(closed=True) from exc
    finally:
        sock.close()


def start_service(paths) -> "float | None":
    """Ask for a service (`probe.service.client ensure`, the CLI's own way), detached and never
    waited on, at most every ENSURE_EVERY_S. When the last start was asked (`time.time()`), or
    None: no CLI is installed here to start one."""
    import subprocess  # noqa: PLC0415

    stamp = _ensure_stamp(paths)
    now = time.time()
    try:
        age = now - os.stat(stamp).st_mtime
        if 0 <= age < ENSURE_EVERY_S:  # dated ahead (the clock stepped back): stale
            return now - age
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
    if python is None or not _touch(stamp, now):
        return None  # a stamp that cannot be written cannot hold the starts to one per ENSURE_EVERY_S
    try:
        subprocess.Popen(  # noqa: S603 -- the CLI's own interpreter and client
            [python, "-I", "-m", "probe.service.client", "ensure"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd="/", close_fds=True, start_new_session=True,
        )
    except OSError:
        return None
    return now


def _ensure_stamp(paths) -> str:
    """ENSURE_STAMP beside this machine's service folder, named by the machine: there before a
    service ever made that folder (a machine's first session: `ensure` makes it)."""
    return str(paths.root_dir() / f"{ENSURE_STAMP}.{paths.machine_key()}")


def _touch(path: str, now: float) -> bool:
    """Create or re-date `path` at `now` by THIS machine's clock, which every age here is read by (an
    NFS server dates a bare `os.utime(path, None)` by its own). False: it cannot be written."""
    try:
        with open(path, "a"):
            pass
        os.utime(path, (now, now))
    except OSError:
        return False
    return True


class Hold:
    """THE WAIT's hold on a connection the service is slow to answer, shared by this machine's draws
    through HOLD_STAMP: younger than START_WAIT_S, a hold under way (join it, to its end); younger than
    ENSURE_EVERY_S, one that got no answer (hold nothing); else take one."""

    def __init__(self, paths, started: float):
        self.stamp = os.path.join(str(paths.service_dir()), HOLD_STAMP)
        self.started = started
        self.held = False

    def until(self) -> "float | None":
        """When this draw may hold its connection until (`time.monotonic()`), or None. Too near this
        draw's end, or with a stamp that cannot be written, it holds nothing and writes nothing (a
        stamp would read as a hold that got no answer)."""
        now = time.time()
        try:
            age = now - os.stat(self.stamp).st_mtime
        except OSError:
            age = -1.0
        joining = 0 <= age < START_WAIT_S
        if not joining and 0 <= age < ENSURE_EVERY_S:
            return None
        end = self.started + START_WAIT_S
        if joining:
            end = min(end, time.monotonic() + START_WAIT_S - age)
        if end - time.monotonic() < RETRY_EVERY_S or not (joining or _touch(self.stamp, now)):
            return None
        self.held = True
        return end

    def answered(self) -> None:
        """A held connection was answered, with a line or not: the next slow draw may hold again."""
        if self.held:
            try:
                os.unlink(self.stamp)
            except OSError:
                pass


def can_wait(environ) -> bool:
    """Only Claude Code's own `statusLine` waits (THE WAIT)."""
    return not (environ.get(KIMI_ENV) or environ.get(AGENT_ENV))


def wait_for_service(paths, raw: str, started: float, asked_at: "float | None", failure: OSError,
                     hold: Hold) -> "str | None":
    """THE WAIT after a first ask that got no answer (`failure`): the line, or None. Nothing listened
    to that ask: a service binds its socket before it can answer, so a slow connection then is held
    to the end of the start's window (already one per ENSURE_EVERY_S), whatever HOLD_STAMP says."""
    own_end = started + START_WAIT_S
    starting = not isinstance(failure, Unanswered)
    if not starting:
        end = hold.until() if failure.closed else None
        if end is None:
            return None  # held to its end, or a hold got no answer lately
    elif asked_at is not None:
        end = min(own_end, time.monotonic() + (asked_at + START_WAIT_S - time.time()))
    else:
        return None

    def window_end() -> float:
        return end

    delay = RETRY_EVERY_S
    while time.monotonic() + delay < end:
        time.sleep(delay)
        try:
            out = ask_service(paths, raw, min(end, time.monotonic() + TIMEOUT_S),
                              window_end if starting else hold.until)
            hold.answered()
            return out
        except Unanswered as exc:
            later = end if starting else hold.until()
            if not exc.closed or later is None:
                return None
            end, delay = later, min(delay * 2, RETRY_MAX_S)
        except OSError:
            pass
    return None


def main() -> int:
    started = time.monotonic()
    try:
        raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    except (OSError, ValueError):
        raw = ""
    out = None
    paths = None
    hold = None
    failure = None
    try:
        paths = _paths()
        hold = Hold(paths, started) if can_wait(os.environ) else None
        out = ask_service(paths, raw, started + TIMEOUT_S, hold.until if hold else None)
        if hold is not None:
            hold.answered()
    except OSError as exc:  # no answer; anything else, waiting will not mend
        failure = exc
    except Exception:  # noqa: BLE001 -- a render path may never traceback
        out = None
    if out is None and paths is not None:
        try:
            asked_at = start_service(paths)
            if hold is not None and failure is not None:
                out = wait_for_service(paths, raw, started, asked_at, failure, hold)
        except Exception:  # noqa: BLE001
            out = None
    if out is None:
        out = SERVICE_DOWN
    if out:
        # No newline: the caller joins segments, and a newline would become a row.
        sys.stdout.write(out.replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
