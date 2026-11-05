"""make sure a service runs: the OS starts it where its activation units are (D19), else a rate-limited detached spawn; a fenced caller goes through the drop folder; an older service is replaced (D15); the uninstall barrier."""

from __future__ import annotations

import contextlib
import errno
import importlib
import json
import os
import re
import subprocess
import sys
import time

from . import paths, procid
from .service_api import TIMEOUT_S, Connection, ServiceError, connect
from .vocabularies import Client
from .wire import Method

#: `ensure` stops spawning after this many starts in START_WINDOW_S: a service
#: that crash-loops shows up as repeated starts in service.log, never as a
#: process storm (plan "Crash").
MAX_STARTS = 3
START_WINDOW_S = 60.0
#: A spawn younger than this whose pid still runs is starting: no second spawn.
SPAWN_SETTLE_S = 3.0
START_LOCK_WAIT_S = 2.0
#: `min_version`: how long an older service may take to exit once asked to stop (its recorders pause first),
#: and how long the current one started in its place gets to answer.
STOP_EXIT_WAIT_S = 15.0
START_WAIT_S = 10.0
#: A fenced caller's hello through the drop folder waits at least this long (as `connect`'s 0.5 s).
FENCED_HELLO_S = 0.5
#: `systemctl` / `launchctl`, at most.
MANAGER_TIMEOUT_S = 10.0

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
    "PROBE_SERVICE_TELEMETRY_HOST",
    # The person's opt-out of Probe's entries in each agent's sandbox config (service-only M3).
    "PROBE_SANDBOX_ACCESS",
)


class Ensured:
    """What `ensure` did."""

    RUNNING = "running"  # a service answered
    SPAWNED = "spawned"  # none did; this call started one
    #: One is starting: another caller spawned it moments ago, or the OS starts it (D19).
    STARTING = "starting"
    BACKOFF = "backoff"  # MAX_STARTS in START_WINDOW_S already: not again yet
    #: No interpreter to start it with, the OS would not start it, or (fenced) no drop folder.
    UNAVAILABLE = "unavailable"
    #: `probe uninstall` runs (its barrier is LIVE: `uninstall_barrier`). None started.
    UNINSTALLING = "uninstalling"
    #: `min_version` was asked and an older service answers that was not replaced (D15): it
    #: refused to stop (an update or an action runs), what would start is older too, or this
    #: caller is fenced (it never stops the service). It still serves.
    OLDER = "older"


# -- the uninstall barrier (plan clean-uninstall-reinstall, build contract) ------
#
# `probe uninstall` writes `<state dir>/uninstall/barrier.json`; while it is
# LIVE nothing of Probe's starts or updates anything: `ensure` (UNINSTALLING),
# the service's own start, the guard, the keeper, `write_runtime`, a daemon
# worker's start, and every installer. ONE reader decides LIVE / STALE:
# `probe.shared.updates.uninstall_barrier` (stdlib). This file is also the tap's copy,
# which has no `probe` to import: there the file being present is enough
# (conservative: nothing parsed, nothing cleared; the CLI's reader clears a
# stale one).

UNINSTALLING_MESSAGE = "Probe is being uninstalled on this computer; nothing of Probe starts until that finishes"


def uninstall_barrier() -> dict | None:
    """The uninstall barrier's record when it is LIVE, else None (none, or a
    stale one). `{}` for a LIVE one nothing could be read of. Never raises."""
    try:
        os.stat(paths.uninstall_barrier_path())
    except FileNotFoundError:
        return None  # the common path: one failed stat, nothing imported
    except OSError:
        pass
    reader = None
    # The old name is a CLI from before agent:12, which the tap's copy of this file can sit beside.
    for name in ("probe.shared.updates.uninstall_barrier", "probe._shared.uninstall_barrier"):  # restructure-guard-ok
        try:
            reader = importlib.import_module(name)
            break
        except ImportError:
            continue
    if reader is None:
        return {}
    try:
        data = reader.read()
        return data if data is not None and reader.state(data) == reader.State.LIVE else None
    except Exception:  # noqa: BLE001 -- a broken barrier reads as an uninstall running
        return {}


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
    if uninstall_barrier() is not None:
        return False  # Probe is being uninstalled: nothing of it is recorded again
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


#: A child that outlives its parent and never gets its terminal's Ctrl-C: its own session.
_DETACHED: dict = {"start_new_session": True}


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
        child = subprocess.Popen(argv, **spawn)  # noqa: S603 -- the CLI's own interpreter, our own module
    finally:
        os.close(log_fd)
    _children.append(child)
    return child.pid


