"""What each open Kimi Code session LOADED, and whether every one of them takes
a Probe update with no restart (kimi/pi update parity K4).

Kimi reads a plugin's manifest (`.kimi-plugin/plugin.json`: its hook entries,
skills path and MCP servers) once, at its start or `/reload`, and keeps running
the folder it loaded (its plugin-root variable). Probe's own hooks hand every event
to the copy Kimi's plugin list records (plugin 0.175.0, tap 0.10.18), so new
hook CODE reaches an open session; a new MANIFEST does not. The folders on disk
cannot say what a session loaded: Probe prunes old version folders into links
to the kept one and a token sync rewrites every kept manifest
(`probe.cli.kimi_config`). So the hook writes, into the record it keeps for
the Kimi process (`session_marker.record_harness_process`), what that process
loaded: `{"plugin_root", "plugin", "plugin_version", "plugin_fp", "tap_root",
"tap_version", "tap_fp", "late"}`, first seen at its SessionStart (`late`:
first seen at a later event, by a session that started before this record
existed, which cannot be proven).

`sessions_follow()` is true only when every live Kimi process running Probe's
hooks has such a record, started on hand-over-capable releases, loaded the
manifests now installed, and has its transcript recorded by the Probe service
(a session still on its own `tap watch`, a sandboxed one, keeps that watcher's
code until it ends).

Canonical here; a byte-identical copy sits beside the hooks as `_kimi_live.py`
(`make sync-session-marker`, tests/test_kimi_live.py). Stdlib only, Python 3.9.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys

#: The first releases whose hooks hand every event to the installed copy.
FIRST_FOLLOWING_PLUGIN = (0, 175, 0)
FIRST_FOLLOWING_TAP = (0, 10, 18)
TAP_PLUGIN = "probe-research-tap"
MANIFEST = os.path.join(".kimi-plugin", "plugin.json")
MANAGED = "managed"
PLUGIN_LIST = "installed.json"
#: A Kimi plugin version folder's name: `<id>@<version>`.
_VERSIONED_ID = re.compile(r"^[A-Za-z0-9._-]+@\d+(\.\d+)+$")
WATCHER_PREFIX = "probe-research-tap"
WATCHER_DIR_ENV = "PROBE_TAP_WATCHER_DIR"
#: The events whose record is trusted: the manifest was just loaded.
START_EVENTS = ("SessionStart",)
#: The Probe service's list of the sessions it records (`probe.service.recorders.RECORDERS_NAME`).
RECORDERS_NAME = "recorders.json"


def _numbers(version) -> tuple:
    if not isinstance(version, str) or not re.match(r"^\d+(\.\d+)+$", version.strip()):
        return ()
    return tuple(int(p) for p in version.strip().split("."))


def _manifest(root: str) -> "dict | None":
    try:
        with open(os.path.join(root, MANIFEST), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _tree_digest(folder: str) -> str:
    """Every file under `folder` (names and contents): what Kimi reads from a
    plugin's skills folder, which stays the folder it loaded."""
    digest = hashlib.sha256()
    for base, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(base, name)
            try:
                with open(path, "rb") as handle:
                    body = handle.read()
            except OSError:
                continue
            digest.update(os.path.relpath(path, folder).encode() + b"\0" + hashlib.sha256(body).digest())
    return digest.hexdigest()[:16]


def manifest_fingerprint(root: str, *, contents: bool = True) -> "str | None":
    """What Kimi takes from a plugin at load and keeps: its manifest's hook
    entries, its MCP servers (address and a digest of their headers, never the
    headers: a token or workspace-limit change needs a reload too, as
    `kimi_config.sync_mcp_bearer` says) and its skills (the folder the
    manifest names, every file in it: Kimi reads them from the folder it
    loaded). `contents=False`: the manifest's own shape only (no skill files,
    no header digest), what the K3 pin test holds still. None when there is no
    readable manifest."""
    data = _manifest(root)
    if data is None:
        return None
    hooks = data.get("hooks") if isinstance(data.get("hooks"), list) else []
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
    skills = data.get("skills")
    shape = {
        "hooks": [{k: h.get(k) for k in ("event", "matcher", "command", "timeout")} for h in hooks if isinstance(h, dict)],
        "skills": skills,
        "skills_files": _tree_digest(os.path.join(root, skills)) if contents and isinstance(skills, str) and skills else "",
        "mcp": {name: {**{k: s.get(k) for k in ("type", "url", "auth", "command", "args")},
                       "headers": hashlib.sha256(json.dumps(s.get("headers") if contents else None,
                                                            sort_keys=True).encode()).hexdigest()[:16]}
                for name, s in sorted(servers.items()) if isinstance(s, dict)},
    }
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:16]


