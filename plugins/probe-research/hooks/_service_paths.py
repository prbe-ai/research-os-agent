"""Where the Probe service keeps its files (API v1, "Transport" and "Lifecycle").

ONE service per user per MACHINE, and its identity is a place on disk: the
default state dir under `$HOME`, keyed by this machine,
`~/.local/state/probe/service/<machine key>/` (plan D8). Deliberately NOT
`$XDG_STATE_HOME`: every client (an agent's hook, the desktop app, a terminal)
carries its own environment, and two clients that disagreed about
`XDG_STATE_HOME` would start two services and each see half the sessions
(plan X2). `HOME` and the machine are the two things they all agree on.

Keyed by machine because a cluster's nodes often share one home folder (NFS):
one folder for all of them gave one node a service and left the others'
sessions out of its sight. The key is a hash of the OS's own machine id
(Linux `/etc/machine-id`, macOS's hardware UUID), never the hostname, which
changes on a Mac with its network; the hostname only when neither exists.
The desktop app computes the same key (prbe-ai/desktop `src-tauri/src/service.rs`;
pi reaches the service through the tap's copy of this file): change both together.

Two files stay in the shared folder above the machine's (`root_dir`), because
what they describe is the install, not the machine: `runtime.json` (the CLI's
interpreter) and `app-update.json` (the desktop app's report, written at a
path the app fixed before this key existed). `legacy_socket_path` and
`legacy_lock_path` are where clients from before the key look: the service
serves there too while it can take that folder's lock (`server.py`), so a
session started before an update keeps reaching it.

Stdlib only, and synced byte-for-byte into the tap (`tap/service/paths.py`,
`make sync-service-client`): the tap runs under the system python with no
`probe` importable. Imports between the synced files stay relative.
"""

from __future__ import annotations

import hashlib
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

#: Windows (plan R3): the service's folder is under %LOCALAPPDATA% (private to
#: the user by default), and it listens on a named pipe, not a UNIX socket
#: (`pipe_name`, `winpipe.py`).
WINDOWS = os.name == "nt"

#: A UNIX socket path is at most 108 bytes on Linux and 104 on macOS; past
#: this the socket moves to a private folder under /tmp (API v1, "Transport").
MAX_SOCKET_PATH = 100

SERVICE_DIRNAME = "service"
SOCKET_NAME = "service.sock"
#: Held (flock) for the whole life of the one running service.
LOCK_NAME = "service.lock"
#: Held (flock) only while a client decides whether to spawn a service.
START_LOCK_NAME = "start.lock"
#: The spawns `ensure` made lately, for its back-off (3 in 60 s).
STARTS_NAME = "starts.json"
REGISTRY_NAME = "sessions.json"
LOG_NAME = "service.log"
#: `{"python": "<abs path>", "cli_version": "..."}`: the CLI's own interpreter,
#: written by the CLI, read by every client that may have to spawn the service.
RUNTIME_NAME = "runtime.json"
#: `<action_id>.jsonl` (each action's journal) and `<action_id>.log` (its
#: child's stderr): what `action.status` / `action.events` answer from, also
#: after the service restarted (plan X5).
ACTIONS_DIRNAME = "actions"
#: Present while the service applies a CLI or plugin update: every CLI start
#: and daemon worker start waits for it to go (plan X1; `barrier.py`).
BARRIER_NAME = "update.barrier"
#: The updater's own record: when it last checked, what came of it.
UPDATE_STATE_NAME = "update.json"
#: Written by the desktop app's `--background-update` run (prbe-ai/desktop #11).
APP_UPDATE_NAME = "app-update.json"
#: Set on every `probe` this service starts itself (`children.cli_argv`, with
#: `children.cli_env`): such a command never asks the service for an update
#: (`probe.cli.main._hand_update_to_service`). The service is the updater, and
#: an ask from one command of a job would land while the job's next command
#: still runs from the files an update replaces. Here, not in `children`: the
#: CLI reads it on its every-command path, which has loaded this module
#: already and must not pay for `children`'s asyncio import.
SERVICE_CHILD_ENV = "PROBE_SERVICE_CHILD"
#: Hook events' record-keeping routes, queued for the service to run once
#: (plan D36; `hookqueue.py`): `<event id>.json` waits, `<event id>.done` marks
#: one taken. `drained-by` holds the version of the service that drains it: a
#: dispatcher with no service to ask writes here only when that is the CLI
#: version the next service starts as (`hooks/dispatch.py` `queue_folder`).
HOOK_QUEUE_DIRNAME = "hooks-queue"
HOOK_QUEUE_DRAINER = "drained-by"

