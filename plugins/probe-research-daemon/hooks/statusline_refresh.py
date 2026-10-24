#!/usr/bin/env python3
"""Keep this session's status-line marker warm, from the authoritative answer.

`GET /v1/sessions/{id}/work` knows what a coding-agent session has put in Probe,
because every client write stamps its session id on a header. This hook caches
that answer into the marker file so the status line can render it with no network
and no credential on the render path.

WHY A SERVER READ RATHER THAN INSTRUMENTING THE WRITE PATHS. A session's work can
be created through the SDK in a training process, the `probe` CLI in a shell, the
hosted MCP (which runs no code on this machine at all), or a script three
processes deep. Only some of those are ours to hook, and a status line that says
"untracked" because it missed one is worse than no status line. One read covers
every path by construction. The one exception is the PROJECT name: the daemon
stores the project its last write landed in (`<session>.scope`), and
`_session_marker.read` shows that in place of this answer's guess.

CONTRACT — this is a hook, so it inherits the house rules:

  * NEVER BLOCKS. The observing process throttles, then hands the fetch on
    and returns. A slow or unreachable API costs the tool call nothing. Where
    the Probe service answers, it takes the fetch (`statusline.refresh`): ONE
    child per plugin folder on its schedule, `fetch-all` below, refreshes
    every session that asked, instead of a process per session per refresh.
    Else (no service, an older one, a credential in this environment the
    service cannot be given) a DETACHED child of this script does it
    (`start_new_session`, as telemetry.py's sender does), as before.
  * SILENT ALWAYS. Exit 0 whatever happens; print nothing on stdout, which for a
    PostToolUse hook is a channel that can alter the session.
  * NOT TELEMETRY, so deliberately NOT gated on `PROBE_TELEMETRY`. That
    killswitch turns off analytics about the user; this is a feature the user
    asked for by installing the status line. `PROBE_STATUSLINE=off` disables it.
  * SELF-HOST SAFE. It talks to whatever base_url the client is configured with
    and to nothing else — there is no vendor endpoint involved, so the hosted-only
    gate that telemetry needs does not apply here.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

#: Ceiling on how often the network is touched per session. The hook fires after
#: every matching tool call, and a session doing a hundred of them in a minute
#: needs one read, not a hundred. Small enough that a `probe run start` shows up
#: in the status line while the researcher is still looking at the terminal.
REFRESH_SECONDS = 5

#: The fetch is detached, so this bounds nothing user-visible; it exists so a
#: hung socket cannot leave a child alive for the rest of the session.
FETCH_TIMEOUT = 10



def _hook_harness():
    """The harness resolver beside this file, loaded by explicit path."""
    import importlib.util  # noqa: PLC0415

    key = "_probe_hooks._hook_harness"
    if key in sys.modules:
        return sys.modules[key]
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_hook_harness.py")
    spec = importlib.util.spec_from_file_location(key, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module

def _load(name: str):
    """A vendored sibling module by explicit path — see statusline.py's `_load`."""
    import importlib.util  # noqa: PLC0415

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


#: Files the status-line install copies out of this directory, and that this
#: hook keeps current. See `probe.cli.statusline.RENDERER_FILES` -- the installer
#: side owns the list; this is its runtime twin.
RENDERER_FILES = ("statusline.py", "_session_marker.py")


def install_dir() -> str:
    claude = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(claude, "probe-research-statusline")


def install_dirs() -> list[str]:
    """Every place a status-line install keeps the renderer copy: Claude Code's
    (`statusline.install_dir`) and Kimi Code's (`statusline.kimi_install_dir`,
    under `$KIMI_CODE_HOME`, default `~/.kimi-code`)."""
    kimi = os.environ.get("KIMI_CODE_HOME") or os.path.join(os.path.expanduser("~"), ".kimi-code")
    return [install_dir(), os.path.join(kimi, "probe-research-statusline")]