def _is_like_its_ancestors() -> bool:
    """Linux: is this process confined as the oldest process of this user above it is (its agent,
    its login shell, a container's first process of this user)? A seccomp filter, no_new_privs, or
    a mount or user namespace of its own that one lacks is a sandbox around this process, and a
    service spawned here would inherit it (round 3, M-d): then nothing is spawned. Unknown: yes.
    KNOWN GAP: a Landlock domain shows in none of these; and inside a pid namespace of its own the
    walk ends at that namespace's first process."""
    if not sys.platform.startswith("linux"):
        return True
    mine, ancestor = _get_confinement(os.getpid()), _get_oldest_ancestor()
    if mine is None or ancestor is None:
        return True
    theirs = _get_confinement(ancestor)
    return theirs is None or theirs == mine


def _get_confinement(pid: int) -> tuple | None:
    """(Seccomp, Seccomp_filters, NoNewPrivs, mount namespace, user namespace) of `pid`; None unreadable."""
    try:
        fields = {}
        with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                fields[key] = value.strip()
        return (fields.get("Seccomp"), fields.get("Seccomp_filters"), fields.get("NoNewPrivs"),
                os.readlink(f"/proc/{pid}/ns/mnt"), os.readlink(f"/proc/{pid}/ns/user"))
    except OSError:
        return None


def _get_oldest_ancestor() -> int | None:
    """The oldest process of this user above this one (by parent pid), or None when there is none."""
    uid, pid, found, seen = str(os.getuid()), os.getppid(), None, set()
    while pid > 1 and pid not in seen and len(seen) < 64:
        seen.add(pid)
        try:
            with open(f"/proc/{pid}/status", encoding="utf-8") as handle:
                fields = dict(line.partition(":")[::2] for line in handle)
        except OSError:
            break
        if fields.get("Uid", "").split()[:1] != [uid]:
            break
        found = pid
        try:
            pid = int(fields.get("PPid", "0").strip())
        except ValueError:
            break
    return found