#: Where `uv tool install probe-research` puts the CLI's interpreter, for a
#: client that finds no runtime.json (a CLI that has not run since its install).
UV_TOOL_PYTHON = (".local", "share", "uv", "tools", "probe-research", "bin", "python")


#: Where Linux keeps its machine id (systemd; dbus's copy on older systems).
MACHINE_ID_FILES = ("/etc/machine-id", "/var/lib/dbus/machine-id")
#: Hex digits of sha256("probe-service\0" + machine id) that name the folder.
MACHINE_KEY_LEN = 16
#: `ioreg` is the fallback where gethostuuid() cannot be called.
_IOREG_TIMEOUT_S = 5

_machine_key: str | None = None


def home() -> Path:
    """`$HOME`, as every client spells it (Path.home() reads HOME on POSIX)."""
    return Path.home()


def state_dir() -> Path:
    """`~/.local/state/probe`, never moved by XDG_STATE_HOME (X2)."""
    return home() / ".local" / "state" / "probe"


def _darwin_uuid() -> str | None:
    """The Mac's hardware UUID (IOPlatformUUID), upper case with dashes:
    gethostuuid(2) through ctypes, else `ioreg`."""
    try:
        import ctypes
        import ctypes.util

        try:
            libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
        except OSError:
            libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.dylib")
        buf = ctypes.create_string_buffer(16)
        wait = (ctypes.c_long * 2)(5, 0)  # struct timespec {5 s, 0 ns}
        if libc.gethostuuid(buf, wait) == 0:
            raw = buf.raw.hex().upper()
            return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"
    except Exception:  # noqa: BLE001 -- no ctypes in this build, or no symbol: `ioreg`
        pass
    try:
        out = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True,
            text=True,
            timeout=_IOREG_TIMEOUT_S,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if '"IOPlatformUUID"' in line and "=" in line:
            value = line.split("=", 1)[1].strip().strip('"').upper()
            return value or None
    return None


def _windows_guid() -> str | None:
    """Windows's machine id: HKLM\\SOFTWARE\\Microsoft\\Cryptography MachineGuid."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)) as key:
            value, _kind = winreg.QueryValueEx(key, "MachineGuid")
        return str(value).strip() or None
    except (ImportError, OSError):
        return None


def machine_id() -> str:
    """This machine's own id as the OS keeps it: Linux's machine-id, macOS's
    hardware UUID, Windows's MachineGuid, else the hostname (never empty)."""
    if WINDOWS:
        found = _windows_guid()
        if found:
            return found
    elif sys.platform == "darwin":
        found = _darwin_uuid()
        if found:
            return found
    else:
        for name in MACHINE_ID_FILES:
            try:
                value = Path(name).read_text(encoding="ascii", errors="replace").strip()
            except OSError:
                continue
            if value:
                return value
    return socket.gethostname() or "localhost"


def machine_key() -> str:
    """The folder name of this machine's service (computed once per process)."""
    global _machine_key
    if _machine_key is None:
        digest = hashlib.sha256(b"probe-service\0" + machine_id().encode("utf-8", "replace")).hexdigest()
        _machine_key = digest[:MACHINE_KEY_LEN]
    return _machine_key


def root_dir() -> Path:
    """`~/.local/state/probe/service`: every machine's folder, and the files
    that describe the install rather than one machine. On Windows
    `%LOCALAPPDATA%\\probe\\service` (private to the user there)."""
    if WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) if local else home() / "AppData" / "Local"
        return base / "probe" / SERVICE_DIRNAME
    return state_dir() / SERVICE_DIRNAME


def service_dir() -> Path:
    """`~/.local/state/probe/service/<machine key>`: this machine's service."""
    return root_dir() / machine_key()


def lock_path() -> Path:
    return service_dir() / LOCK_NAME


def start_lock_path() -> Path:
    return service_dir() / START_LOCK_NAME


def starts_path() -> Path:
    return service_dir() / STARTS_NAME


def registry_path() -> Path:
    return service_dir() / REGISTRY_NAME


def log_path() -> Path:
    return service_dir() / LOG_NAME


def runtime_path() -> Path:
    return root_dir() / RUNTIME_NAME


def actions_dir() -> Path:
    return service_dir() / ACTIONS_DIRNAME


def barrier_path() -> Path:
    return service_dir() / BARRIER_NAME


def hook_queue_dir() -> Path:
    return service_dir() / HOOK_QUEUE_DIRNAME


def update_state_path() -> Path:
    return service_dir() / UPDATE_STATE_NAME


def app_update_path() -> Path:
    return root_dir() / APP_UPDATE_NAME


