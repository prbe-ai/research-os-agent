"""Where the Probe service keeps its files (API v1, "Transport" and "Lifecycle").

ONE service per user per machine, and its identity is a place on disk: the
default state dir under `$HOME`, `~/.local/state/probe/service/`. Deliberately
NOT `$XDG_STATE_HOME`: every client (an agent's hook, the desktop app, a
terminal) carries its own environment, and two clients that disagreed about
`XDG_STATE_HOME` would start two services and each see half the sessions
(plan X2). `HOME` is the one thing they all agree on.

Stdlib only, and synced byte-for-byte into the tap (`tap/service/paths.py`,
`make sync-service-client`): the tap runs under the system python with no
`probe` importable. Imports between the synced files stay relative.
"""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

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

#: Where `uv tool install probe-research` puts the CLI's interpreter, for a
#: client that finds no runtime.json (a CLI that has not run since its install).
UV_TOOL_PYTHON = (".local", "share", "uv", "tools", "probe-research", "bin", "python")


def home() -> Path:
    """`$HOME`, as every client spells it (Path.home() reads HOME on POSIX)."""
    return Path.home()


def state_dir() -> Path:
    """`~/.local/state/probe`, never moved by XDG_STATE_HOME (X2)."""
    return home() / ".local" / "state" / "probe"


def service_dir() -> Path:
    return state_dir() / SERVICE_DIRNAME


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
    return service_dir() / RUNTIME_NAME


def actions_dir() -> Path:
    return service_dir() / ACTIONS_DIRNAME


def barrier_path() -> Path:
    return service_dir() / BARRIER_NAME


def update_state_path() -> Path:
    return service_dir() / UPDATE_STATE_NAME


def app_update_path() -> Path:
    return service_dir() / APP_UPDATE_NAME


def fallback_python() -> Path:
    return home().joinpath(*UV_TOOL_PYTHON)


def socket_path() -> Path:
    """`<service dir>/service.sock`, or, when that is too long for a UNIX
    socket, `/tmp/probe-<uid>/service-<hash>.sock`.

    The hash names the service dir, so two HOMEs of one user (a test, a second
    checkout of a dotfile setup) never share one socket while each holds its
    own `service.lock`: the identity stays the state dir, as above.
    """
    path = service_dir() / SOCKET_NAME
    if len(os.fsencode(str(path))) <= MAX_SOCKET_PATH:
        return path
    digest = hashlib.sha256(os.fsencode(str(service_dir()))).hexdigest()[:16]
    return Path("/tmp") / f"probe-{os.getuid()}" / f"service-{digest}.sock"


def private_dir(path: Path) -> Path:
    """Create `path` (and parents) and make sure only this user can open it.

    0700, owned by us, and not a symlink: the socket's permissions are the
    access control on platforms that do not expose the peer's uid, so the
    folder around it must not be anyone else's to plant files in.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise PermissionError(f"{path} is not a private folder of this user")


def ensure_dirs() -> Path:
    """The service dir and the socket's folder, both private. Returns the socket path."""
    private_dir(service_dir())
    sock = socket_path()
    private_dir(sock.parent)
    return sock