def _has_no_new_privs() -> bool:
    """Linux: does this process run with no_new_privs (`NoNewPrivs: 1`)? Elsewhere, no."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            return any(line.split(":", 1)[1].strip() == "1" for line in handle if line.startswith("NoNewPrivs:"))
    except OSError:
        return False


def _lock(fd: int, deadline: float) -> bool:
    while True:
        if paths.try_lock(fd):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def ensure(*, wait: float = 0.0, min_version: str | None = None) -> str:
    """Make sure a service runs (an `Ensured` value). Never raises.

    Where this machine's activation units are (`paths.get_activation_unit()`, D19) the OS starts
    it: this asks the OS, and spawns only when the OS will not (no user manager answers: a home
    shared between machines, a container). Elsewhere (a container, a training box) this spawns
    `python -I -m probe.service` detached. A fenced caller (a sandbox refused the socket, or hides
    its folder) never starts one: its hello in the drop folder does (`_ensure_fenced`). `wait` > 0 waits that long
    for it to answer (`probe service start`); hooks and taps pass nothing and go on at once.
    `min_version`: an older service is stopped and a current one started (D15), else OLDER.
    While `probe uninstall` runs (`uninstall_barrier`): UNINSTALLING, nothing started or written.
    """
    _reap()
    if uninstall_barrier() is not None:
        return Ensured.UNINSTALLING
    reached = _reach(0.5)
    if reached == _FENCED:
        return _ensure_fenced(wait, min_version)
    outcome = Ensured.RUNNING if reached else _start(wait)
    if min_version and outcome == Ensured.RUNNING:
        return _replace_older(min_version, wait)
    return outcome


#: `_reach`: a sandbox refused this process the socket (EPERM/EACCES at `socket()` or `connect()`).
_FENCED = "fenced"


def _reach(timeout: float) -> str | None:
    """RUNNING when something listens on the socket, _FENCED when a sandbox refuses it to this
    process (EPERM / EACCES) or hides this machine's folder from it (`_is_folder_hidden`), else None. A private-folder
    refusal (a PermissionError with no errno) is None."""
    try:
        Connection.open(timeout).close()
    except PermissionError as exc:
        return _FENCED if exc.errno in (errno.EPERM, errno.EACCES) or _is_folder_hidden() else None
    except OSError:
        return _FENCED if _is_folder_hidden() else None
    return Ensured.RUNNING


def _is_folder_hidden() -> bool:
    """Has a sandbox hidden this machine's service folder from this process? It mounts something
    else over it (an empty tmpfs): the folder is on another device than the folder holding it. A
    machine no service ran on yet has no folder at all, and that one spawns."""
    try:
        return os.stat(paths.service_dir()).st_dev != os.stat(paths.root_dir()).st_dev
    except OSError:
        return False


def _start(wait: float) -> str:
    """No service answers: the OS starts it (`_activate`) where this machine's units are, else
    (or when the OS will not) one spawn; then, with `wait`, its first answer. A process with
    no_new_privs never spawns one where the units are (L4: it may be in a Landlock domain no check
    sees, and its service would be too); where none are (a container, a training box: every process
    has it) it does."""
    is_activated = paths.get_activation_unit() is not None
    outcome = _activate() if is_activated else Ensured.UNAVAILABLE
    if outcome == Ensured.UNAVAILABLE and not (is_activated and _has_no_new_privs()) and _is_like_its_ancestors():
        outcome = _spawn_once()
    if wait > 0 and outcome in (Ensured.SPAWNED, Ensured.STARTING):
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            probe = connect(0.5)
            if probe is not None:
                probe.close()
                return Ensured.RUNNING
            time.sleep(0.02)
    return outcome


def _spawn_once() -> str:
    """One detached spawn under start.lock, rate-limited (MAX_STARTS in START_WINDOW_S)."""
    try:
        paths.ensure_dirs()
        fd = os.open(paths.start_lock_path(), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return Ensured.UNAVAILABLE
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
            return Ensured.STARTING
        if len(starts) >= MAX_STARTS:
            return Ensured.BACKOFF
        python = runtime_python()
        if python is None:
            return Ensured.UNAVAILABLE
        try:
            pid = _spawn(python)
        except OSError:
            return Ensured.UNAVAILABLE
        starts.append({"at": now, "pid": pid})
        _write_starts(starts)
        return Ensured.SPAWNED
    finally:
        os.close(fd)  # releases start.lock


def get_manager_env() -> dict:
    """The environment `systemctl --user` needs to find this user's manager: a service or hook
    may run without XDG_RUNTIME_DIR or the session bus, filled in from `/run/user/<uid>`."""
    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    with contextlib.suppress(OSError):
        if os.path.isdir(runtime) and os.stat(runtime).st_uid == os.getuid():
            env["XDG_RUNTIME_DIR"] = runtime
            bus = os.path.join(runtime, "bus")
            if "DBUS_SESSION_BUS_ADDRESS" not in env and os.path.exists(bus):
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def _run_manager(argv: list) -> int:
    """`systemctl` / `launchctl`'s exit status; -1 when it would not run."""
    try:
        return subprocess.run(argv, capture_output=True, timeout=MANAGER_TIMEOUT_S, check=False,  # noqa: S603
                              stdin=subprocess.DEVNULL, env=get_manager_env()).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def _activate() -> str:
    """Ask the OS to listen on the service's socket and drop folder (the units were never started
    since a service `ensure` spawned wrote them, H2, or were stopped, or failed): systemd resets a
    failed socket or path unit, starts both, and restarts the socket when nothing listens after;
    launchd kicks the agent, loading it first when it is not. STARTING (the OS starts the service
    on the next connection), else UNAVAILABLE: no user manager answers, or it would not start
    them (the caller then spawns one)."""
    if sys.platform == "darwin":
        domain, label = f"gui/{os.getuid()}", paths.get_activation_label()
        if _run_manager(["launchctl", "kickstart", f"{domain}/{label}"]) != 0:
            _run_manager(["launchctl", "bootstrap", domain, str(paths.get_activation_path())])
            if _run_manager(["launchctl", "kickstart", f"{domain}/{label}"]) != 0:
                return Ensured.UNAVAILABLE
        return Ensured.STARTING
    name = paths.get_activation_name()
    units = [f"{name}.socket", f"{name}.path"]
    if _run_manager(["systemctl", "--user", "show-environment"]) != 0:
        return Ensured.UNAVAILABLE
    _run_manager(["systemctl", "--user", "reset-failed", *units])
    if _run_manager(["systemctl", "--user", "start", *units]) != 0:
        return Ensured.UNAVAILABLE
    if _reach(0.5) is None and _run_manager(["systemctl", "--user", "restart", units[0]]) != 0:
        return Ensured.UNAVAILABLE
    return Ensured.STARTING


