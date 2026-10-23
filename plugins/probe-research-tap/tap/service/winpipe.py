"""Windows named pipes for the Probe service (plan R3, D17).

    \\\\.\\pipe\\probe-service-<machine key>-<user hash>

Windows has no UNIX sockets Python can use, so the service listens on a named
pipe instead, and only this user may open it: the pipe's DACL grants the
current user (and SYSTEM) and nobody else, and the first instance is created
with FILE_FLAG_FIRST_PIPE_INSTANCE, so a pipe someone made first under the
name makes the service refuse to start rather than share it. A client checks
the other way round too: the process serving the pipe must run as this user
(`GetNamedPipeServerProcessId` + its token), or it closes the connection.

    client   `connect(name, timeout)` -> a `PipeStream`: the socket calls
             the client uses (settimeout, recv, sendall, close, fileno) over
             `multiprocessing.connection`'s pipe connection (message mode)
    server   `serve(loop, name, client_connected_cb)`: asyncio's proactor pipe
             server, its pipes made by `create_pipe` with that DACL
    peers    `client_pid(handle)`, `server_pid(handle)`, `process_sid(pid)`

Stdlib only (ctypes), and synced into the tap with the other client files.
Imported on Windows only.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import socket
import time
from ctypes import wintypes


PIPE_ACCESS_DUPLEX = 0x00000003
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_TYPE_MESSAGE = 0x00000004
PIPE_READMODE_MESSAGE = 0x00000002
PIPE_WAIT = 0x00000000
PIPE_REJECT_REMOTE_CLIENTS = 0x00000008
PIPE_UNLIMITED_INSTANCES = 255
BUFSIZE = 65536
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
TOKEN_QUERY = 0x0008
TOKEN_USER = 1
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SDDL_REVISION_1 = 1


class SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID),
                ("bInheritHandle", wintypes.BOOL)]


# Windows only: the module imports anywhere (the suite imports every module),
# and is used only where `os.name == "nt"`.
if os.name == "nt":
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                          wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                          ctypes.POINTER(SECURITY_ATTRIBUTES)]
    kernel32.CreateNamedPipeW.restype = wintypes.HANDLE
    kernel32.GetNamedPipeClientProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
    kernel32.GetNamedPipeServerProcessId.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.ULONG)]
    kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    kernel32.LocalFree.restype = wintypes.HLOCAL
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                                             ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.ULONG)]
    advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL

def _token_sid(process: int) -> str | None:
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(process, TOKEN_QUERY, ctypes.byref(token)):
        return None
    try:
        size = wintypes.DWORD(0)
        advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
        if not size.value:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_USER, buf, size, ctypes.byref(size)):
            return None
        sid = ctypes.cast(buf, ctypes.POINTER(wintypes.LPVOID))[0]  # TOKEN_USER.User.Sid
        text = wintypes.LPWSTR()
        if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            kernel32.LocalFree(ctypes.cast(text, wintypes.HLOCAL))
    finally:
        kernel32.CloseHandle(token)


_user_sid: str | None = None


def user_sid() -> str:
    """This process's user, as a SID string (`S-1-5-21-...`)."""
    global _user_sid
    if _user_sid is None:
        _user_sid = _token_sid(kernel32.GetCurrentProcess()) or "unknown"
    return _user_sid


def process_sid(pid: int) -> str | None:
    """The user process `pid` runs as, or None when it cannot be read."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return None
    try:
        return _token_sid(handle)
    finally:
        kernel32.CloseHandle(handle)


def pipe_name(machine_key: str, folder: str = "") -> str:
    """This user's service pipe on this machine, for the service folder
    `folder` (as the POSIX socket lives in it: two homes never share one)."""
    who = hashlib.sha256(f"{user_sid()}\0{folder}".encode("utf-8")).hexdigest()[:12]
    return rf"\\.\pipe\probe-service-{machine_key}-{who}"


_security: list = []


def _attributes() -> SECURITY_ATTRIBUTES:
    """A DACL granting this user and SYSTEM alone (protected: nothing inherited)."""
    if not _security:
        sd = wintypes.LPVOID()
        sddl = f"D:P(A;;GA;;;{user_sid()})(A;;GA;;;SY)"
        if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, SDDL_REVISION_1,
                                                                            ctypes.byref(sd), None):
            raise ctypes.WinError(ctypes.get_last_error())
        attrs = SECURITY_ATTRIBUTES()
        attrs.nLength = ctypes.sizeof(SECURITY_ATTRIBUTES)
        attrs.lpSecurityDescriptor = sd
        attrs.bInheritHandle = False
        _security.append(attrs)  # kept for the process's life: every instance uses it
    return _security[0]


def create_pipe(name: str, first: bool) -> int:
    """One overlapped, message-mode instance of `name`, this user's alone."""
    flags = PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED | (FILE_FLAG_FIRST_PIPE_INSTANCE if first else 0)
    mode = PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS
    handle = kernel32.CreateNamedPipeW(name, flags, mode, PIPE_UNLIMITED_INSTANCES, BUFSIZE, BUFSIZE, 0,
                                       ctypes.byref(_attributes()))
    if handle in (None, INVALID_HANDLE_VALUE):
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def client_pid(handle: int) -> int | None:
    pid = wintypes.ULONG(0)
    if not kernel32.GetNamedPipeClientProcessId(handle, ctypes.byref(pid)):
        return None
    return pid.value or None