def sync_renderer() -> None:
    """Refresh the installed renderer copies from this plugin. Never raises.

    The status line points at a stable directory rather than at this plugin,
    because the plugin's own path carries its version number and would break on
    every release. The cost of that choice is that the copy goes stale, so this
    closes the loop at the one moment it is free: SessionStart, right after an
    update would have landed.

    ONLY WHEN ALREADY INSTALLED. An absent directory means the user never turned
    the status line on in the wizard, and a hook that created it would be installing a
    status line nobody asked for.

    ONLY FROM THE INSTALLED COPY. Every session's status line runs the one copy,
    so a session left open on an older plugin copy whose SessionStart (a
    `/clear`, a resume) could not hand over to the installed one wrote its OLD
    renderer over the new one (surface audit #7). When Claude Code's install
    ledger names another copy of this plugin as the renderer's
    (`renderer_copy`: its user-scope install, else the newest), this one
    writes nothing; the Probe service refreshes it from that copy after an
    update (`probe.service.followup`). No ledger, or one that names none (a
    checkout, Kimi Code's own copy): as before.
    """
    if _another_copy_installed():
        return
    for target in install_dirs():
        if os.path.isdir(target):
            _sync_into(target)


def _another_copy_installed() -> bool:
    """Does Claude Code's install ledger name a copy of this plugin other than
    this folder as the renderer's (`renderer_copy` of its key,
    `dispatch.ledger_key`)? False when it cannot be read or names none. Never
    raises."""
    try:
        import json

        root = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        dispatch = _load("dispatch")
        raw = dispatch._read_ledger(dispatch._ledger(os.environ))
        if raw is None:
            return False
        plugins = json.loads(raw.decode("utf-8")).get("plugins")
        chosen = renderer_copy(plugins.get(dispatch.ledger_key(root)) if isinstance(plugins, dict) else None)
        return chosen is not None and chosen != root
    except BaseException:  # noqa: BLE001 - a hook contracted to silence
        return False


def renderer_copy(entries) -> "str | None":
    """Which of a ledger key's install folders the renderer comes from: its
    user-scope one (a project's own install serves that project only), else
    the newest version folder; the newest of several either way. None for
    anything but a non-empty list of entries that each name one. The
    service's twin is `probe.service.followup.renderer_copy`."""
    if not isinstance(entries, list) or not entries:
        return None
    try:
        listed = [(e.get("scope"), os.path.realpath(e["installPath"])) for e in entries]
    except (KeyError, TypeError, AttributeError, ValueError):
        return None
    candidates = {p for scope, p in listed if scope == "user"} or {p for _, p in listed}

    def version(path: str) -> tuple:
        parts = os.path.basename(path).split(".")
        return tuple(int(p) for p in parts) if all(p.isdigit() for p in parts) else ()

    return max(sorted(candidates), key=version)


def _sync_into(target: str) -> None:
    source = os.path.dirname(os.path.abspath(__file__))
    for name in RENDERER_FILES:
        src, dst = os.path.join(source, name), os.path.join(target, name)
        try:
            if not os.path.isfile(src):
                continue
            with open(src, "rb") as handle:
                new = handle.read()
            try:
                with open(dst, "rb") as handle:
                    if handle.read() == new:
                        continue
            except OSError:
                pass
            tmp = dst + "." + str(os.getpid()) + ".tmp"
            with open(tmp, "wb") as handle:
                handle.write(new)
            os.replace(tmp, dst)
        except OSError:
            continue


def installed() -> bool:
    """Whether the status line was ever installed. THE GATE FOR ALL OF THIS.

    Nothing here is worth a single request unless something will render the
    answer. Without this gate every plugin user paid three API calls per refresh
    for a segment they may never have opted into — and under Codex, which has no
    status-line surface at all, that spend could never buy anything: its plugin
    manifest declares skills, mcpServers and interface, and there is nowhere for
    a rendered line to go.

    The install directory is the right signal because the status-line install
    (the wizard's step) is the only thing that creates it, and `uninstall` is the only thing that
    would have a reason to remove it. An opt-in feature should cost nothing at
    all to the people who did not opt in.

    The notify flag counts too: under Codex the segment can never render, so
    `install` opts into the on-change notice instead — and that notice reads the
    same marker, so it needs the same refresh behind it.
    """
    if any(os.path.isdir(target) for target in install_dirs()):
        return True
    try:
        return bool(_load("_session_marker").notify_enabled())
    except BaseException:  # noqa: BLE001 - a hook contracted to silence
        return False


def disabled() -> bool:
    return (os.environ.get("PROBE_STATUSLINE") or "").strip().lower() in {
        "off",
        "0",
        "false",
        "no",
        "disabled",
    }


