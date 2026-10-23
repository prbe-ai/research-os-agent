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
                Never start the service: each is for a service that already
                runs (OSError when none answers), and the caller falls back.
    call()      any method, for the CLI and tests.
    Connection  a kept connection: the tap's registration thread holds one, so
                a restarted service is noticed (EOF) and re-told at once.

ONE source, byte-identical copies: the tap (`tap/service/client.py`) and,
through the tap's `python -m tap service ...`, pi carry this file because
neither can import `probe` (it lives in the CLI's uv tool env, they run under
the system python). `make sync-service-client` copies it with `paths.py`,
`procid.py` and `protocol.py`; `tests/test_service_client_sync.py` fails on
drift. Stdlib only, imports between the four relative.

There is no switch: the service runs on every POSIX machine with a new
enough CLI. A session whose Probe mode is `off` never starts or holds it.
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

from . import paths, procid
from .protocol import API_VERSION, JSONRPC, MAX_LINE_BYTES, Client, Method, Mode, encode, request


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

#: The only variables the service starts with (plan X2): never an agent's
#: PROBE_TOKEN, endpoint or PYTHONPATH, which would leak the first launcher's
#: context into every later client's requests. The PROBE_SERVICE_* are
#: test-only overrides of the lifecycle and updater timers.
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
    "PROBE_SERVICE_LEGACY_RETRY_S",
    "PROBE_SERVICE_RECORDER_RETRY_S",
    "PROBE_SERVICE_KEEPER",
)


class Ensured:
    """What `ensure` did."""

    RUNNING = "running"  # a service answered
    SPAWNED = "spawned"  # none did; this call started one
    STARTING = "starting"  # another caller started one moments ago
    BACKOFF = "backoff"  # MAX_STARTS in START_WINDOW_S already: not again yet
    UNAVAILABLE = "unavailable"  # no interpreter to start it with


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
) -> dict:
    params = {"sid": sid, "harness": harness, "mode": mode, "owner": owner, "generation": generation}
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


def wait_for_exit(pid: int, *, timeout: float = WAIT_FOR_PID_S, poll: float = 0.25) -> bool:
    """Until `pid` is gone (True), or `timeout` passed (False).

    The CLI asks for an update from its root callback, before its own command
    runs, and imports lazily all the way through it: an update that started
    now would replace files that command is still reaching for (a
    `ModuleNotFoundError`; `probe._shared.autoupdate.wait_for_pid_exit`). The
    barrier stops new starts only, so the asking process waits it out here."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            pass  # exists, not ours to signal: still there
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
        os.replace(tmp, target)
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
        os.replace(tmp, target)
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
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return procid.start_time(pid) is not None  # a zombie we spawned does not run


def _spawn(python: str) -> int:
    log_fd = os.open(
        paths.log_path(), os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    try:
        child = subprocess.Popen(  # noqa: S603 -- the CLI's own interpreter, our own module
            [python, "-I", "-m", "probe.service"],
            stdin=subprocess.DEVNULL,
            stdout=log_fd,
            stderr=log_fd,
            cwd=str(paths.service_dir()),
            env=_clean_env(),
            start_new_session=True,
            close_fds=True,
        )
    finally:
        os.close(log_fd)
    _children.append(child)
    return child.pid


def _lock(fd: int, deadline: float) -> bool:
    import fcntl

    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
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
    # itself (pi's tool guard, `hooks.run`): the machine key needs the OS's
    # machine id, which pi would otherwise have to work out again.
    sub.add_parser("socket", help="print this machine's service socket path")
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
        print(json.dumps({"socket": str(paths.socket_path())}))
        return 0
    try:
        if args.cmd == "register":
            if args.ensure:
                ensure(wait=3.0)
            owner = owner_of(args.owner_pid) if args.owner_pid else None
            answer = register(
                args.session_id, args.harness, args.mode, owner, args.generation,
                client=client, client_version=client_version,
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