def server_pid(handle: int) -> int | None:
    pid = wintypes.ULONG(0)
    if not kernel32.GetNamedPipeServerProcessId(handle, ctypes.byref(pid)):
        return None
    return pid.value or None


# -- the server ----------------------------------------------------------------------


def _patch_pipe_server() -> None:
    """asyncio's proactor makes its pipes with no security attributes (the
    default DACL lets Everyone read); make them with ours."""
    import asyncio.windows_events as we
    from asyncio import windows_utils

    if getattr(we.PipeServer, "_probe_dacl", False):
        return

    class PipeServer(we.PipeServer):
        _probe_dacl = True

        def _server_pipe_handle(self, first):
            if self.closed():
                return None
            pipe = windows_utils.PipeHandle(create_pipe(self._address, first))
            self._free_instances.add(pipe)
            return pipe

    we.PipeServer = PipeServer


async def serve(loop, name: str, client_connected_cb, *, limit: int):
    """Serve `name` on the proactor loop; `client_connected_cb(reader, writer)`
    as `asyncio.start_unix_server` calls it. Returns the server (`close()`)."""
    import asyncio

    _patch_pipe_server()

    def factory():
        reader = asyncio.StreamReader(limit=limit, loop=loop)
        return asyncio.StreamReaderProtocol(reader, client_connected_cb, loop=loop)

    # A client still holding the last service's pipe keeps its name alive a
    # moment, and the first instance is refused (ACCESS_DENIED) meanwhile;
    # this service holds `service.lock`, so no other one is starting.
    deadline = time.monotonic() + 2.0
    while True:
        try:
            servers = await loop.start_serving_pipe(factory, name)
            break
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(0.1)
    return servers[0] if isinstance(servers, list) else servers


def peer_of(writer) -> int | None:
    """The connecting process of a served pipe (from its transport)."""
    pipe = writer.get_extra_info("pipe")
    handle = getattr(pipe, "handle", None) if pipe is not None else None
    return client_pid(handle) if handle else None


# -- the client ----------------------------------------------------------------------


class PipeStream:
    """The socket calls `client.Connection` makes, on a pipe connection."""

    def __init__(self, conn) -> None:
        self._conn = conn
        self._timeout: float | None = None
        self._pending = b""

    def settimeout(self, value: float | None) -> None:
        self._timeout = value

    def gettimeout(self) -> float | None:
        return self._timeout

    def fileno(self) -> int:
        return self._conn.fileno()

    def readable(self, timeout: float) -> bool:
        return bool(self._pending) or self._conn.poll(max(0.0, timeout))

    def recv(self, size: int) -> bytes:
        if not self._pending:
            if self._timeout is not None and not self._conn.poll(max(0.0, self._timeout)):
                raise socket.timeout("timed out")
            try:
                self._pending = self._conn.recv_bytes()
            except (EOFError, BrokenPipeError):
                return b""
        data, self._pending = self._pending[:size], self._pending[size:]
        return data

    def sendall(self, data: bytes) -> None:
        """Write `data`, within the timeout as a socket would: a service that
        stops reading never holds a caller past it (`socket.timeout`)."""
        import _winapi

        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        for start in range(0, len(data), BUFSIZE):
            chunk = data[start:start + BUFSIZE]
            ov, err = _winapi.WriteFile(self._conn.fileno(), chunk, overlapped=True)
            timed_out = False
            try:
                if err == _winapi.ERROR_IO_PENDING:
                    # One deadline for the whole write, not one per chunk.
                    wait_ms = (_winapi.INFINITE if deadline is None
                               else max(1, int((deadline - time.monotonic()) * 1000)))
                    if _winapi.WaitForMultipleObjects([ov.event], False, wait_ms) != _winapi.WAIT_OBJECT_0:
                        timed_out = True
                        ov.cancel()
            except BaseException:
                ov.cancel()
                raise
            finally:
                written, err = ov.GetOverlappedResult(True)
            if timed_out or err == _winapi.ERROR_OPERATION_ABORTED:
                raise socket.timeout("timed out")
            if err or written != len(chunk):
                raise BrokenPipeError(f"pipe write failed ({err})")

    def close(self) -> None:
        self._conn.close()


def connect(name: str, timeout: float) -> PipeStream:
    """Connect to this user's service pipe, or raise FileNotFoundError (none
    serves), PermissionError (another user's process serves it) or OSError."""
    from multiprocessing.connection import PipeConnection
    import _winapi

    deadline = time.monotonic() + max(0.05, timeout)
    while True:
        try:
            _winapi.WaitNamedPipe(name, max(1, int((deadline - time.monotonic()) * 1000)))
            handle = _winapi.CreateFile(name, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0, _winapi.NULL,
                                        _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED, _winapi.NULL)
            break
        except OSError as exc:
            if getattr(exc, "winerror", None) == 2:  # ERROR_FILE_NOT_FOUND: no service
                raise FileNotFoundError(2, "no Probe service is listening", name) from None
            if getattr(exc, "winerror", None) not in (121, 231) or time.monotonic() >= deadline:
                raise  # not ERROR_SEM_TIMEOUT / ERROR_PIPE_BUSY, or out of time
    try:
        _winapi.SetNamedPipeHandleState(handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
        owner = server_pid(handle)
        if owner is None or process_sid(owner) != user_sid():
            raise PermissionError(f"{name} is served by a process of another user")
    except BaseException:
        _winapi.CloseHandle(handle)
        raise
    stream = PipeStream(PipeConnection(handle))
    stream.settimeout(timeout)
    return stream