def _version(root: str) -> str:
    data = _manifest(root) or {}
    version = data.get("version")
    if isinstance(version, str) and _numbers(version):
        return version
    tail = os.path.basename(os.path.realpath(root)).split("@", 1)
    return tail[1] if len(tail) == 2 else ""


def recorded_roots(plugin_root: str) -> "dict[str, str]":
    """{plugin id: root} Kimi's plugin list records, for the `managed/` folder
    `plugin_root` sits in; {} when it cannot be read."""
    managed = os.path.dirname(os.path.realpath(plugin_root))
    if os.path.basename(managed) != MANAGED:
        return {}
    try:
        with open(os.path.join(os.path.dirname(managed), PLUGIN_LIST), encoding="utf-8") as handle:
            records = json.load(handle)["plugins"]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    out: dict = {}
    for record in records if isinstance(records, list) else []:
        if isinstance(record, dict) and isinstance(record.get("id"), str) and isinstance(record.get("root"), str):
            out[record["id"]] = record["root"]
    return out


def loaded_info(plugin_root: str, event: str, source: str = "") -> "dict | None":
    """What a Kimi process loaded, from the folder its entry ran from
    (`plugin_root`: the session's own, before any hand-over) and the tap its
    plugin list recorded at that moment. `source`: a SessionStart's (`startup`,
    `resume`: a `/reload` reopens its session with `resume`), which decides
    whether this replaces what the process recorded before
    (`session_marker.record_harness_process`). None when `plugin_root` is no
    Kimi plugin folder."""
    if not plugin_root or _manifest(plugin_root) is None:
        return None
    plugin = os.path.basename(os.path.realpath(plugin_root)).split("@", 1)[0]
    tap_root = recorded_roots(plugin_root).get(TAP_PLUGIN, "")
    managed = os.path.dirname(os.path.realpath(plugin_root))
    # What Kimi read at its start: a first record proves it only when none of
    # these changed after the process started (`record_harness_process`).
    read_at_start = [os.path.join(os.path.dirname(managed), PLUGIN_LIST), os.path.realpath(plugin_root)]
    if tap_root:
        read_at_start.append(os.path.realpath(tap_root))
    return {
        "plugin_root": plugin_root, "plugin": plugin, "plugin_version": _version(plugin_root),
        "plugin_fp": manifest_fingerprint(plugin_root),
        "tap_root": tap_root, "tap_version": _version(tap_root) if tap_root else "",
        "tap_fp": manifest_fingerprint(tap_root) if tap_root else None,
        "late": event not in START_EVENTS,
        "source": source if event in START_EVENTS and isinstance(source, str) else "",
        "read_at_start": read_at_start,
    }


