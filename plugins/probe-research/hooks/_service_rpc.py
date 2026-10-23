"""A hook's line to the Probe service (plan Q2): one JSON-RPC connection.

The reads hook claims the daemon's messages through it (`mailbox.claim`) and
waits on it for a wake (`events.subscribe`); the approvals hook records an
answer through it (`mailbox.answer`). Stdlib only, Python 3.9, loaded beside
the hook by explicit path. Nothing here raises past `connect` (None: no
service, a folder that is not this user's alone, a refused hello) and `call`
(`Failed`): every caller then does the step on the files itself.

`PROBE_HOOK_SERVICE=off` (or the older `PROBE_READS_SERVICE=off`): never ask.
"""

from __future__ import annotations

import json
import os
import sys
import time

#: Connect and hello, at most.
CONNECT_S = 0.3
SERVICE_ENVS = ("PROBE_HOOK_SERVICE", "PROBE_READS_SERVICE")
MAX_LINE = 1 << 20


class Failed(Exception):
    """The call did not complete: `answered` says whether the service sent an
    error answer (it did nothing) or the line broke or timed out (it may have)."""

    def __init__(self, message: str, *, answered: bool) -> None:
        super().__init__(message)
        self.answered = answered


def _paths():
    import importlib.util  # noqa: PLC0415

    key = "_probe_hooks._service_paths"
    if key in sys.modules:
        return sys.modules[key]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_service_paths.py")
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def enabled() -> bool:
    return os.name == "posix" and not any(os.environ.get(name) == "off" for name in SERVICE_ENVS)


class Rpc:
    def __init__(self, sock) -> None:
        self.sock = sock
        self.buffer = b""
        self._next = 0

    @classmethod
    def connect(cls, timeout: float = CONNECT_S) -> "Rpc | None":
        """A connection that said hello, or None."""
        if not enabled():
            return None
        import socket  # noqa: PLC0415

        sock = None
        try:
            paths = _paths()
            path = paths.socket_path()
            paths.require_private(path.parent)
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            sock.connect(str(path))
            rpc = cls(sock)
            rpc.call("service.hello", {"client": "cli", "client_version": "hooks", "api": 1}, timeout=timeout)
            return rpc
        except Exception:  # noqa: BLE001 -- no service, an older one: the caller works on the files
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            return None

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def line(self) -> "dict | None":
        """One complete message from what was read, or None."""
        if b"\n" not in self.buffer:
            if len(self.buffer) > MAX_LINE:
                raise ConnectionError("the service sent a line over 1 MiB")
            return None
        raw, self.buffer = self.buffer.split(b"\n", 1)
        try:
            message = json.loads(raw.decode("utf-8"))
        except ValueError:
            return {}
        return message if isinstance(message, dict) else {}

    def read(self) -> None:
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ConnectionError("the service closed the connection")
        self.buffer += chunk

    def call(self, method: str, params: dict, *, timeout: float) -> dict:
        """`method`'s result (an object), within `timeout`; `Failed` else."""
        self._next += 1
        ident = self._next
        deadline = time.monotonic() + timeout
        try:
            self.sock.settimeout(max(0.01, timeout))
            self.sock.sendall((json.dumps({"jsonrpc": "2.0", "id": ident, "method": method, "params": params})
                               + "\n").encode("utf-8"))
            while True:
                message = self.line()
                if message is None:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError(f"{method}: no answer in {timeout:g} s")
                    self.sock.settimeout(left)
                    self.read()
                    continue
                if message.get("id") != ident:
                    continue  # an event, or an answer to an earlier call
                if "error" in message:
                    raise Failed(f"{method}: {message.get('error')}", answered=True)
                result = message.get("result")
                return result if isinstance(result, dict) else {}
        except Failed:
            raise
        except Exception as exc:  # noqa: BLE001 -- the line broke, or timed out
            raise Failed(f"{method}: {exc!r}", answered=False) from None