def _ensure_fenced(wait: float, min_version: str | None) -> str:
    """A fenced caller: a hello through the drop folder, for which the OS starts the service
    (systemd's `.path`, launchd's `QueueDirectories`); nothing is spawned or stopped from inside
    a fence (D19). RUNNING, OLDER (below `min_version`), STARTING (asked, no answer yet), or
    UNAVAILABLE (no drop folder; or the tap's copy of this file, which has no drop client)."""
    try:
        transports = importlib.import_module("probe.foundation.service_api.transports")
    except ImportError:
        return Ensured.UNAVAILABLE
    try:
        channel = transports.DropTransport().connect(max(wait, FENCED_HELLO_S), client=Client.CLI,
                                                     client_version="unknown")
        hello = channel.call(Method.HELLO)
    except (ConnectionRefusedError, TimeoutError):
        return Ensured.STARTING  # its request reached the folder: the OS starts the service for it
    except (OSError, ServiceError, ValueError):
        return Ensured.UNAVAILABLE
    found = hello.get("service_version") if isinstance(hello, dict) else None
    return Ensured.OLDER if min_version and _is_older(found, min_version) else Ensured.RUNNING


def _replace_older(min_version: str, wait: float) -> str:
    """D15: a service older than `min_version` is asked to stop, and a current one started once
    it exited. OLDER when it stays: it refused (an update or an action runs), it did not exit in
    STOP_EXIT_WAIT_S, or what would start is older too (`_get_start_version`)."""
    try:
        with Connection.open(TIMEOUT_S) as connection:
            hello = connection.hello(Client.CLI, "unknown")
            if not _is_older(hello.get("service_version"), min_version):
                return Ensured.RUNNING
            if _is_older(_get_start_version(), min_version):
                return Ensured.OLDER
            old = hello.get("pid")
            connection.call(Method.STOP)
    except ServiceError:
        return Ensured.OLDER
    except OSError:
        old = None  # it went meanwhile
    deadline = time.monotonic() + STOP_EXIT_WAIT_S
    while _pid_runs(old):
        if time.monotonic() >= deadline:
            return Ensured.OLDER
        time.sleep(0.05)
    return _start(max(wait, START_WAIT_S)) if _reach(0.5) is None else _wait_for_answer(max(wait, START_WAIT_S))


def _wait_for_answer(wait: float) -> str:
    """The OS listens on the path (activation): its next service answers within `wait`, or STARTING."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError, ServiceError, ValueError):
            with Connection.open(0.5) as connection:
                connection.hello(Client.CLI, "unknown")
                return Ensured.RUNNING
        time.sleep(0.02)
    return Ensured.STARTING


def _get_start_version() -> str | None:
    """The release that would start: the python this machine's activation unit runs, asked
    (M3: the unit names the tool path's python, which need not be runtime.json's), else
    runtime.json's `cli_version`. None: unknown."""
    unit = paths.get_activation_unit()
    python = _get_unit_python(unit) if unit is not None else None
    if python is None:
        return _get_runtime_version()
    try:
        done = subprocess.run([python, "-I", "-c", "import probe; print(probe.__version__)"],  # noqa: S603
                              capture_output=True, text=True, timeout=MANAGER_TIMEOUT_S, check=False,
                              stdin=subprocess.DEVNULL, env=_clean_env())
    except (OSError, subprocess.SubprocessError):
        return None
    found = done.stdout.strip()
    return found if done.returncode == 0 and found else None


def _get_unit_python(unit) -> str | None:
    """The interpreter an activation unit runs: the `.service` beside a systemd `.socket`
    (`ExecStart="<python>" ...`), launchd's `ProgramArguments[0]`."""
    try:
        if unit.suffix == ".plist":
            import plistlib

            argv = plistlib.loads(unit.read_bytes()).get("ProgramArguments") or []
            return argv[0] if argv and isinstance(argv[0], str) else None
        text = unit.with_suffix(".service").read_text(encoding="utf-8")
    except (OSError, ValueError, AttributeError):
        return None
    found = re.search(r'^ExecStart="((?:[^"\\]|\\.)*)"', text, re.MULTILINE)
    if found is None:
        return None
    return found.group(1).replace('\\"', '"').replace("\\\\", "\\").replace("%%", "%").replace("$$", "$")


def _get_runtime_version() -> str | None:
    """runtime.json's `cli_version`: the release `ensure` starts (None: unknown)."""
    try:
        found = json.loads(paths.runtime_path().read_text(encoding="utf-8")).get("cli_version")
    except (OSError, ValueError, AttributeError):
        return None
    return found if isinstance(found, str) else None


def _get_release(version: object) -> tuple | None:
    """`0.398.0` (or `0.398.0.dev3+g1`) -> (0, 398, 0); None for anything else."""
    found = re.match(r"(\d+(?:\.\d+)*)", version) if isinstance(version, str) else None
    return tuple(int(part) for part in found.group(1).split(".")) if found else None


def _is_older(found: object, wanted: object) -> bool:
    """Is `found` a release older than `wanted`? Unknown either way: no."""
    have, need = _get_release(found), _get_release(wanted)
    return have is not None and need is not None and have < need
