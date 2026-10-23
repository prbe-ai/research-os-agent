"""The warm hook host: this plugin's dispatcher, loaded once, forked per event (plan A0, D34).

    python3 -S hooks/zygote.py <socket> <service pid>

The Probe service starts one per installed plugin folder that sessions use
(`probe.service.hookhosts`) and stops it with itself. `dispatch.sh` -> `dispatch.py`
hands each event here instead of running it, when this answers in time
(`dispatch.via_host`): the event runs in a child forked from this process, so
it starts with the dispatcher, the routes' shared modules and their bytecode
already loaded, in exactly the state a fresh `python3 -S` would have them --
the parent never runs an event itself, so nothing one event leaves behind
reaches the next.

The child is the event's own process, as today's dispatcher is: the harness's
stdout and stderr (passed over the socket, SCM_RIGHTS), the session's
environment and working folder, the payload on stdin; the dispatcher then
runs the routes exactly as it would have (in-process, or as children, with
their budgets). Only its parent differs, so the harness's pid travels as
PROBE_HOOK_PARENT_PID for the one route that walks up to its agent
(`_session_marker._ancestors`).

Exactly once: the client sends the event; this answers `R` (ready); the client
answers `G` (go) only while it is still within its 300 ms (D28); only then is
the child forked, and this says `S` (started). A host that will not fork it
(stopping, or fork failed) says `N` instead and the event runs where the
client is. A client that gave up before `R` reached it closed the connection,
and the event runs there instead, never twice. After `S` the event is this
host's: the client waits for its exit status, and a host that dies meanwhile
reads as exit 0 (fail open, as every Probe hook).

Never for a caller in a sandbox (`_sandboxed`: an agent's tool command,
seccomp/no-new-privs/namespaces on Linux, the macOS sandbox): the event would
run outside it, with an environment and PATH that caller chose. Closed at
once; the event runs where it is.

Wire, one connection per event:

    client -> host   {"v": 1, "argv": [...], "cwd", "env": {...}, "parent": pid,
                      "deadline": epoch, "payload": N}\\n  + N payload bytes,
                     with the harness's stdout and stderr fds on the first send
    host -> client   R
    client -> host   G
    host -> client   S (or N: run it yourself)
    host -> client   {"exit": code}\\n

Exits by itself: idle IDLE_S, its service gone, or this folder's dispatcher or
routes changed (a new copy runs in a new host). Stdlib only, Python 3.9.
"""

from __future__ import annotations

import errno
import json
import os
import select
import signal
import socket
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
IDLE_S = 15 * 60
#: A request's header and payload together, at most (a huge tool result).
MAX_REQUEST = 64 * 1024 * 1024
#: The client has this long, after `R`, to say `G`.
GO_WAIT_S = 1.0
#: Past an event's deadline its child (and every process in its group) is killed.
KILL_GRACE_S = 1.0
#: Modules every event's routes import, loaded once here.
PRELOAD = ("json", "re", "subprocess", "threading", "signal", "runpy", "io", "hashlib",
           "_session_marker", "_hook_harness", "harness_io", "_probe_config", "_telemetry_core",
           "_harness_registry")
#: What a child changes when it starts: this host's own files, so a change shows.
WATCHED = ("dispatch.py", "routes.json", "zygote.py")


def _mtimes() -> tuple:
    out = []
    for name in WATCHED:
        try:
            out.append(os.stat(os.path.join(HERE, name)).st_mtime_ns)
        except OSError:
            out.append(None)
    return tuple(out)


def _peer_pid(conn: socket.socket):
    """The connecting process's pid (Linux SO_PEERCRED, macOS LOCAL_PEERPID), or None."""
    try:
        if sys.platform.startswith("linux"):
            import struct

            pid, _uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            return pid if pid > 0 else None
        if sys.platform == "darwin":
            import struct

            raw = conn.getsockopt(0, 0x002, 4)  # SOL_LOCAL, LOCAL_PEERPID
            pid = struct.unpack("i", raw)[0] if isinstance(raw, bytes) else int(raw)
            return pid if pid > 0 else None
    except (OSError, ValueError, AttributeError):
        return None
    return None


