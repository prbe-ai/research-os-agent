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

ONE source, byte-identical copies: the tap (`tap/service/`) and, through the
tap's `python -m tap service ...`, pi carry this folder's ten files because
neither can import `probe` (it lives in the CLI's uv tool env, they run under
the system python). `make sync-service-client` copies them under the same
names; `tests/test_service_client_sync.py` fails on drift. Stdlib only,
imports between the ten relative.

There is no switch: the service runs on every Linux and macOS machine with a new enough CLI (Probe does
not run on Windows, service-only D16). A session whose Probe mode is `off` never starts or holds it.
"""

from __future__ import annotations

import contextlib
import json
import socket
import time
from collections.abc import Callable

from . import paths
from .vocabularies import Client
from .wire import API_VERSION, JSONRPC, MAX_LINE_BYTES, Method, encode, request

#: Every socket operation's deadline (plan X6: the registrant's own 2 s).
TIMEOUT_S = 2.0
#: `desktop_request`'s socket deadline: the service's longest (a change, 120 s)
#: and a little more, so its own `timed_out` answer arrives first.
DESKTOP_TIMEOUT_S = 125.0
#: What a training process waits for the service, at most, all told (connect,
#: hello, the call; plan Q5): a hand-off never holds a training loop longer.
HANDOFF_BUDGET_S = 0.25


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
        try:
            return cls._at(paths.socket_path(), timeout)
        except (FileNotFoundError, ConnectionRefusedError) as first:
            try:
                return cls._at(paths.legacy_socket_path(), timeout)
            except (FileNotFoundError, ConnectionRefusedError):
                raise first from None

    @classmethod
    def _at(cls, path, timeout: float) -> Connection:
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
        """Has the service sent something (or closed), within `timeout`? (`select`)"""
        if self._buffer:
            return True
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

    def call(self, method: str, params: dict | None = None, fds=()) -> object:
        """Send one request and return its `result`; ServiceError for an `error`.
        `fds`: file descriptors sent with the request's first bytes (SCM_RIGHTS)."""
        self._next_id += 1
        id_ = self._next_id
        line = encode(request(id_, method, params))
        if fds:
            sent = socket.send_fds(self.sock, [line], list(fds))
            line = line[sent:]
        if line:
            self.sock.sendall(line)
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
