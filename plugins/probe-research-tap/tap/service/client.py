"""Connect to the Probe service, starting it when nobody has (API v1, "Lifecycle").

    ensure()    connect; else take start.lock, try again, else spawn the CLI's
                python `-m probe.service` detached. Never waits for readiness
                unless asked: a hook's budget is a connect() and a fork.
    register()  session.register over one short connection (hello first).
    queue_hook() hooks.queue: a hook event's record-keeping routes, run once (D36).
    end()       session.end, likewise.
    check_now() ensure the service (waiting for it), then update.check_now:
                what the CLI and the plugin hook do instead of spawning an
                updater of their own while the switch is on (plan Q4)
    finish_recorders()  recorders.finish: `probe capture off` for one agent
    hold_run() / release_run()  run.hold / run.release: a `probe exec`'s run
    refresh_manifest()  manifest.refresh: a stale release-manifest cache
    desktop_request()   desktop.request: one of the desktop app's
                management requests, answered as `probe desktop request`
    drain_outbox() / watch_box() / send_telemetry()  outbox.drain, box.watch,
                telemetry.send: a training process's background work (plan
                Q5), each within one short budget (`call_within`)
    refresh_statusline() / sync_notes()  statusline.refresh / notes.sync: a
                hook's status-line read and team-note sync
    start_import()  import.start: an approved import's worker
                Never start the service: each is for a service that already
                runs (OSError when none answers), and the caller falls back.
    locations() where this machine's service keeps its socket and files
                (`python -m probe.service.client socket` prints them)
    call()      any method, for the CLI and tests.
    call_within()  one request, hello pipelined with it, under ONE deadline.
    Connection  a kept connection: the tap's registration thread holds one, so
                a restarted service is noticed (EOF) and re-told at once.

ONE source, byte-identical copies: the tap (`tap/service/client.py`) and,
through the tap's `python -m tap service ...`, pi carry this file because
neither can import `probe` (it lives in the CLI's uv tool env, they run under
the system python). `make sync-service-client` copies it with `paths.py`,
`procid.py` and `protocol.py`; `tests/test_service_client_sync.py` fails on
drift. Stdlib only, imports between the four relative.

There is no switch: the service runs on every Linux, macOS and (since R3,
over a named pipe: `winpipe.py`) Windows machine with a new enough CLI. A session whose Probe mode is `off` never starts or holds it.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable

from . import paths, procid
from .protocol import (
    API_VERSION, JSONRPC, MAX_LINE_BYTES, REPORT_ENV, Client, Method, Mode, encode, report_consent, request,
)


#: Every socket operation's deadline (plan X6: the registrant's own 2 s).
TIMEOUT_S = 2.0
#: `ensure` stops spawning after this many starts in START_WINDOW_S: a service
#: that crash-loops shows up as repeated starts in service.log, never as a
#: process storm (plan "Crash").
MAX_STARTS = 3
START_WINDOW_S = 60.0
#: A spawn younger than this whose pid still runs is starting: no second spawn.
SPAWN_SETTLE_S = 3.0
START_LOCK_WAIT_S = 2.0
#: `check_now` waits this long for a service it had to start (never in a hook:
#: the hook and the CLI run it as a detached process).
CHECK_NOW_WAIT_S = 5.0
#: `check-now --wait-for-pid` waits at most this long for the command that
#: asked (`probe._shared.autoupdate.WAIT_FOR_PID_TIMEOUT`).
WAIT_FOR_PID_S = 1800.0
#: `desktop_request`'s socket deadline: the service's longest (a change, 120 s)
#: and a little more, so its own `timed_out` answer arrives first.
DESKTOP_TIMEOUT_S = 125.0
#: What a training process waits for the service, at most, all told (connect,
#: hello, the call; plan Q5): a hand-off never holds a training loop longer.
HANDOFF_BUDGET_S = 0.25

#: The only variables the service starts with (plan X2): never an agent's
#: PROBE_TOKEN, endpoint or PYTHONPATH, which would leak the first launcher's
#: context into every later client's requests. The PROBE_SERVICE_* are
#: test-only overrides of the lifecycle and updater timers (and of where the
#: telemetry thread POSTs, `background.TELEMETRY_HOST_ENV`).
ENV_ALLOWLIST = (
    "HOME",
    "PATH",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "PROBE_SERVICE_GRACE_S",
    "PROBE_SERVICE_DISCOVERY_S",
    "PROBE_SERVICE_TTL_S",
    "PROBE_SERVICE_WORKER_STOP_S",
    "PROBE_SERVICE_DEFER_LIMIT_S",
    "PROBE_SERVICE_APP_RESULT_TIMEOUT_S",
    "PROBE_SERVICE_UPDATE_TIMEOUT_S",
    "PROBE_SERVICE_UPDATE_STOP_GRACE_S",
    "PROBE_SERVICE_LEGACY_RETRY_S",
    "PROBE_SERVICE_RECORDER_RETRY_S",
    "PROBE_SERVICE_ALERT_POLL_S",
    "PROBE_SERVICE_KEEPER",
    "PROBE_SERVICE_WORKER_POLL_S",
    "PROBE_SERVICE_WORKER_BACKOFF_S",
    # The watch's test-only switches (`watch.REPORTS_ENV`, `watch.SCAN_ENV`).
    "PROBE_SERVICE_REPORTS",
    "PROBE_SERVICE_WATCH_S",
    # Windows: what a Python process and its children need there to run at all
    # (SYSTEMROOT for sockets and random), and where the user's folders are.
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "TEMP",
    "TMP",
    "USERPROFILE",
    "USERNAME",
    "USERDOMAIN",
    "HOMEDRIVE",
    "HOMEPATH",
    "LOCALAPPDATA",
    "APPDATA",
    "PROGRAMDATA",
    "PROBE_SERVICE_TELEMETRY_HOST",
)


class Ensured:
    """What `ensure` did."""

    RUNNING = "running"  # a service answered
    SPAWNED = "spawned"  # none did; this call started one
    STARTING = "starting"  # another caller started one moments ago
    BACKOFF = "backoff"  # MAX_STARTS in START_WINDOW_S already: not again yet
    UNAVAILABLE = "unavailable"  # no interpreter to start it with
    #: This process runs with no_new_privs (Linux): a service it started would
    #: too, and could do no caller's work (`caller.landlock_possible`). None started.
    CONFINED = "confined"


class Unanswered(ConnectionError):
    """`call_within`: the request went out (or may have, in part) and no
    answer came back: the connection closed, or broke. The service may have
    acted on it."""


class AnswerTimeout(Unanswered, TimeoutError):
    """`call_within`: the request went out and no answer came in time."""


class ServiceError(Exception):
    """A JSON-RPC error answer: `code` is API v1's error code."""

    def __init__(self, code: int, message: str, data: object = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.data = data


# -- connections --------------------------------------------------------------


class Connection:
    """One connection to the service: line-framed JSON-RPC, one call at a time."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._buffer = b""
        self._next_id = 0
        #: Notifications that arrived while waiting for an answer, oldest first.
        self.events: list = []

    @classmethod
    def open(cls, timeout: float = TIMEOUT_S) -> Connection:
        """Connect, or raise OSError (FileNotFoundError / ConnectionRefusedError
        when no service runs; PermissionError when the socket's folder is not
        this user's alone, so what answers there could be anyone's).

        This machine's socket first, then the one a service from before the
        machine key listens on (plan D8): a CLI that is not updated yet starts
        that older service, and this client must still find it. "None runs"
        (the first error) only when that one is absent or unlistened too; its
        own timeout or foreign folder is raised as itself, never read as no
        service."""
        if paths.WINDOWS:  # this user's named pipe (`winpipe.py`); no older service there
            return cls._at(None, timeout)
        try:
            return cls._at(paths.socket_path(), timeout)
        except (FileNotFoundError, ConnectionRefusedError) as first:
            try:
                return cls._at(paths.legacy_socket_path(), timeout)
            except (FileNotFoundError, ConnectionRefusedError):
                raise first from None

    @classmethod
    def _at(cls, path, timeout: float) -> Connection:
        if paths.WINDOWS:
            from . import winpipe

            return cls(winpipe.connect(paths.pipe_name(), timeout))
        paths.require_private(path.parent)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(path))
        except BaseException:
            sock.close()
            raise
        return cls(sock)

    def fileno(self) -> int:
        return self.sock.fileno()

    def wait_readable(self, timeout: float) -> bool:
        """Has the service sent something (or closed), within `timeout`?
        `select` on POSIX; the pipe's own poll on Windows, which `select`
        cannot wait on."""
        if self._buffer:
            return True
        if paths.WINDOWS:
            return self.sock.readable(timeout)
        import select

        readable, _, _ = select.select([self.sock.fileno()], [], [], max(0.0, timeout))
        return bool(readable)

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.close()

    def __enter__(self) -> Connection:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _read_message(self) -> dict:
        while b"\n" not in self._buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionResetError("the service closed the connection")
            self._buffer += chunk
            if len(self._buffer) > MAX_LINE_BYTES + 1 and b"\n" not in self._buffer:
                raise ConnectionError("the service sent a line over 1 MiB")
        line, self._buffer = self._buffer.split(b"\n", 1)
        message = json.loads(line.decode("utf-8"))
        if not isinstance(message, dict):
            raise ConnectionError("the service sent something other than a JSON object")
        return message

    def call(self, method: str, params: dict | None = None) -> object:
        """Send one request and return its `result`; ServiceError for an `error`."""
        self._next_id += 1
        id_ = self._next_id
        self.sock.sendall(encode(request(id_, method, params)))
        while True:
            message = self._read_message()
            if message.get("method") == "event" and "id" not in message:
                self.events.append(message.get("params") or {})
                continue
            if message.get("jsonrpc") != JSONRPC or message.get("id") != id_:
                continue
            if "error" in message:
                err = message["error"] or {}
                raise ServiceError(int(err.get("code", 0)), str(err.get("message", "")), err.get("data"))
            return message.get("result")

    def hello(self, client: str, client_version: str, *, keepalive: bool = False) -> dict:
        params = {"client": client, "client_version": client_version or "unknown", "api": API_VERSION}
        if keepalive:
            params["keepalive"] = True
        answer = self.call(Method.HELLO, params)
        return answer if isinstance(answer, dict) else {}

    def next_event(self, timeout: float) -> dict | None:
        """The next notification (after `events.subscribe`), or None by `timeout`."""
        if self.events:
            return self.events.pop(0)
        previous = self.sock.gettimeout()
        self.sock.settimeout(timeout)
        try:
            message = self._read_message()
        except socket.timeout:
            return None
        finally:
            self.sock.settimeout(previous)
        if message.get("method") == "event":
            return message.get("params") or {}
        return None


def connect(timeout: float = TIMEOUT_S) -> Connection | None:
    """A connection, or None when no service answers."""
    try:
        return Connection.open(timeout)
    except OSError:
        return None


def call(
    method: str,
    params: dict | None = None,
    *,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> object:
    """One request on its own connection (hello first). OSError when no service runs."""
    with Connection.open(timeout) as conn:
        conn.hello(client, client_version)
        return conn.call(method, params)


def call_within(
    method: str,
    params: dict | None = None,
    *,
    budget: float = HANDOFF_BUDGET_S,
    deadline: float | None = None,
    on_sent: Callable[[], None] | None = None,
    client: str = Client.SDK,
    client_version: str = "unknown",
) -> object:
    """One request on its own connection, its hello sent in the same write,
    the answer by `deadline` (monotonic; default `budget` seconds from now)
    all told (`call`'s timeout is per socket operation; here every operation
    gets what is left). OSError when no service answers in time;
    `Unanswered` (`AnswerTimeout` for time) once the request may have gone
    out, so a caller can tell "it may have it" from "it never got it";
    ServiceError for an error answer, the hello's included (a service from
    before the client kind or the method). `on_sent` is called just before
    the request is written."""
    if deadline is None:
        deadline = time.monotonic() + budget

    def left() -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("the service did not answer in time")
        return remaining

    with Connection.open(left()) as conn:
        hello = {"client": client, "client_version": client_version or "unknown", "api": API_VERSION}
        conn.sock.settimeout(left())
        if on_sent is not None:
            on_sent()
        buffer = b""
        try:
            conn.sock.sendall(encode(request(1, Method.HELLO, hello)) + encode(request(2, method, params)))
            conn._next_id = 2
            while True:
                while b"\n" not in buffer:
                    conn.sock.settimeout(left())
                    chunk = conn.sock.recv(65536)
                    if not chunk:
                        raise ConnectionResetError("the service closed the connection")
                    buffer += chunk
                    if len(buffer) > MAX_LINE_BYTES + 1 and b"\n" not in buffer:
                        raise ConnectionError("the service sent a line over 1 MiB")
                line, buffer = buffer.split(b"\n", 1)
                message = json.loads(line.decode("utf-8"))
                if not isinstance(message, dict) or message.get("jsonrpc") != JSONRPC:
                    continue
                if message.get("id") not in (1, 2):
                    continue  # an event (none before a subscribe), or not ours
                if "error" in message:
                    err = message["error"] or {}
                    raise ServiceError(int(err.get("code", 0)), str(err.get("message", "")), err.get("data"))
                if message.get("id") == 2:
                    return message.get("result")
        except socket.timeout as exc:
            raise AnswerTimeout(str(exc) or "the service did not answer in time") from None
        except (OSError, ValueError) as exc:  # closed, reset, or not an answer
            raise Unanswered(f"no answer: {exc!r}") from None


def drain_outbox(
    directory: str,
    python: str,
    env: dict,
    *,
    cwd: str | None = None,
    budget: float = HANDOFF_BUDGET_S,
    deadline: float | None = None,
    on_sent: Callable[[], None] | None = None,
    client_version: str = "unknown",
) -> dict:
    """`outbox.drain`: `{accepted: true, state}` or `{accepted: false, reason}`."""
    params = {"dir": directory, "python": python, "env": env}
    if cwd:
        params["cwd"] = cwd
    return _dict(call_within(Method.OUTBOX_DRAIN, params, budget=budget, deadline=deadline, on_sent=on_sent,
                             client_version=client_version))


def watch_box(
    python: str,
    env: dict,
    *,
    cwd: str | None = None,
    budget: float = HANDOFF_BUDGET_S,
    deadline: float | None = None,
    on_sent: Callable[[], None] | None = None,
    client_version: str = "unknown",
) -> dict:
    """`box.watch`: `{accepted: true, state}` or `{accepted: false, reason}`.
    `env` names the box registry folder (PROBE_BOX_DIR)."""
    params = {"python": python, "env": env}
    if cwd:
        params["cwd"] = cwd
    return _dict(call_within(Method.BOX_WATCH, params, budget=budget, deadline=deadline, on_sent=on_sent,
                             client_version=client_version))


def send_telemetry(
    entries: list,
    *,
    batch_id: str | None = None,
    budget: float = HANDOFF_BUDGET_S,
    deadline: float | None = None,
    on_sent: Callable[[], None] | None = None,
    client_version: str = "unknown",
) -> dict:
    """`telemetry.send`: `{accepted: true}` or `{accepted: false, reason}`.
    `batch_id`: the same id on a resend, so the service takes the batch once."""
    params: dict = {"entries": entries}
    if batch_id:
        params["id"] = batch_id
    return _dict(call_within(Method.TELEMETRY_SEND, params, budget=budget, deadline=deadline, on_sent=on_sent,
                             client_version=client_version))


def owner_of(pid: int) -> dict | None:
    """`{pid, start_time}` for a running pid, or None (a heartbeat-only owner)."""
    start = procid.start_time(pid)
    return {"pid": pid, "start_time": start} if start else None


def register(
    sid: str,
    harness: str,
    mode: str,
    owner: dict | None,
    generation: int,
    *,
    client: str = Client.TAP,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
    report_env: bool | None = None,
) -> dict:
    params = {"sid": sid, "harness": harness, "mode": mode, "owner": owner, "generation": generation}
    if report_env is not None:
        params[REPORT_ENV] = report_env
    answer = call(Method.SESSION_REGISTER, params, client=client, client_version=client_version, timeout=timeout)
    return answer if isinstance(answer, dict) else {}


def end(
    sid: str,
    generation: int,
    *,
    client: str = Client.TAP,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    params = {"sid": sid, "generation": generation}
    answer = call(Method.SESSION_END, params, client=client, client_version=client_version, timeout=timeout)
    return answer if isinstance(answer, dict) else {}


def _dict(answer: object) -> dict:
    return answer if isinstance(answer, dict) else {}


def finish_recorders(
    harness: str,
    *,
    wait: float = 0.0,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`recorders.finish`: `{finished: [sid], unfinished: [sid]}`. The socket
    waits `wait` longer than `timeout`, the service's own wait."""
    params = {"harness": harness, "wait": wait}
    return _dict(call(Method.RECORDERS_FINISH, params, client=client, client_version=client_version,
                      timeout=timeout + wait))