def _confinement(pid) -> "tuple | None":
    """What confines a process: (seccomp, its filter count, no_new_privs, mnt ns, user ns) on
    Linux, (sandboxed,) on macOS; None when it cannot be read."""
    try:
        if sys.platform.startswith("linux"):
            fields = {}
            with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
                for line in handle:
                    key, _, value = line.partition(":")
                    fields[key] = value.strip()
            # The filter count too: a sandbox inside a container that already
            # runs everything under seccomp and no-new-privs adds filters of its own.
            return (fields.get("Seccomp"), fields.get("Seccomp_filters"), fields.get("NoNewPrivs"),
                    os.readlink(f"/proc/{pid}/ns/mnt"), os.readlink(f"/proc/{pid}/ns/user"))
        if sys.platform == "darwin":
            import ctypes

            lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
            lib.sandbox_check.restype = ctypes.c_int
            lib.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
            return (lib.sandbox_check(int(pid), None, 0) != 0,)
    except Exception:  # noqa: BLE001 - unknown: treated as confined
        return None
    return None


_OWN_CONFINEMENT: list = []


def _sandboxed(conn: socket.socket) -> bool:
    """Is the caller more confined than this host (an agent's sandboxed tool
    command)? Unknown counts as yes: the event then runs where it is, as before."""
    pid = _peer_pid(conn)
    if pid is None:
        return True
    if not _OWN_CONFINEMENT:
        _OWN_CONFINEMENT.append(_confinement(os.getpid()))
    own, theirs = _OWN_CONFINEMENT[0], _confinement(pid)
    # A Landlock domain shows in none of those fields, and a process with
    # no_new_privs may add one at any time: such a caller cannot be told from
    # a sandboxed one (`probe.service.caller.landlock_possible`).
    landlock = sys.platform.startswith("linux") and theirs is not None and len(theirs) >= 3 and theirs[2] == "1"
    return own is None or theirs is None or theirs != own or landlock


def _private_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if not os.path.isdir(path) or os.path.islink(path) or st.st_uid != os.getuid():
        raise OSError(f"{path} is not a private folder of this user")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)


class Pending:
    """One connection: reading its request, waiting for `G`, or waiting for its child."""

    def __init__(self, conn: socket.socket) -> None:
        self.conn = conn
        self.buffer = b""
        self.fds: list = []
        self.header: dict | None = None
        self.payload = b""
        self.state = "reading"
        self.since = time.monotonic()
        self.child: int | None = None
        self.deadline = 0.0

    def close(self) -> None:
        for fd in self.fds:
            try:
                os.close(fd)
            except OSError:
                pass
        self.fds = []
        try:
            self.conn.close()
        except OSError:
            pass


