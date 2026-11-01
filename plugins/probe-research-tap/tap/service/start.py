"""make sure a service runs: rate-limited start records, the CLI's runtime interpreter, the detached spawn, the uninstall barrier; on Windows, spawn detached and outside the job, past the venv launcher."""

from __future__ import annotations

import contextlib
import importlib
import json
import os
import subprocess
import sys
import time

from . import paths, procid
from .service_api import connect

#: `ensure` stops spawning after this many starts in START_WINDOW_S: a service
#: that crash-loops shows up as repeated starts in service.log, never as a
#: process storm (plan "Crash").
MAX_STARTS = 3
START_WINDOW_S = 60.0
#: A spawn younger than this whose pid still runs is starting: no second spawn.
SPAWN_SETTLE_S = 3.0
START_LOCK_WAIT_S = 2.0

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
    #: `probe uninstall` runs (its barrier is LIVE: `uninstall_barrier`). None started.
    UNINSTALLING = "uninstalling"


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
    sandboxed (`probe.service.actions.caller.landlock_possible`): it would start,
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
    start`); hooks and taps pass nothing and go on at once. While `probe
    uninstall` runs (`uninstall_barrier`): UNINSTALLING, nothing started,
    nothing written.
    """
    _reap()
    if uninstall_barrier() is not None:
        return Ensured.UNINSTALLING
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