def _session_marker():
    """`probe.sdk.session_marker`, or the copy beside the hooks."""
    try:
        from probe.sdk import session_marker  # noqa: PLC0415

        return session_marker
    except ImportError:
        import importlib.util  # noqa: PLC0415

        name = "_probe_hooks._session_marker"
        if name in sys.modules:
            return sys.modules[name]
        spec = importlib.util.spec_from_file_location(name, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                                          "_session_marker.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module


def _processes(marker) -> "dict | None":
    """Every process here as {pid: (parent, start, title)}; None when they
    cannot be listed."""
    if os.path.isdir("/proc/self"):
        out = {}
        for name in os.listdir("/proc"):
            if name.isdigit():
                info = marker._proc_linux(int(name))  # noqa: SLF001
                if info is not None:
                    out[int(name)] = info
        return out or None
    return marker._ps_table() or None  # noqa: SLF001


def live_records(harness: str, titles=(), marker=None) -> "list[dict | None] | None":
    """One entry per live process of `harness`: the record its hooks keep
    (`harness-processes/<pid>-<start>.json`), or None for a live process titled
    one of `titles` (the registry's `process_title` and `binary`) with no
    readable record of this harness -- one Probe cannot prove anything about.
    None (the whole answer) when processes cannot be listed here."""
    if os.name == "nt":
        return None
    marker = marker or _session_marker()
    table = _processes(marker)
    if table is None:
        return None
    out: list = []
    seen = set()
    try:
        folder = marker.harness_processes_dir()
        entries = sorted(folder.glob("*.json")) if folder.is_dir() else []
    except OSError:
        return None
    for path in entries:
        pid_text, _, start_text = path.stem.partition("-")
        if not pid_text.isdigit():
            continue
        info = table.get(int(pid_text))
        if info is None or re.sub(r"[^A-Za-z0-9]", "", info[1]) != start_text:
            continue  # gone, or a pid reused since
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict) and data.get("harness") == harness:
            out.append(data)
            seen.add(int(pid_text))
    wanted = tuple(t for t in titles if t)
    for pid, (_parent, _start, title) in table.items():
        if pid not in seen and wanted and any(marker._titled(title, t) for t in wanted):  # noqa: SLF001
            out.append(None)  # a live process of this harness with no record: unproven
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        pass
    import subprocess  # noqa: PLC0415

    try:
        return subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(pid)],  # noqa: S603,S607
                              capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _pid_in(path: str) -> "int | None":
    """The pid a watcher file of this user's own names (a regular file, never
    followed through a link: the watcher dir is shared /tmp), else None."""
    import stat  # noqa: PLC0415

    try:
        info = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode) or (hasattr(os, "getuid") and info.st_uid != os.getuid()):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 1 else None


def _service_holds(session_id: str) -> bool:
    """Does the Probe service's saved list of what it records (`recorders.json`
    in its folder) name this session? Read from `probe.service.paths`, or the
    copy beside the hooks; False when it cannot be read."""
    try:
        try:
            from probe.service import paths  # noqa: PLC0415
        except ImportError:
            import importlib.util  # noqa: PLC0415

            name = "_probe_hooks._service_paths"
            paths = sys.modules.get(name)
            if paths is None:
                spec = importlib.util.spec_from_file_location(
                    name, os.path.join(os.path.dirname(os.path.abspath(__file__)), "_service_paths.py"))
                paths = importlib.util.module_from_spec(spec)
                sys.modules[name] = paths
                spec.loader.exec_module(paths)
        with open(os.path.join(str(paths.service_dir()), RECORDERS_NAME), encoding="utf-8") as handle:
            sessions = json.load(handle).get("sessions") or []
        return any(isinstance(item, dict) and item.get("sid") == session_id for item in sessions)
    except Exception:  # noqa: BLE001 - unproven
        return False


_WATCH_ARGV = ("-m", "tap", "watch")


def _argv_of(pid: int) -> "list[str]":
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return [part.decode("utf-8", "replace") for part in handle.read().split(b"\0") if part]
    except OSError:
        return []


#: A Kimi tap watch in one `ps` command line, whose folders may hold spaces: the
#: `--plugin-root` runs up to its `<kimi>/plugins/managed/<id>@<version>` folder.
_WATCH_COMMAND = re.compile(
    r"(?:^|\s)-m\s+tap\s+watch(?:\s|$)")
_WATCH_SESSION = re.compile(r"\s--session-id\s+(\S+)")
_WATCH_ROOT = re.compile(r"\s--plugin-root\s+(.+?/" + MANAGED + r"/[A-Za-z0-9._-]+@\d+(?:\.\d+)+)/?(?=\s|$)")


def _watch_in_command(command: str) -> "str | None":
    """The session id of a Kimi tap watch in one `ps` command line (macOS gives
    no argv boundaries: a folder with a space in it splits), else None."""
    if not _WATCH_COMMAND.search(command):
        return None
    session = _WATCH_SESSION.search(command)
    return session.group(1) if session and _WATCH_ROOT.search(command) else None