def _child(pending: Pending, listener: socket.socket, others: list) -> None:
    """The event's own process. Never returns."""
    code = 0
    try:
        os.setsid()
        # As a fresh `python3` starts: SIGPIPE ignored (a route that exits
        # without reading its stdin must not kill the dispatcher feeding it),
        # SIGINT raising KeyboardInterrupt, the rest at their defaults.
        for sig in (signal.SIGCHLD, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_DFL)
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.default_int_handler)
        signal.set_wakeup_fd(-1)
        listener.close()
        for other in others:
            if other is not pending:
                # Its connection AND the stdout/stderr it was handed: a copy held
                # here would keep another event's pipe open until this one exits.
                try:
                    other.conn.close()
                except OSError:
                    pass
                for fd in other.fds:
                    if fd > 2:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                other.fds = []
        out, err = (pending.fds + [-1, -1])[:2]
        if out >= 0:
            os.dup2(out, 1)
        if err >= 0:
            os.dup2(err, 2)
        null = os.open(os.devnull, os.O_RDONLY)
        os.dup2(null, 0)
        for fd in pending.fds + [null]:
            if fd > 2:
                try:
                    os.close(fd)
                except OSError:
                    pass
        pending.conn.close()
        header = pending.header or {}
        env = header.get("env")
        if isinstance(env, dict):
            os.environ.clear()
            os.environ.update({str(k): str(v) for k, v in env.items()})
        parent = header.get("parent")
        if isinstance(parent, int) and parent > 1:
            os.environ["PROBE_HOOK_PARENT_PID"] = str(parent)
        # What a fresh process reads from its environment at start.
        if hasattr(time, "tzset"):
            time.tzset()
        telemetry = sys.modules.get("_telemetry_core")
        if telemetry is not None and hasattr(telemetry, "POSTHOG_HOST"):
            telemetry.POSTHOG_HOST = os.environ.get("PROBE_TELEMETRY_HOST") or "https://us.i.posthog.com"
        cwd = header.get("cwd")
        try:
            os.chdir(cwd if isinstance(cwd, str) and cwd else os.path.expanduser("~"))
        except OSError:
            os.chdir("/")
        # Reopen the standard streams on the new descriptors.
        sys.stdout = os.fdopen(1, "w", closefd=False)
        sys.stderr = os.fdopen(2, "w", buffering=1, errors="backslashreplace", closefd=False)
        import dispatch  # loaded already (`serve`)

        argv = [str(a) for a in header.get("argv") or []]
        dispatch.run_hosted(argv, pending.payload)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 0
    except BaseException:  # noqa: BLE001 - as an uncaught error ends `python3 <route>`
        # The dispatcher's own failures are caught inside it (fail open); what
        # reaches here is a route's, run in-process: its traceback and exit 1,
        # exactly as the dispatcher run here would end.
        try:
            import traceback

            traceback.print_exc()
        except Exception:  # noqa: BLE001
            pass
        code = 1
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(code if isinstance(code, int) else 0)


def _refuse(pending: Pending) -> None:
    """`N`: the client runs the event itself (after its `G`, or instead of one)."""
    try:
        pending.conn.setblocking(True)
        pending.conn.settimeout(1.0)
        pending.conn.sendall(b"N")  # answers a `G` on its way too
    except OSError:
        pass
    pending.close()


def _read(pending: Pending) -> bool:
    """Take what arrived; True when the connection is finished (closed or bad)."""
    try:
        data, fds, _flags, _addr = socket.recv_fds(pending.conn, 1 << 20, 4)
    except (BlockingIOError, InterruptedError):
        return False
    except OSError:
        return True
    pending.fds.extend(fds)
    if not data:
        return True
    if pending.state == "go":
        if data[:1] == b"G":
            pending.state = "go!"
        return pending.state != "go!"
    pending.buffer += data
    if len(pending.buffer) > MAX_REQUEST:
        return True
    if pending.header is None:
        line, sep, rest = pending.buffer.partition(b"\n")
        if not sep:
            return False
        try:
            header = json.loads(line.decode("utf-8"))
        except ValueError:
            return True
        if not isinstance(header, dict) or header.get("v") != 1:
            return True
        pending.header = header
        pending.buffer = rest
    want = pending.header.get("payload")
    if not isinstance(want, int) or want < 0 or want > MAX_REQUEST:
        return True
    if len(pending.buffer) >= want:
        pending.payload = pending.buffer[:want]
        pending.buffer = b""
        try:
            pending.conn.sendall(b"R")
        except OSError:
            return True
        pending.state = "go"
        pending.since = time.monotonic()
    return False


