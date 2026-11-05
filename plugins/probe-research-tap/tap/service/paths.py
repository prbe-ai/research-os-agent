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

import contextlib
import hashlib
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

#: A UNIX socket path is at most 108 bytes on Linux and 104 on macOS; past
#: this the socket moves to a private folder under /tmp (API v1, "Transport").
MAX_SOCKET_PATH = 100
#: Names the service's state root instead of `~/.local/state/probe/service`: another copy's own
#: service (D13), or the SDK's fallback on a read-only home (D9). Absolute; every client and the
#: service read it.
SERVICE_ROOT_ENV = "PROBE_SERVICE_ROOT"
#: In a folder whose socket path is too long: a symlink to the short folder the
#: socket lives in, made with `mkdtemp` (this user's, a name nobody can take first).
SOCKET_LINK_NAME = "socket-dir"

#: A fenced caller's requests and their answers, in this machine's folder (`get_drop_dir`): the
#: requests in `requests/` (the only files there: launchd starts the service while it holds any),
#: the answers in `answers/`, a request being written in the folder itself.
DROP_DIRNAME = "drop"
DROP_REQUESTS_DIRNAME = "requests"
DROP_ANSWERS_DIRNAME = "answers"
#: The OS units that start the service on its first connection (`get_activation_unit`), one set per
#: machine (a home folder shared between machines holds each one's): systemd's
#: `probe-service-<machine key>.socket` / `.service` / `.path`, launchd's agent.
ACTIVATION_NAME = "probe-service"
ACTIVATION_LABEL = "ai.prbe.probe-service"

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
#: Set by the service on itself, so every process it runs inherits it: its role
#: (`vocabularies.ServiceRole`). An SDK under a service sends its own HTTP; one
#: anywhere else sends it through the service (`sdk/delivery/transport/relay.py`).
SERVICE_ROLE_ENV = "PROBE_SERVICE_ROLE"
#: Hook events' record-keeping routes, queued for the service to run once
#: (plan D36; `hookqueue.py`): `<event id>.json` waits, `<event id>.done` marks
#: one taken. `drained-by` holds the version of the service that drains it: a
#: dispatcher with no service to ask writes here only when that is the CLI
#: version the next service starts as (`hooks/dispatch.py` `queue_folder`).
HOOK_QUEUE_DIRNAME = "hooks-queue"
HOOK_QUEUE_DRAINER = "drained-by"

#: `probe uninstall`'s own folder, `<state dir>/uninstall` (plan
#: clean-uninstall-reinstall U-T3/U-T9): `barrier.json` (while it is LIVE
#: nothing of Probe's starts: `client.uninstall_barrier`), `plan.json` (what the
#: uninstall owns) and `finisher.py` (the stdlib copy that deletes the CLI
#: last). Of the install, not of one machine: under the state dir itself.
UNINSTALL_DIRNAME = "uninstall"
UNINSTALL_BARRIER_NAME = "barrier.json"
UNINSTALL_PLAN_NAME = "plan.json"

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


def machine_id() -> str:
    """This machine's own id as the OS keeps it: Linux's machine-id, macOS's
    hardware UUID, else the hostname (never empty)."""
    if sys.platform == "darwin":
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


#: Another copy's own service roots (service-only D13): `<machine root>/copies/<hash of its prefix>`.
COPIES_DIRNAME = "copies"


def is_copy_root() -> bool:
    """Is this the service (or a client) of another copy than the machine's install: a
    checkout's or a project venv's `probe`, which runs a service of its own under
    `<root>/copies/<key>` (service-only D13; `cli_shim.choose_service_root` sets
    SERVICE_ROOT_ENV)? A root named for another reason (a read-only home's fallback) is not."""
    named = os.environ.get(SERVICE_ROOT_ENV)
    return bool(named) and os.path.isabs(named) and Path(named).parent.name == COPIES_DIRNAME


#: This process's own state root, when it picked one (`set_root`): an SDK on a read-only home, or
#: beside an older machine service (C1). A value, never `os.environ`: threads pick it (L-4).
_root_override: list = []


def set_root(path: str | None) -> None:
    """Use `path` (absolute) as this process's state root from now on; None: back to the default.
    The services it starts get it as PROBE_SERVICE_ROOT (`start._clean_env`)."""
    _root_override[:] = [os.path.abspath(path)] if path else []


def get_root_override() -> str | None:
    return _root_override[0] if _root_override else None