def due(marker, session_id: str) -> bool:
    """Whether enough time has passed since the last refresh.

    Reads the marker's own timestamp rather than a side-car clock file: one
    fewer thing to keep in step, and a marker that was just written is by
    definition a fresh answer.
    """
    try:
        path = marker.marker_path(session_id)
        return (time.time() - os.path.getmtime(path)) >= REFRESH_SECONDS
    except OSError:
        return True  # no marker yet: the first refresh is always due


#: Agents whose session id the server records on `runs.foreign_keys`. Must track
#: `app.runs.agent_session.CAPTURED_AGENTS` and the SDK's capability table; an
#: agent named here that the server does not record simply returns no rows.
def _captured_ids() -> tuple:
    import json as _json  # noqa: PLC0415

    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "harnesses.json"), encoding="utf-8") as fh:
            rows = _json.load(fh)["harnesses"]
        return tuple(row["id"] for row in rows if row.get("captured") is True)
    except (OSError, ValueError, KeyError, TypeError):
        return ("claude_code", "codex", "pi")


AGENT_KEYS = _captured_ids()


def _get(base_url: str, token: str, path: str, params: dict | None = None):
    """One authenticated GET, decoded. None on any failure — never raises."""
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": "Bearer " + token,
            "User-Agent": "probe-statusline-refresh",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT) as response:
            return json.load(response)
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def fetch_work(base_url: str, token: str, session_id: str) -> dict | None:
    """The session's projects and runs. None on failure."""
    payload = _get(
        base_url, token, "/v1/sessions/" + urllib.parse.quote(session_id, safe="") + "/work"
    )
    return payload if isinstance(payload, dict) else None


def fetch_active_run_ids(base_url: str, token: str, session_id: str, marker) -> list:
    """Run ids the SERVER considers live, for runs originating in this session.

    `?active=true` is the server's own verdict — status is running AND the newest
    substantive update or heartbeat is inside the liveness window — so it sees a
    run executing on a cluster, which the local lock scan structurally cannot.

    The `foreign_key` lookup keys on the ORIGINATING session
    (`<agent>_session_id`, written first-write-wins when the run is created), so
    this is precisely "runs this conversation opened".

    One request per captured agent, because a session id alone does not say which
    agent issued it. One cheap indexed lookup per captured agent, off the
    render path.
    """
    ids = []
    for agent in AGENT_KEYS:
        payload = _get(
            base_url,
            token,
            "/v1/runs",
            {"foreign_key": agent + "_session_id:" + session_id, "active": "true"},
        )
        ids.extend(marker.from_active_runs(payload))
    return ids


#: The `/tmp` filename prefix the tap's watcher files carry, per agent. A
#: The watcher prefix and the tap's state directory come from the harness
#: registry copy beside this file (`_hook_harness`), the same rows
#: `tap.config` and `probe.cli.capabilities` read from their own copies.
#: `agent/tests/test_watcher_prefix_parity.py` still runs every home and
#: compares them.
#:
#: WHY THE STATE DIR AND NOT JUST THE PID FILE. A missing pid file has two
#: causes that are nothing alike: a daemon that died, and a tap that was never
#: installed. Reading only `/tmp` collapses them into `not started`, so the
#: segment on a tracking-only machine would blame a crashed daemon that never
#: existed.


def _agent_source(env=None) -> str:
    """Which harness this session belongs to, by the hook's own evidence
    (`_hook_harness.current`: PROBE_AGENT, the plugin-root variable, the
    harness marker, else the registry default)."""
    return _hook_harness().current(env).id


def _harness(source: str):
    reg = _hook_harness().registry()
    found = reg.find(source)
    if found is None or found.capture is None:
        return reg.get(reg.default)
    return found


def _tap_plugin_dir(source: str, env=None) -> str:
    """Where the tap keeps its durable state for `source`. Never raises: an
    unknown source reads the default harness's folder (a status line only
    reports; it never writes there)."""
    try:
        return str(_harness(source).capture.state_dir(os.environ if env is None else env))
    except Exception:  # noqa: BLE001 - the status line must render
        return os.path.join(os.path.expanduser("~"), ".claude", "plugins", "probe-research-tap")


def _watcher_prefix(source: str) -> str:
    return _harness(source).capture.watcher_prefix