def serve(sock_path: str, service_pid: int) -> int:
    sys.path[0] = HERE
    for name in PRELOAD:
        try:
            __import__(name)
        except Exception:  # noqa: BLE001 - a module a route lacks costs only its own preload
            pass
    import dispatch  # noqa: F401 - warm: compiled and imported once, for every child

    _private_dir(os.path.dirname(sock_path))
    try:
        os.unlink(sock_path)
    except FileNotFoundError:
        pass
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old = os.umask(0o177)
    try:
        listener.bind(sock_path)
    finally:
        os.umask(old)
    listener.listen(64)
    listener.setblocking(False)
    bound = os.stat(sock_path)
    wake_r, wake_w = os.pipe()
    os.set_blocking(wake_w, False)
    signal.set_wakeup_fd(wake_w)
    signal.signal(signal.SIGCHLD, lambda *_: None)
    stop = []
    signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
    signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    stamps = _mtimes()
    pendings: list = []
    last = time.monotonic()
    try:
        while not stop:
            readers = [listener, wake_r] + [p.conn for p in pendings if p.state in ("reading", "go")]
            try:
                ready, _, _ = select.select(readers, [], [], 0.5)
            except InterruptedError:
                ready = []
            now = time.monotonic()
            if wake_r in ready:
                try:
                    os.read(wake_r, 4096)
                except OSError:
                    pass
            if listener in ready:
                while True:
                    try:
                        conn, _ = listener.accept()
                    except (BlockingIOError, InterruptedError):
                        break
                    except OSError:
                        break
                    if _sandboxed(conn):
                        conn.close()  # the event runs where it is
                        continue
                    conn.setblocking(False)
                    pendings.append(Pending(conn))
                    last = now
            for pending in list(pendings):
                if pending.conn in ready and pending.state in ("reading", "go"):
                    if _read(pending):
                        pending.close()
                        pendings.remove(pending)
                        continue
                if pending.state == "go!":
                    header = pending.header or {}
                    deadline = header.get("deadline")
                    pending.deadline = float(deadline) if isinstance(deadline, (int, float)) else time.time() + 60
                    try:
                        pid = os.fork()
                    except OSError:
                        _refuse(pending)  # no process to run it in: it runs where the client is
                        pendings.remove(pending)
                        continue
                    if pid == 0:
                        _child(pending, listener, pendings)
                    pending.child = pid
                    pending.state = "running"
                    try:
                        pending.conn.sendall(b"S")
                    except OSError:
                        pass
                    for fd in pending.fds:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                    pending.fds = []
                elif pending.state in ("reading", "go") and now - pending.since > GO_WAIT_S + 5:
                    pending.close()
                    pendings.remove(pending)
            # Children that ended, and children past their deadline.
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                except ChildProcessError:
                    break
                if pid == 0:
                    break
                code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)
                code = 0 if code < 0 else code
                for pending in list(pendings):
                    if pending.child == pid:
                        try:
                            pending.conn.setblocking(True)
                            pending.conn.settimeout(1.0)
                            pending.conn.sendall((json.dumps({"exit": code}) + "\n").encode())
                        except OSError:
                            pass
                        pending.close()
                        pendings.remove(pending)
            wall = time.time()
            for pending in pendings:
                if pending.state == "running" and pending.child and wall > pending.deadline + KILL_GRACE_S:
                    try:
                        os.killpg(pending.child, signal.SIGKILL)
                    except OSError:
                        pass
            if not pendings and now - last > IDLE_S:
                break
            if service_pid > 1:
                try:
                    os.kill(service_pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    pass
            if _mtimes() != stamps:
                break
    finally:
        try:
            st = os.stat(sock_path)
            if (st.st_dev, st.st_ino) == (bound.st_dev, bound.st_ino):
                os.unlink(sock_path)
        except OSError:
            pass
        listener.close()
        # An event told to go, or about to be, that was not forked yet runs
        # where its client is: never closed unread (it would be lost).
        for pending in [p for p in pendings if p.state in ("go", "go!")]:
            _refuse(pending)
            pendings.remove(pending)
        # Events already handed here run to their end.
        deadline = time.time() + 30
        while any(p.state == "running" for p in pendings) and time.time() < deadline:
            try:
                pid, status = os.waitpid(-1, 0)
            except ChildProcessError:
                break
            for pending in list(pendings):
                if pending.child == pid:
                    code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)
                    try:
                        pending.conn.setblocking(True)
                        pending.conn.sendall((json.dumps({"exit": max(code, 0)}) + "\n").encode())
                    except OSError:
                        pass
                    pending.close()
                    pendings.remove(pending)
        for pending in pendings:
            pending.close()
    return 0


def main(argv: list) -> int:
    if len(argv) < 2:
        print("usage: zygote.py <socket> <service pid>", file=sys.stderr)
        return 2
    try:
        service_pid = int(argv[1]) if len(argv) > 1 else 0
    except ValueError:
        service_pid = 0
    try:
        return serve(argv[0], service_pid)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            return 0
        print(f"zygote: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