def root_dir() -> Path:
    """`~/.local/state/probe/service`: every machine's folder, and the files
    that describe the install rather than one machine; `PROBE_SERVICE_ROOT`
    when set (an absolute path)."""
    if _root_override:
        return Path(_root_override[0])
    named = os.environ.get(SERVICE_ROOT_ENV)
    if named and os.path.isabs(named):
        return Path(named)
    return state_dir() / SERVICE_DIRNAME


def service_dir() -> Path:
    """`~/.local/state/probe/service/<machine key>`: this machine's service."""
    return root_dir() / machine_key()


def get_drop_dir() -> Path:
    """`<service dir>/drop`: where a fenced caller whose sandbox refuses the socket leaves its
    requests (`<id>.req.json`) and finds their answers (`<id>.ans.json`), service-only plan Q1."""
    return service_dir() / DROP_DIRNAME


def get_drop_requests_dir() -> Path:
    """`<drop>/requests`: where a request is renamed in (`<id>.req.json`), and nothing else."""
    return get_drop_dir() / DROP_REQUESTS_DIRNAME


def get_drop_answers_dir() -> Path:
    """`<drop>/answers`: where the service leaves each request's answer (`<id>.ans.json`)."""
    return get_drop_dir() / DROP_ANSWERS_DIRNAME


def get_activation_name() -> str:
    """This machine's systemd unit name (no suffix): `probe-service-<machine key>`."""
    return f"{ACTIVATION_NAME}-{machine_key()}"


def get_activation_label() -> str:
    """This machine's launchd label: `ai.prbe.probe-service.<machine key>`."""
    return f"{ACTIVATION_LABEL}.{machine_key()}"


def get_activation_path() -> Path | None:
    """Where this machine's activation unit is written: systemd's `<name>.socket` (Linux),
    launchd's `<label>.plist` (macOS); None elsewhere."""
    if sys.platform == "darwin":
        return home() / "Library" / "LaunchAgents" / f"{get_activation_label()}.plist"
    if sys.platform.startswith("linux"):
        base = os.environ.get("XDG_CONFIG_HOME")
        return (Path(base) if base else home() / ".config") / "systemd" / "user" / f"{get_activation_name()}.socket"
    return None


def get_activation_unit() -> Path | None:
    """This machine's OS unit that starts the service on its first connection (service-only plan
    D19, `get_activation_path`), when it is on disk AND listens on this machine's socket
    (`ListenStream`, launchd's `SockPathName`); None otherwise. `ensure` then asks the OS to start
    the service (and spawns one only when the OS will not)."""
    unit = get_activation_path()
    if unit is None:
        return None
    try:
        text = unit.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    sock = str(socket_path())
    if unit.suffix == ".plist":
        import plistlib

        try:
            listeners = plistlib.loads(text.encode("utf-8")).get("Sockets") or {}
            named = {spec.get("SockPathName") for spec in listeners.values() if isinstance(spec, dict)}
        except (ValueError, AttributeError):
            return None
        return unit if sock in named else None
    named = {line.partition("=")[2].strip().replace("%%", "%") for line in text.splitlines()
             if line.startswith("ListenStream=")}
    return unit if sock in named else None


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


def uninstall_dir() -> Path:
    """`~/.local/state/probe/uninstall`: `probe uninstall`'s barrier, plan and finisher."""
    return state_dir() / UNINSTALL_DIRNAME


def uninstall_barrier_path() -> Path:
    return uninstall_dir() / UNINSTALL_BARRIER_NAME


def uninstall_plan_path() -> Path:
    return uninstall_dir() / UNINSTALL_PLAN_NAME


def fallback_python() -> Path:
    return home().joinpath(*UV_TOOL_PYTHON)


def _socket_in(folder: Path) -> Path:
    """`<folder>/service.sock`, or, when that is too long for a UNIX socket,
    `service.sock` in the short folder `<folder>/socket-dir` links to (a
    `mkdtemp` folder under /tmp: this user's, unguessable, never one another
    user made first). No link yet: the long path, which no client can
    connect to (no service runs). The link is made by the service
    (`ensure_dirs`), in its own private folder, so each folder keeps its own socket.
    """
    path = folder / SOCKET_NAME
    if len(os.fsencode(str(path))) <= MAX_SOCKET_PATH:
        return path
    try:
        short = os.readlink(folder / SOCKET_LINK_NAME)
    except OSError:
        return path
    return Path(short) / SOCKET_NAME if os.path.isabs(short) else path