def _capture_reading(session_id: str, agent: str | None = None) -> dict:
    """Is a transcript daemon live for this session, and if not, why? Stdlib only.

    COMPUTED HERE, IN THE DETACHED CHILD, AND NEVER IN THE RENDERER.
    `statusline.py` renders in ~26ms measured and its contract is no network, no
    `probe` subprocess, and nothing of ours imported but one vendored module by
    explicit path. Putting this in the marker means the segment gains a FIELD TO
    READ rather than a syscall to make, and the render path is untouched.

    THE GATE ORDER IS `probe.cli.capture_state.session_capture_state`'s, and the
    order is the meaning: installed, then not switched off, then running. Each
    answer is a different sentence for the reader — `not installed` is a
    decision of theirs being honoured, `killswitch` likewise, and only
    `not started` is a fault. Two implementations that ordered these
    differently would put two different words on the same machine.

    `halted` (the tap's 401 latch) sits between those and running, as there:
    see below.

    A pid that exists is taken at face value here, where `probe.cli.capture_state`
    additionally asks `ps` what the process IS. That check is a subprocess, and
    this runs every few seconds for the life of a session; the accident it
    guards against is pid reuse inside the refresh window, which costs one
    over-optimistic reading that the next refresh corrects.

    A session the Probe service records has no wrapper and so no pid file: its
    `.hosted` marker names the service instead (`tap record`, plan A2). With no
    live pid in a pid file of this user's, that marker is read as
    `capture_state._hosted_pid` reads it, and NOT at face value: nothing rewrites
    the marker of a service that stopped without resuming the session, so a
    reused pid would read as recording for as long as it lives. The marker's pid
    must be the service's: `probe.service` in its `/proc` command line, or, with
    no `/proc` (macOS), the pid the service wrote into its lock file. Reading
    only the pid file put `not started` on every session the service recorded.

    `halted` is read here too, from the tap's own state db (`last_401_at`, the
    latch `tap/outbox.py` sets on a 401): a recorder the service keeps retrying
    after a 401 leaves a live service behind its marker, so without this the
    segment would say capture is fine while nothing uploads. The rejected
    token's fingerprint is not compared (that needs the tap's credential
    resolution): a re-paired machine clears the latch at its recorder's next
    start, seconds later under the service.

    A pid file whose pid is gone is stale, not an answer, as there: its
    wrapper gave up or was killed, and the session may have moved to the
    service since, which leaves its marker beside that pid file.

    Windows: the watcher files are in the folder the hooks named
    (`_watcher_dir`), a pid is never probed with `os.kill` (signal 0 is
    CTRL_C_EVENT, sent to a whole process group), and the marker's pid must be
    the one the service wrote into its lock file, still that same process
    (`_alive_nt`), as `capture_state._hosted_pid` reads it there. A wrapper's
    pid file names a Git Bash (MSYS) pid, which no Windows call can ask, so
    there only the marker can say running.

    A marker naming the service is not taken as "running" on its own: the
    service is asked what its recorder of the session does (`service.status`,
    `_recorder_reading`), as `capture_state` asks it: `not recording` while a
    live recorder does not hold the session (its server does not answer, it
    waits to try again), `not started` when it records no such session. No
    answer (no service, an older one) reads the marker alone, as before. Asked
    here, in the refresh, never in the renderer.

    `agent` is the session's harness as its hook resolved it, for a refresh
    the Probe service runs (its environment names no harness).
    """
    source = agent or _agent_source()

    plugin_dir = _tap_plugin_dir(source)
    if not os.path.exists(plugin_dir):
        return {"running": False, "reason": "not installed"}
    if os.path.exists(os.path.join(plugin_dir, ".disabled")):
        return {"running": False, "reason": "killswitch"}
    if _halted(plugin_dir):
        return {"running": False, "reason": "halted"}

    stem = os.path.join(_watcher_dir(source), _watcher_prefix(source) + "-watcher-" + session_id)
    if os.name != "nt":
        pid = _own_pid(stem + ".pid")
        if pid is not None and pid > 0:
            try:
                os.kill(pid, 0)
                return {"running": True, "reason": "running"}
            except OSError:
                pass
    if not _is_the_service(_own_pid(stem + ".hosted")):
        return {"running": False, "reason": "not started"}
    reading = _recorder_reading(session_id)
    if reading in (None, "running"):
        return {"running": True, "reason": "running"}
    return {"running": False, "reason": reading}


#: Mirrors `tap.config.WATCHER_DIR_ENV`: on Windows, Git Bash's `/tmp` as a
#: Windows path, which session-start.sh exports for what it starts ...
_WATCHER_DIR_ENV = "PROBE_TAP_WATCHER_DIR"
#: ... and keeps in the tap's state folder for what it does not start, this
#: refresh included (`capture_state.WATCHER_DIR_FILE`).
_WATCHER_DIR_FILE = ".watcher_dir"