def hold_run(
    run: str,
    owner: dict,
    *,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`run.hold`: `{held: true}` or `{held: false, reason}`."""
    params = {"run": run, "owner": owner}
    return _dict(call(Method.RUN_HOLD, params, client=client, client_version=client_version, timeout=timeout))


def release_run(
    run: str,
    owner: dict | None = None,
    *,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`run.release`: `{released: n}`."""
    params: dict = {"run": run}
    if owner is not None:
        params["owner"] = owner
    return _dict(call(Method.RUN_RELEASE, params, client=client, client_version=client_version, timeout=timeout))


def refresh_manifest(
    context: dict | None = None,
    *,
    wait: float = 0.0,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`manifest.refresh`: `{outcome, done, result}`. `context`: the caller's
    environment, by `protocol.CONTEXT_ENV` name."""
    params: dict = {"wait": wait}
    if context:
        params["context"] = context
    return _dict(call(Method.MANIFEST_REFRESH, params, client=client, client_version=client_version,
                      timeout=timeout + wait))


def queue_hook(
    event: dict,
    *,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`hooks.queue` (plan D36): hand the service one hook event's
    record-keeping routes, to run once by `event["id"]` (`{id, plugin_root,
    lane, payload, env, cwd, at}`, `protocol.need_hook_queue`). The answer:
    `{"queued": true, "duplicate": bool}`, or `{"refused": why}` (then run them
    yourself). OSError when no service runs. The plugin's dispatcher speaks it
    on its own socket (`hooks/dispatch.py` `Miss`): it cannot import this."""
    answer = call(Method.HOOKS_QUEUE, event, client=client, client_version=client_version, timeout=timeout)
    return answer if isinstance(answer, dict) else {}


def desktop_request(
    request: dict,
    context: dict | None = None,
    *,
    origin: str | None = None,
    client: str = Client.APP,
    client_version: str = "unknown",
    timeout: float = DESKTOP_TIMEOUT_S,
) -> dict:
    """`desktop.request`: one management request (`probe desktop request
    --json`'s stdin object) -> its v1 envelope, `{v, ok, data | error}`.
    `origin="desktop"`: a person asked. ServiceError for a refusal (its
    `data.reason`), OSError when no service answers: the caller then runs the
    CLI, as before (`agent/docs/desktop-service-management.md`)."""
    params: dict = {"request": request}
    if context:
        params["context"] = context
    if origin is not None:
        params["origin"] = origin
    return _dict(call(Method.DESKTOP_REQUEST, params, client=client, client_version=client_version, timeout=timeout))


def locations() -> dict:
    """Where this machine's service keeps its files, for a client that speaks
    API v1 itself and must not work out the machine key again (the desktop
    app, pi's tool guard): `socket` (connect here first), `legacy_socket`
    (then here: a service from before the machine key), `service_dir`,
    `root_dir`, `runtime` (the CLI's interpreter, `runtime.json`) and
    `machine_key`. All absolute; nothing is created."""
    return {
        "socket": str(paths.socket_path()),
        "legacy_socket": str(paths.legacy_socket_path()),
        "service_dir": str(paths.service_dir()),
        "root_dir": str(paths.root_dir()),
        "runtime": str(paths.runtime_path()),
        "machine_key": paths.machine_key(),
    }
def refresh_statusline(
    sid: str,
    plugin_root: str,
    *,
    agent: str | None = None,
    python: str | None = None,
    context: dict | None = None,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`statusline.refresh`: `{scheduled: true, due_in}` or `{scheduled: false, reason}`."""
    params: dict = {"sid": sid, "plugin_root": plugin_root}
    if agent:
        params["agent"] = agent
    if python:
        params["python"] = python
    if context:
        params["context"] = context
    return _dict(call(Method.STATUSLINE_REFRESH, params, client=client, client_version=client_version,
                      timeout=timeout))


def sync_notes(
    mode: str,
    *,
    rules_for: list | None = None,
    feedback: bool = False,
    context: dict | None = None,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`notes.sync`: `{outcome: scheduled | queued}` or `{outcome: null, reason}`."""
    params: dict = {"mode": mode, "rules_for": list(rules_for or []), "feedback": feedback}
    if context:
        params["context"] = context
    return _dict(call(Method.NOTES_SYNC, params, client=client, client_version=client_version, timeout=timeout))


def start_import(
    job_id: str,
    directory: str,
    attempt: int,
    env: dict,
    *,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`import.start`: `{started: true, pid, start_time}` or `{started: false, reason}`."""
    params = {"job_id": job_id, "directory": directory, "attempt": attempt, "env": env}
    return _dict(call(Method.IMPORT_START, params, client=client, client_version=client_version, timeout=timeout))


def wait_for_exit(pid: int, *, timeout: float = WAIT_FOR_PID_S, poll: float = 0.25) -> bool:
    """Until `pid` is gone (True), or `timeout` passed (False).

    The CLI asks for an update from its root callback, before its own command
    runs, and imports lazily all the way through it: an update that started
    now would replace files that command is still reaching for (a
    `ModuleNotFoundError`; `probe._shared.autoupdate.wait_for_pid_exit`). The
    barrier stops new starts only, so the asking process waits it out here."""
    deadline = time.monotonic() + timeout
    while True:
        if not procid.exists(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def check_now(
    trigger: str = "cli",
    *,
    wait: float = CHECK_NOW_WAIT_S,
    client: str = Client.CLI,
    client_version: str = "unknown",
) -> dict:
    """Ask the service for an update check now, starting it when none runs.
    `trigger` says what asks (app | cli | session | schedule). OSError when no
    service answers after `wait`."""
    ensure(wait=wait)
    answer = call(
        Method.UPDATE_CHECK_NOW, {"trigger": trigger}, client=client, client_version=client_version
    )
    return answer if isinstance(answer, dict) else {}


# -- starting the service ------------------------------------------------------

#: Services this process spawned, polled on every later `ensure` so a
#: long-lived spawner (a tap) reaps the exited ones instead of keeping zombies.
_children: list = []


def _reap() -> None:
    for child in list(_children):
        if child.poll() is not None:
            _children.remove(child)


def runtime_python() -> str | None:
    """The CLI's interpreter: runtime.json's, else the uv tool env's, else None."""
    candidates = []
    try:
        data = json.loads(paths.runtime_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("python"), str):
            candidates.append(data["python"])
    except (OSError, ValueError):
        pass
    candidates.append(str(paths.fallback_python()))
    for candidate in candidates:
        if os.path.isabs(candidate) and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def write_runtime(python: str, cli_version: str) -> bool:
    """Record the CLI's interpreter for every client (the CLI calls this at start).

    Cheap when nothing changed: one small read. True when the file was written.
    """
    # abspath, never realpath: a venv's python is a symlink to the base
    # interpreter, and resolving it would lose the venv.
    wanted = {"python": os.path.abspath(python), "cli_version": cli_version}
    target = paths.runtime_path()
    try:
        if json.loads(target.read_text(encoding="utf-8")) == wanted:
            return False
    except (OSError, ValueError):
        pass
    paths.private_dir(target.parent)
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
            json.dump(wanted, handle)
        paths.replace(tmp, target)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
    return True


def _clean_env() -> dict:
    env = {name: os.environ[name] for name in ENV_ALLOWLIST if name in os.environ}
    env.setdefault("HOME", str(paths.home()))
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    return env


def _read_starts(now: float) -> list:
    try:
        data = json.loads(paths.starts_path().read_text(encoding="utf-8"))
        starts = data.get("starts") if isinstance(data, dict) else None
    except (OSError, ValueError):
        starts = None
    kept = []
    for entry in starts or []:
        if isinstance(entry, dict) and isinstance(entry.get("at"), (int, float)):
            if now - float(entry["at"]) < START_WINDOW_S:
                kept.append(entry)
    return kept


def _write_starts(starts: list) -> None:
    target = paths.starts_path()
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
            json.dump({"starts": starts}, handle)
        paths.replace(tmp, target)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def forget_start(*pids: int) -> None:
    """A service restarting on purpose (an update, a newer tap) takes its own
    start out of the count: only a service that dies backs `ensure` off
    (MAX_STARTS). Without it an update's restart and the tap restart right
    after it left the next start refused for most of a minute, and every hook
    meanwhile found no service. Never raises."""
    try:
        paths.ensure_dirs()
        fd = os.open(paths.start_lock_path(), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return
    try:
        if not _lock(fd, time.monotonic() + START_LOCK_WAIT_S):
            return
        starts = _read_starts(time.time())
        kept = [entry for entry in starts if entry.get("pid") not in pids]
        if len(kept) != len(starts):
            _write_starts(kept)
    finally:
        os.close(fd)  # releases start.lock


def _pid_runs(pid: object) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    if not procid.exists(pid):
        return False
    return procid.start_time(pid) is not None  # a zombie we spawned does not run


#: A child that outlives its parent and never gets its console's Ctrl-C:
#: its own session on POSIX; on Windows its own process group and a console of
#: its own with no window (CREATE_NO_WINDOW, which DETACHED_PROCESS would
#: cancel), so neither it nor the console programs it starts show a window.
_DETACHED: dict = (
    {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}  # Windows-only names
    if os.name == "nt" else {"start_new_session": True}
)
#: Windows: out of the spawner's job too, when the job allows it (a job that
#: kills its processes when it closes would take the service with it).
_BREAKAWAY = 0x01000000  # CREATE_BREAKAWAY_FROM_JOB


def _without_venv_launcher(argv: list, env: dict) -> tuple[list, dict]:
    """Windows: a venv's `Scripts\\python.exe` is CPython's venv launcher, which
    runs the base interpreter as its child inside a job object that kills
    everything in it when the launcher goes (KILL_ON_JOB_CLOSE): the service
    then died with whatever ended its launcher, with no word in its log. Start
    the base interpreter directly, as the launcher does (`__PYVENV_LAUNCHER__`
    names the venv's python, so sys.executable, sys.prefix and site-packages
    are the venv's): no launcher, no job of its own. Anything unexpected: as
    before."""
    python = argv[0]
    try:
        base = _venv_base(os.path.dirname(os.path.dirname(os.path.abspath(python))))
    except OSError:
        return argv, env
    if not base or not os.path.isfile(base) or os.path.normcase(base) == os.path.normcase(os.path.abspath(python)):
        return argv, env
    return [base, *argv[1:]], {**env, "__PYVENV_LAUNCHER__": os.path.abspath(python)}


def _venv_base(venv: str) -> "str | None":
    """The very interpreter a venv's launcher runs, so the service keeps its
    build (a free-threaded python3.13t.exe beside a regular python.exe in one
    `home`, whose native packages the other cannot import): the exact one
    pyvenv.cfg names (CPython 3.11+ `executable`, virtualenv `base-executable`),
    else, for a free-threaded venv (a Scripts\\python3.Nt.exe, or a version
    saying so), that name in `home`, else `home`'s python.exe. None: no
    pyvenv.cfg, or a free-threaded venv whose interpreter is not there (the
    launcher then stays: never the other build)."""
    with open(os.path.join(venv, "pyvenv.cfg"), encoding="utf-8") as handle:
        cfg = {key.strip().lower(): value.strip()
               for key, sep, value in (line.partition("=") for line in handle) if sep}
    for key in ("executable", "base-executable"):
        named = cfg.get(key)
        # Never another venv's launcher (a venv made from inside a venv names
        # that one's Scripts\python.exe), which would set its own prefix.
        if named and os.path.isfile(named) and not os.path.isfile(
                os.path.join(os.path.dirname(os.path.dirname(named)), "pyvenv.cfg")):
            return named
    home = cfg.get("home")
    if not home:
        return None
    try:
        threaded = sorted(n for n in os.listdir(os.path.join(venv, "Scripts"))
                          if n.lower().startswith("python3") and n.lower().endswith("t.exe"))
    except OSError:
        threaded = []
    version = cfg.get("version_info", cfg.get("version", "")).lower()
    if not threaded and ("freethreaded" in version or cfg.get("gil", "").lower() == "disabled"):
        threaded = ["python{}t.exe".format(".".join(version.split("+")[0].split(".")[:2]))]
    if threaded:
        base = os.path.join(home, threaded[0])
        return base if os.path.isfile(base) else None
    return os.path.join(home, "python.exe")


def _spawn(python: str) -> int:
    log_fd = os.open(
        paths.log_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        spawn = dict(
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            cwd=str(paths.service_dir()),
            env=_clean_env(),
            close_fds=True,
            **_DETACHED,
        )
        argv = [python, "-I", "-m", "probe.service"]
        child = None
        if os.name == "nt":
            argv, spawn["env"] = _without_venv_launcher(argv, spawn["env"])
            with contextlib.suppress(OSError):  # the job forbids breaking away: stay in it
                child = subprocess.Popen(argv, **{**spawn, "creationflags": spawn["creationflags"] | _BREAKAWAY})  # noqa: S603
        if child is None:
            child = subprocess.Popen(argv, **spawn)  # noqa: S603 -- the CLI's own interpreter, our own module
    finally:
        os.close(log_fd)
    _children.append(child)
    return child.pid


def no_new_privs() -> bool:
    """Linux: does this process run with no_new_privs (a hardened container,
    a sandboxed agent's process tree)? A service started from here inherits
    it, and every caller then reads as one it cannot rule out being
    sandboxed (`probe.service.caller.landlock_possible`): it would start,
    refuse all work and idle out, again at every hook. Elsewhere, no."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("NoNewPrivs:"):
                    return line.split(":", 1)[1].strip() == "1"
    except OSError:
        return False
    return False


def _lock(fd: int, deadline: float) -> bool:
    while True:
        if paths.try_lock(fd):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def ensure(*, wait: float = 0.0) -> str:
    """Make sure a service runs (an `Ensured` value). Never raises.

    `wait` > 0 polls for the socket that long after a spawn (`probe service
    start`); hooks and taps pass nothing and go on at once.
    """
    _reap()
    probe = connect(0.5)
    if probe is not None:
        probe.close()
        return Ensured.RUNNING
    if no_new_privs():
        return Ensured.CONFINED
    try:
        paths.ensure_dirs()
        fd = os.open(paths.start_lock_path(), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return Ensured.UNAVAILABLE
    outcome = Ensured.STARTING
    try:
        if not _lock(fd, time.monotonic() + START_LOCK_WAIT_S):
            return Ensured.STARTING
        probe = connect(0.5)
        if probe is not None:
            probe.close()
            return Ensured.RUNNING
        now = time.time()
        starts = _read_starts(now)
        last = starts[-1] if starts else None
        if last is not None and now - float(last["at"]) < SPAWN_SETTLE_S and _pid_runs(last.get("pid")):
            outcome = Ensured.STARTING
        elif len(starts) >= MAX_STARTS:
            return Ensured.BACKOFF
        else:
            python = runtime_python()
            if python is None:
                return Ensured.UNAVAILABLE
            try:
                pid = _spawn(python)
            except OSError:
                return Ensured.UNAVAILABLE
            starts.append({"at": now, "pid": pid})
            _write_starts(starts)
            outcome = Ensured.SPAWNED
    finally:
        os.close(fd)  # releases start.lock
    if wait > 0:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            probe = connect(0.5)
            if probe is not None:
                probe.close()
                return Ensured.RUNNING
            time.sleep(0.02)
    return outcome


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
    p_check.add_argument("--trigger", default="cli", choices=("app", "cli", "session", "schedule"))
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


if __name__ == "__main__":
    sys.exit(main())