def own_watches() -> "set[str] | None":
    """The session ids of every live `tap watch` running from a Kimi tap folder
    (`<kimi>/plugins/managed/probe-research-tap@<version>`, its `--plugin-root`):
    sessions on their own watcher, whatever folder their pid files are in.
    None when processes cannot be listed here."""
    if os.path.isdir("/proc/self"):
        argvs = [_argv_of(int(name)) for name in os.listdir("/proc") if name.isdigit()]
    else:
        import subprocess  # noqa: PLC0415

        try:
            out = subprocess.run(["ps", "-A", "-ww", "-o", "command="], capture_output=True, text=True,  # noqa: S603,S607
                                 timeout=5, check=False).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        lines = out.splitlines()
        if not lines:
            return None
        return {sid for sid in (_watch_in_command(line) for line in lines) if sid}
    found = set()
    for argv in argvs:
        at = next((i for i in range(len(argv) - 2) if tuple(argv[i:i + 3]) == _WATCH_ARGV), None)
        if at is None:
            continue
        args = argv[at + 3:]
        value = {flag: args[i + 1] for i, flag in enumerate(args[:-1]) if flag in ("--session-id", "--plugin-root")}
        root = value.get("--plugin-root", "").rstrip("/")
        if (_VERSIONED_ID.match(os.path.basename(root)) and os.path.basename(os.path.dirname(root)) == MANAGED
                and value.get("--session-id")):
            found.add(value["--session-id"])
    return found


def service_records(session_id: str, env=None, recorded=None) -> bool:
    """Is this session's transcript the Probe service's? Only when the service
    says it records it (`_service_holds`) and no live `tap watch` of its own
    is seen, or the one seen is hosted by a live service (`.hosted`). A session
    on its own watcher keeps that watcher's tap code until it ends; one whose
    watcher this process cannot see (another watcher folder, a sandbox's own
    /tmp) is not the service's either, which is why the service's own list
    decides, never the watcher file's absence. `recorded`: the sessions the
    running service says it records now (`service.status`'s recorders whose
    loop holds the claim); given, only that answers."""
    if recorded is not None:
        return session_id in recorded
    if not _service_holds(session_id):
        return False
    values = os.environ if env is None else env
    given = values.get(WATCHER_DIR_ENV) or ""
    stem = os.path.join(given if os.path.isabs(given) else "/tmp", f"{WATCHER_PREFIX}-watcher-{session_id}")
    watcher = _pid_in(stem + ".pid")
    if watcher is None or not _alive(watcher):
        return True
    hosted = _pid_in(stem + ".hosted")
    return hosted is not None and _alive(hosted) and "probe.service" in _cmdline(hosted)


def why_not(record: "dict | None", env=None, recorded=None) -> "str | None":
    """None when this live Kimi process takes the update with no restart; else why not."""
    if record is None:
        return "no record of what it loaded"
    loaded = record.get("loaded")
    if not isinstance(loaded, dict):
        return "started before Probe recorded what Kimi loaded"
    if loaded.get("late"):
        return "its start was not seen"
    if _numbers(loaded.get("plugin_version")) < FIRST_FOLLOWING_PLUGIN:
        return f"plugin {loaded.get('plugin_version') or '?'} hooks do not hand over"
    if loaded.get("tap_root") and _numbers(loaded.get("tap_version")) < FIRST_FOLLOWING_TAP:
        return f"tap {loaded.get('tap_version') or '?'} hooks do not hand over"
    installed = recorded_roots(str(loaded.get("plugin_root") or ""))
    plugin_now = installed.get(str(loaded.get("plugin") or ""))
    if not plugin_now or manifest_fingerprint(plugin_now) != loaded.get("plugin_fp"):
        return "the plugin's manifest changed since it loaded"
    tap_now = installed.get(TAP_PLUGIN)
    if loaded.get("tap_root") and (not tap_now or manifest_fingerprint(tap_now) != loaded.get("tap_fp")):
        return "the tap's manifest changed since it loaded"
    session_id = record.get("session_id")
    if isinstance(session_id, str) and not service_records(session_id, env, recorded):
        return "its transcript is still on its own tap watch"
    return None


def sessions_follow(harness: str, titles, env=None, marker=None, recorded=None) -> bool:
    """Do all open sessions of `harness` (a registry id; `titles`: the names its
    process goes by) take the update NOW INSTALLED with no restart? False when
    any cannot be proven, or processes cannot be listed here. Ask it after an
    update installed, never to predict one: a release not yet installed may
    change what Kimi loads. Never raises."""
    try:
        records = live_records(harness, titles, marker)
        if records is None:
            return False
        if not all(why_not(record, env, recorded) is None for record in records):
            return False
        # A session on its own watcher (a sandbox's, one in a host running
        # several sessions) keeps that watcher's tap code: any one seen that
        # the running service does not record keeps the restart line.
        watches = own_watches()
        return watches is not None and (not watches or (recorded is not None and watches <= set(recorded)))
    except Exception:  # noqa: BLE001 - unproven: the restart line
        return False