def _watcher_dir(source: str | None = None) -> str:
    """Where the tap's hooks keep a session's watcher files: `/tmp`. On Windows
    the folder Git Bash's `/tmp` is: the one session-start.sh recorded in the
    tap's state folder (`_WATCHER_DIR_FILE`), else `_WATCHER_DIR_ENV`, else the
    user's temp folder. Mirrors `capture_state._watcher_dir`."""
    if os.name == "nt":
        if source:
            try:
                with open(os.path.join(_tap_plugin_dir(source), _WATCHER_DIR_FILE), encoding="utf-8") as handle:
                    saved = handle.read(4096).strip()
            except (OSError, ValueError):
                saved = ""
            if saved and os.path.isabs(saved):
                return saved
        given = os.environ.get(_WATCHER_DIR_ENV, "")
        if given and os.path.isabs(given):
            return given
        import tempfile  # noqa: PLC0415

        return tempfile.gettempdir()
    return "/tmp"


#: One `service.status` per refresh process, at most every this many seconds:
#: the service's `fetch-all` child refreshes many sessions at once.
_RECORDERS_TTL_S = 2.0
#: ... and how long it may take (the connect has its own 0.3 s).
_RECORDERS_TIMEOUT_S = 0.5
_recorders_cache: dict = {}
_recorders_lock = threading.Lock()


def _service_recorders() -> "list | None":
    """The Probe service's `recorders` (`service.status`), through the vendored
    `_service_rpc`; None when no service answers in time (or a hook is told
    not to ask, `PROBE_HOOK_SERVICE=off`). Never raises."""
    with _recorders_lock:
        cached = _recorders_cache.get("rows")
        if cached is not None and time.monotonic() - cached[0] < _RECORDERS_TTL_S:
            return cached[1]
        rows = None
        try:
            rpc = _load("_service_rpc").Rpc.connect()
            if rpc is not None:
                try:
                    found = rpc.call("service.status", {}, timeout=_RECORDERS_TIMEOUT_S).get("recorders")
                    rows = [row for row in found if isinstance(row, dict)] if isinstance(found, list) else None
                finally:
                    rpc.close()
        except Exception:  # noqa: BLE001 - the status line must render: the marker alone
            rows = None
        _recorders_cache["rows"] = (time.monotonic(), rows)
        return rows


def _recorder_reading(session_id: str) -> "str | None":
    """`capture_state._recorder_reading`, through `_service_recorders`: what
    the service's recorder of the session does now (`running`, `not
    recording`: a live recorder that does not hold the session, `not started`:
    none), or None when the service cannot say (the marker alone, as before)."""
    rows = _service_recorders()
    if rows is None:
        return None
    for row in rows:
        if row.get("sid") != session_id:
            continue
        if row.get("alive") is False:
            return "not started"
        claim = row.get("recording")
        if not isinstance(claim, bool):
            return None
        return "running" if claim else "not recording"
    return "not started"