def _make_short_socket_dir(folder: Path) -> Path:
    """The short folder `_socket_in` names for `folder`, made now when missing. Two processes
    making it at once agree (M8): the link is made with `symlink`, and the one that finds it made
    already keeps the winner's. A folder /tmp's sweep removed is made again at the same place (a
    `mkdir` there fails when anyone else got it first, and then the link moves to a fresh one)."""
    import tempfile

    link = folder / SOCKET_LINK_NAME
    try:
        current = Path(os.readlink(link))
    except OSError:
        current = None
    if current is not None and current.is_absolute():
        with contextlib.suppress(FileExistsError, FileNotFoundError):
            os.mkdir(current, 0o700)  # swept from /tmp: made again where the link points
        try:
            require_private(current)
            return current
        except OSError:
            pass  # someone else's: the link moves to a fresh folder below
    made = Path(tempfile.mkdtemp(prefix=f"probe-{os.getuid()}-", dir="/tmp"))
    if current is None:
        try:
            os.symlink(str(made), link)
            return made
        except FileExistsError:
            with contextlib.suppress(OSError):
                os.rmdir(made)
            winner = Path(os.readlink(link))
            require_private(winner)
            return winner
    temporary = folder / f"{SOCKET_LINK_NAME}.{os.getpid()}"
    with contextlib.suppress(OSError):
        os.unlink(temporary)
    os.symlink(str(made), temporary)
    os.replace(temporary, link)
    return made


def socket_path() -> Path:
    """This machine's service socket (see `_socket_in`)."""
    return _socket_in(service_dir())


def legacy_socket_path() -> Path:
    """Where clients from before the machine key connect: `root_dir()`'s socket, by
    their own rule (a too-long path: `/tmp/probe-<uid>/service-<hash>.sock`, which
    `private_dir` and `require_private` refuse when another user made the folder)."""
    path = root_dir() / SOCKET_NAME
    if len(os.fsencode(str(path))) <= MAX_SOCKET_PATH:
        return path
    digest = hashlib.sha256(os.fsencode(str(root_dir()))).hexdigest()[:16]
    return Path("/tmp") / f"probe-{os.getuid()}" / f"service-{digest}.sock"


def legacy_barrier_path() -> Path:
    """Where a service from before the machine key raises its update barrier:
    a `probe` that starts while such a service still updates waits for it too."""
    return root_dir() / BARRIER_NAME


def legacy_lock_path() -> Path:
    """Held by whichever service serves `legacy_socket_path` (one per HOME)."""
    return root_dir() / LOCK_NAME


def try_lock(fd: int, *, shared: bool = False) -> bool:
    """Take the whole-file advisory lock on `fd` without waiting: True when
    taken, False when another holds it. `fcntl.flock` (exclusive or shared),
    per open handle: a second open in the same process conflicts."""
    import fcntl

    try:
        fcntl.flock(fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def unlock(fd: int) -> None:
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def private_dir(path: Path) -> Path:
    """Create `path` (and parents) and make sure only this user can open it.

    0700, owned by us, and not a symlink: the socket's permissions are the
    access control on platforms that do not expose the peer's uid, so the
    folder around it must not be anyone else's to plant files in.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Checked and made 0700 through a descriptor of the folder itself, never a link
    # (`O_DIRECTORY|O_NOFOLLOW`, `fchmod`): a swap after the check changes nothing it does.
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_CLOEXEC", 0))
    except OSError as exc:
        raise OSError(f"{path} is not a private folder of this user ({exc.strerror or exc})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise OSError(f"{path} is not a private folder of this user")
        if st.st_mode & 0o077:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
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
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise PermissionError(f"{path} is not a private folder of this user")


def ensure_dirs() -> Path:
    """The shared folder, this machine's, its drop folder and the socket's, all private.
    Returns the socket path."""
    private_dir(root_dir())
    private_dir(service_dir())
    private_dir(get_drop_dir())
    private_dir(get_drop_requests_dir())
    private_dir(get_drop_answers_dir())
    sock = socket_path()
    if len(os.fsencode(str(service_dir() / SOCKET_NAME))) > MAX_SOCKET_PATH:
        _make_short_socket_dir(service_dir())
        sock = socket_path()
    private_dir(sock.parent)
    return sock