def fallback_python() -> Path:
    if WINDOWS:
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else home() / "AppData" / "Roaming"
        return base / "uv" / "tools" / "probe-research" / "Scripts" / "python.exe"
    return home().joinpath(*UV_TOOL_PYTHON)


def _socket_in(folder: Path) -> Path:
    """`<folder>/service.sock`, or, when that is too long for a UNIX socket,
    `/tmp/probe-<uid>/service-<hash>.sock`.

    The hash names the folder, so two HOMEs of one user (a test, a second
    checkout of a dotfile setup) never share one socket while each holds its
    own `service.lock`: the identity stays the folder, as above.
    """
    path = folder / SOCKET_NAME
    if WINDOWS or len(os.fsencode(str(path))) <= MAX_SOCKET_PATH:
        return path  # (never listened on, on Windows: `pipe_name`)
    digest = hashlib.sha256(os.fsencode(str(folder))).hexdigest()[:16]
    return Path("/tmp") / f"probe-{os.getuid()}" / f"service-{digest}.sock"


def socket_path() -> Path:
    """This machine's service socket (see `_socket_in`). POSIX: Windows
    serves `pipe_name()` instead."""
    return _socket_in(service_dir())


def pipe_name() -> str:
    """Windows: this user's service pipe on this machine (`winpipe.pipe_name`)."""
    from . import winpipe

    return winpipe.pipe_name(machine_key(), os.path.normcase(str(service_dir())))


def legacy_socket_path() -> Path:
    """Where clients from before the machine key connect: `root_dir()`'s socket."""
    return _socket_in(root_dir())


def legacy_barrier_path() -> Path:
    """Where a service from before the machine key raises its update barrier:
    a `probe` that starts while such a service still updates waits for it too."""
    return root_dir() / BARRIER_NAME


def legacy_lock_path() -> Path:
    """Held by whichever service serves `legacy_socket_path` (one per HOME)."""
    return root_dir() / LOCK_NAME


#: Windows locks one byte this far into the file (`probe._shared.oscompat`'s
#: offset): its byte-range locks are mandatory, and a lock on byte 0 would
#: stop readers of what a lock file carries (a pid).
_WIN_LOCK_OFFSET = 0x7FFFFFFF


def try_lock(fd: int, *, shared: bool = False) -> bool:
    """Take the whole-file advisory lock on `fd` without waiting: True when
    taken, False when another holds it. `fcntl.flock` on POSIX (exclusive or
    shared); one byte with `msvcrt.locking` on Windows (exclusive only), per
    open handle in both: a second open in the same process conflicts."""
    if WINDOWS:
        import msvcrt

        try:
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def unlock(fd: int) -> None:
    if WINDOWS:
        import msvcrt

        try:
            os.lseek(fd, _WIN_LOCK_OFFSET, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def replace(src, dst) -> None:
    """`os.replace`; on Windows retried for up to a second while a reader has
    the target open (Python opens files without FILE_SHARE_DELETE there)."""
    if not WINDOWS:
        os.replace(src, dst)
        return
    import time

    deadline = time.monotonic() + 1.0
    while True:
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def private_dir(path: Path) -> Path:
    """Create `path` (and parents) and make sure only this user can open it.

    0700, owned by us, and not a symlink: the socket's permissions are the
    access control on platforms that do not expose the peer's uid, so the
    folder around it must not be anyone else's to plant files in.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if WINDOWS:
        return path  # under %LOCALAPPDATA%, which only this user (and admins) can open
    st = path.lstat()
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"{path} is not a private folder of this user")
    if st.st_mode & 0o077:
        os.chmod(path, 0o700)
    return path


def require_private(path: Path) -> None:
    """Raise unless `path` is a folder only this user can plant files in: ours,
    not a symlink, writable by nobody else.

    A client checks the socket's folder with it before it trusts whatever
    answers there: on the /tmp fallback any local user could have made
    `/tmp/probe-<uid>` first and listen in it. `private_dir` refuses to SERVE
    in such a folder; this refuses to CONNECT to one. FileNotFoundError when
    the folder is not there (no service has run).
    """
    st = path.lstat()
    if WINDOWS:
        if not stat.S_ISDIR(st.st_mode):
            raise PermissionError(f"{path} is not a folder")
        return
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise PermissionError(f"{path} is not a private folder of this user")


def ensure_dirs() -> Path:
    """The shared folder, this machine's and the socket's, all private.
    Returns the socket path."""
    private_dir(root_dir())
    private_dir(service_dir())
    sock = socket_path()
    if not WINDOWS:
        private_dir(sock.parent)
    return sock