def _own_pid(path: str) -> int | None:
    """The number in a watcher file of this user's, else None. Opened without
    following a link and without blocking (`/tmp` is shared: another user's
    FIFO there would hang this child until the service kills it), then checked
    on the open file: a regular file, this user's."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            return None
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return None
        return int(os.read(fd, 32).decode("ascii", "replace").strip())
    except (OSError, ValueError):
        return None
    finally:
        os.close(fd)


#: Where Linux lists processes; a machine without it (macOS) asks the lock file.
_PROC_DIR = "/proc"
#: The vendored `_service_paths`, loaded once per process (`_service_lock`).
_service_paths = None


def _service_lock() -> str:
    global _service_paths
    if _service_paths is None:
        _service_paths = _load("_service_paths")
    return str(_service_paths.lock_path())


def _alive_nt(pid: int, start: str) -> bool:
    """Windows: is the process (pid, start) still running? `procid.alive`'s
    Windows branch, stdlib only: OpenProcess, its exit code still STILL_ACTIVE
    (259), and its creation FILETIME the start the service wrote. Never
    `os.kill`: on Windows that ends the process (signal 0 is CTRL_C_EVENT)."""
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            times = [wintypes.FILETIME() for _ in range(4)]
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
                return False
            if not k32.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return False
            created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        finally:
            k32.CloseHandle(handle)
    except (OSError, AttributeError, ValueError):
        return False
    return str(created) == start


def _is_the_service(pid: int | None) -> bool:
    """Is `pid` this user's Probe service, alive? Its `/proc` command line names
    `probe.service` (Linux); with no `/proc`, it is the pid the service holding
    its lock wrote there (`server._name_holder`), and alive. On Windows that
    lock line's start time must match too (`_alive_nt`): no `/proc`, and no
    `os.kill`."""
    if pid is None or pid <= 1:
        return False
    if os.name == "nt":
        try:
            with open(_service_lock(), encoding="utf-8") as handle:
                pid_s, _, start = handle.read(64).strip().partition(" ")
        except (OSError, ValueError):
            return False
        return pid_s == str(pid) and bool(start) and _alive_nt(pid, start)
    try:
        with open(os.path.join(_PROC_DIR, str(pid), "cmdline"), "rb") as handle:
            return b"probe.service" in handle.read()
    except FileNotFoundError:
        if os.path.isdir(os.path.join(_PROC_DIR, "self")):
            return False  # /proc is there and this pid is not: it is gone
    except OSError:
        return False
    try:
        with open(_service_lock(), encoding="utf-8") as handle:
            if handle.read(64).split()[:1] != [str(pid)]:
                return False
        os.kill(pid, 0)
    except (OSError, ValueError):
        return False
    return True


def _halted(plugin_dir: str) -> bool:
    """Is the tap in its 401 latch (`last_401_at` in its state db's meta)?
    Read-only, and never waiting on a writer: a missing, locked or older db is
    not a halt."""
    import sqlite3  # noqa: PLC0415 - only a session with a tap state db pays for it
    from pathlib import Path  # noqa: PLC0415

    db = os.path.join(plugin_dir, "state.db")
    if not os.path.isfile(db):
        return False
    try:
        conn = sqlite3.connect(Path(os.path.abspath(db)).as_uri() + "?mode=ro", uri=True, timeout=0.05)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute("SELECT v FROM meta WHERE k = 'last_401_at'").fetchone()
    except sqlite3.Error:
        return False
    finally:
        conn.close()
    return bool(row and row[0])


def refresh(session_id: str, agent: str | None = None, *, core=None, marker=None) -> None:
    """Fetch and store. Runs in the DETACHED child (or the service's
    `fetch-all`, which hands over the modules it loaded once), never in the hook."""
    core = core if core is not None else _load("_telemetry_core")
    marker = marker if marker is not None else _load("_session_marker")
    config = core.read_cli_config()
    token = core.effective_token(config)
    if not token:
        return
    base_url = core.effective_base_url(config)
    work = fetch_work(base_url, token, session_id)
    if work is None:
        # Offline, or a token that cannot read this session. Leave the previous
        # marker in place: a stale project name is a better answer than
        # flickering to "untracked" every time the wifi drops.
        return
    state = marker.from_session_work(work)
    # Liveness is a SEPARATE failure domain from identity. If this call fails we
    # still store the project — the segment simply falls back to the local locks
    # for the accent, which is exactly what the fast path is for.
    state["active_run_ids"] = fetch_active_run_ids(base_url, token, session_id, marker)
    # Local, and stored alongside the server's answer so the segment renders both
    # halves of one state from one file. Written LAST so a reading is never
    # published without the identity it qualifies: `not capturing session
    # transcript` on a segment that does not yet name a project is a warning
    # about nothing.
    state["capture"] = _capture_reading(session_id, agent)
    marker.write(session_id, state)


#: `fetch-all` refreshes at most this many sessions (the service sends fewer)...
MAX_FETCH_ALL = 64
#: ... this many at once: each is a few reads of up to FETCH_TIMEOUT, and the
#: detached fetches it replaces ran side by side too.
FETCH_ALL_THREADS = 8


def fetch_all(request: object) -> None:
    """The Probe service's child: `{"sessions": [{"sid", "agent"}, ...]}` on
    stdin, each session refreshed as `fetch` refreshes one (several at once),
    the gate re-checked first. One session's failure never costs another its turn."""
    if disabled() or not installed() or not isinstance(request, dict):
        return
    sessions = request.get("sessions")
    if not isinstance(sessions, list):
        return
    marker = _load("_session_marker")
    work = []
    for item in sessions[:MAX_FETCH_ALL]:
        if not isinstance(item, dict):
            continue
        session_id, agent = item.get("sid"), item.get("agent")
        if isinstance(session_id, str) and marker.valid_session_id(session_id):
            work.append((session_id, agent if isinstance(agent, str) and agent else None))

    # Everything a session's refresh loads, loaded HERE, once, before any
    # thread runs: a module loaded by path is registered in sys.modules before
    # it has run (`_hook_harness.load_sibling`), so a second thread loading
    # one could read it half built. The threads then only read them.
    core = _load("_telemetry_core")
    for source in {agent for _sid, agent in work} | {None}:
        try:
            resolved = source or _agent_source()
            _tap_plugin_dir(resolved)
            _watcher_prefix(resolved)
        except Exception:  # noqa: BLE001 - the session's own refresh says what fails
            pass

    def one(job) -> None:
        try:
            refresh(*job, core=core, marker=marker)
        except Exception:  # noqa: BLE001 - silent by contract; the others still refresh
            pass

    if len(work) <= 1:
        for job in work:
            one(job)
        return
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    with ThreadPoolExecutor(max_workers=min(FETCH_ALL_THREADS, len(work))) as pool:
        list(pool.map(one, work))


def _service_takes(session_id: str) -> bool:
    """Hand the fetch to the Probe service (`statusline.refresh`): True when
    it took it. Never raises."""
    try:
        service = _load("_service_call")
        root = os.path.realpath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        try:
            agent = _agent_source()
        except Exception:  # noqa: BLE001 - then the service's child resolves its default
            agent = None
        return bool(service.statusline_refresh(session_id, root, agent=agent))
    except Exception:  # noqa: BLE001 - a hook contracted to silence: spawn as before
        return False


def spawn(session_id: str) -> None:
    """Hand the fetch on (the service, else a detached copy of this script)
    and return at once."""
    if _service_takes(session_id):
        return
    try:
        subprocess.Popen(  # noqa: S603 - sys.executable + this file
            [sys.executable, os.path.abspath(__file__), "fetch", session_id],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except (OSError, ValueError):
        pass


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    # Detached child: do the work, then get out. Re-checks the gate because it
    # was spawned some milliseconds ago and an uninstall in between should stop
    # the request rather than let one last one through.
    if argv[:1] == ["fetch"]:
        session_id = argv[1] if len(argv) > 1 else ""
        if session_id and not disabled() and installed():
            try:
                refresh(session_id)
            except BaseException:  # noqa: BLE001 - detached and silent by contract
                pass
        return 0
    # The Probe service's child: every session that asked, in one process.
    if argv[:1] == ["fetch-all"]:
        try:
            fetch_all(json.load(sys.stdin))
        except BaseException:  # noqa: BLE001 - silent by contract
            pass
        return 0

    # Hook: gate, parse, throttle, spawn. The gate comes FIRST — it is one stat,
    # and it is the common case for anyone who never installed the status line,
    # so it should cost them nothing beyond that.
    if disabled() or not installed():
        return 0
    try:
        payload = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return 0

    try:
        marker = _load("_session_marker")
        if not marker.valid_session_id(session_id):
            return 0

        # MAINTENANCE FIRST, and specifically BEFORE the tracking-off gate.
        # Refreshing the installed renderer is housekeeping, not tracking, and a
        # session that turned tracking OFF needs it MORE than one that did not:
        # gating it behind the switch meant such a session never picked up a new
        # renderer, so it could not learn to display `tracking off` and showed a
        # stale, wrong state forever. Pruning is the same shape of chore.
        if payload.get("hook_event_name") == "SessionStart":
            marker.prune()
            sync_renderer()

        # Not tracking: stop spending REQUESTS on it. This asks the SAME resolver
        # the segment renders from, rather than only checking for an explicit
        # `off` -- otherwise a machine whose default is off would keep paying
        # three API calls per refresh for a segment that can never read as
        # tracking. The two must agree or one of them is lying.
        signal = marker.tracking_signal(session_id)
        if signal is not None:
            tracking = marker.is_tracking(signal)
        else:
            cwd = payload.get("cwd")
            cwd = cwd if isinstance(cwd, str) and cwd else None
            tracking, _source = marker.resolve_tracking_default(cwd)
        if not tracking:
            return 0
        if payload.get("hook_event_name") != "SessionStart" and not due(marker, session_id):
            return 0
    except BaseException:  # noqa: BLE001 - a hook contracted to silence
        return 0

    spawn(session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
