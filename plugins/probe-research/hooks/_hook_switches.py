"""Which hooks answer a coding agent's event: Probe's own plugins or the frozen hook shim (codex-capture-fixes D35).

A machine whose agent moved to the frozen shim (`probe-hooks`, plan PR 4-5) keeps Probe's own plugins
installed (they carry the skills, commands and MCP), so both fire on every event and exactly one must
answer it. The answer is the agent PROCESS's birth, read against an append-only switch history: a
process born before a switch to the shim never loaded it and keeps Probe's plugins; one born after is
the shim's. A rollback appends a switch back, so a process already born never changes side, and the
legacy hook and the service, reading one history and one kernel start time, agree.

    <service dir>/hooks/migrations.json   (the machine's own service folder: NFS homes stay apart)
    {"v": 1, "harnesses": {"<registry id>": {"<config root>": {
        "switches": [{"at": <epoch s>, "to": "shim" | "legacy"}, ...], "migrated_at": .., ...}}}}

Keyed by the agent's config root (its registry row's `home`: Claude Code's `CLAUDE_CONFIG_DIR`, else
`~/.claude`), so one config's migration never silences another's. Within MARGIN_S of a switch a birth
is unclear: both sides answer (a double, never a silence). The CLI writes the file
(`probe.shared.harness.hook_migration.MigrationLedger`, its one writer); everything here only reads it.

Stdlib only, Python 3.9, no relative imports: copied byte for byte into the Probe plugins' hooks
(`_hook_switches.py`) and the tap (`tap/hook_switches.py`, run as a script by its shell hooks:
`main`) by `tools/vendor.py`.
"""

from __future__ import annotations

import json
import os
import sys
import time

LEDGER_DIRNAME = "hooks"
LEDGER_NAME = "migrations.json"
LEDGER_VERSION = 1
#: A ledger past this is not read (it holds a few switches per agent).
MAX_LEDGER_BYTES = 1 << 20
#: Seconds around a switch in which a birth reads as unclear: both sides answer.
MARGIN_S = 2.0
#: A hook sits a few shells below its agent; past this the chain is not a hook's.
MAX_WALK = 16
#: Set by the Probe service and the shim's local runner on the routes they run: those ARE the shim's
#: answer (the same route files), so a legacy hook's yield never applies inside them.
VIA_ENV = "PROBE_HOOK_VIA"
_PS_TIMEOUT_S = 3
#: `PROC_PIDPATHINFO_MAXSIZE` (macOS `<sys/proc_info.h>`).
_PATH_MAX = 4096


class Side:
    """Which hooks a switch hands an agent's new processes to."""

    SHIM = "shim"
    LEGACY = "legacy"


def get_ledger_path(service_dir) -> str:
    """`<service dir>/hooks/migrations.json`."""
    return os.path.join(str(service_dir), LEDGER_DIRNAME, LEDGER_NAME)


def get_service_root(home=None) -> str:
    """`~/.local/state/probe/service`, every machine's service folder (`service_api.paths.root_dir`'s
    POSIX rule, which tests hold equal): found with no import, before a hook pays for the machine key."""
    return os.path.join(home or os.path.expanduser("~"), ".local", "state", "probe", "service")


def has_any_ledger(service_root) -> bool:
    """Has any machine under this service root (`~/.local/state/probe/service`) a ledger? One listdir and
    a stat per machine: the cheap check a hook makes before it computes its own machine's folder."""
    try:
        names = os.listdir(str(service_root))
    except OSError:
        return False
    return any(os.path.isfile(get_ledger_path(os.path.join(str(service_root), name))) for name in names)


def load_ledger(path) -> dict:
    """The ledger, or {} (missing, too big, not JSON, not an object)."""
    try:
        with open(str(path), "rb") as handle:
            data = handle.read(MAX_LEDGER_BYTES + 1)
    except OSError:
        return {}
    if len(data) > MAX_LEDGER_BYTES:
        return {}
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def get_switches(ledger: dict, harness: str, config_root: str) -> list:
    """[(at, side)] of one agent's config root, oldest first; [] when it never switched."""
    harnesses = ledger.get("harnesses")
    roots = harnesses.get(harness) if isinstance(harnesses, dict) else None
    record = roots.get(config_root) if isinstance(roots, dict) else None
    entries = record.get("switches") if isinstance(record, dict) else None
    found = []
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        at, side = entry.get("at"), entry.get("to")
        if isinstance(at, (int, float)) and not isinstance(at, bool) and side in (Side.SHIM, Side.LEGACY):
            found.append((float(at), side))
    found.sort(key=lambda switch: switch[0])
    return found


def get_side(switches: list, when: float) -> str:
    """The side the last switch at or before `when` named; Probe's plugins before any."""
    side = Side.LEGACY
    for at, to in switches:
        if at > when:
            break
        side = to
    return side


def is_shim_owned(switches: list, birth) -> bool:
    """A process plainly the shim's (born past a switch to it by MARGIN_S): Probe's plugins step aside."""
    return birth is not None and get_side(switches, birth - MARGIN_S) == Side.SHIM \
        and get_side(switches, birth + MARGIN_S) == Side.SHIM


def is_legacy_owned(switches: list, birth) -> bool:
    """A process plainly Probe's plugins' (no switch at all, or born well inside a legacy stretch): the
    shim's answer steps aside. Birth unknown with a switch on record: neither is plain, both answer."""
    if not switches:
        return True
    return birth is not None and get_side(switches, birth - MARGIN_S) == Side.LEGACY \
        and get_side(switches, birth + MARGIN_S) == Side.LEGACY


def _read_boot_time():
    """Linux's boot time (epoch s, /proc/stat `btime`): the same number for every reader."""
    try:
        with open("/proc/stat", encoding="ascii", errors="replace") as handle:
            for line in handle:
                if line.startswith("btime "):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def get_birth_epoch(start):
    """A process's kernel start time (`procid`'s and `tap/owner.py`'s form) as epoch seconds: Linux's
    clock ticks since boot, else `ps`'s UTC `lstart` (`Wed Sep 23 07:43:28 2026`, words joined by spaces
    or hyphens). None when it cannot be read."""
    text = str(start or "").strip()
    if not text:
        return None
    if text.isdigit():
        boot = _read_boot_time()
        if boot is None:
            return None
        try:
            return boot + int(text) / os.sysconf("SC_CLK_TCK")
        except (ValueError, OSError):
            return None
    import calendar  # noqa: PLC0415 -- only off Linux

    try:
        return float(calendar.timegm(time.strptime(" ".join(text.replace("-", " ").split()), "%a %b %d %H:%M:%S %Y")))
    except (ValueError, OverflowError):
        return None


def get_config_root(home, env) -> "str | None":
    """An agent's config folder, resolved: its registry row's `home` override variable, else
    `$HOME/<home path>`. None for a row with no home."""
    if not isinstance(home, dict) or not home.get("path"):
        return None
    override = (env.get(home.get("env") or "") or "").strip()
    base = override or os.path.join(env.get("HOME") or os.path.expanduser("~"), home["path"])
    return os.path.realpath(os.path.expanduser(base))


def _get_executable_path(pid: int) -> "str | None":
    """The file a macOS process runs (`proc_pidpath`): `ps`'s `args` cannot say where a path with a space
    ends (the Claude app keeps Claude Code under `~/Library/Application Support/`)."""
    try:
        import ctypes  # noqa: PLC0415 -- macOS only
    except ImportError:
        return None
    for library in ("/usr/lib/libproc.dylib", "/usr/lib/libSystem.B.dylib"):
        try:
            pidpath = ctypes.CDLL(library).proc_pidpath
        except (OSError, AttributeError):
            continue
        pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(_PATH_MAX)
        if pidpath(pid, buffer, _PATH_MAX) <= 0:
            return None
        return buffer.value.decode("utf-8", "replace") or None
    return None


def _read_process(pid: int):
    """(parent pid, start, names, zombie) as `procid.proc` reads them, or None."""
    if os.path.isdir("/proc/self"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as handle:
                raw = handle.read().decode("utf-8", "replace")
        except OSError:
            return None
        close = raw.rfind(")")
        fields = raw[close + 2:].split()
        if close < 0 or len(fields) < 20:
            return None
        names = {raw[raw.find("(") + 1:close]}
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                names.add(os.path.basename(handle.read().split(b"\0", 1)[0].decode("utf-8", "replace")))
        except OSError:
            pass
        try:
            return int(fields[1]), fields[19], frozenset(names), fields[0] == "Z"
        except ValueError:
            return None
    import subprocess  # noqa: PLC0415 -- only off Linux

    try:
        out = subprocess.run(
            ["ps", "-ww", "-o", "ppid=,stat=,lstart=,args=", "-p", str(pid)],
            capture_output=True, text=True, timeout=_PS_TIMEOUT_S, check=False,
            env={**os.environ, "LC_ALL": "C", "TZ": "UTC0"},
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    tokens = out.split()
    if len(tokens) < 8:
        return None
    try:
        ppid = int(tokens[0])
    except ValueError:
        return None
    names = {os.path.basename(tokens[7])}
    executable = _get_executable_path(pid) if sys.platform == "darwin" else None
    if executable:
        names.add(os.path.basename(executable))
    return ppid, " ".join(tokens[2:7]), frozenset(names), tokens[1].startswith("Z")


def _is_named(names: frozenset, wanted: frozenset) -> bool:
    return any(name in wanted or os.path.splitext(name)[0] in wanted for name in names)


def find_agent_birth(agent_names, start_pid=None):
    """The birth (epoch s) of the nearest ancestor of `start_pid` (default: this process; never itself)
    whose process name is one of `agent_names`; None when none is found or it cannot be read."""
    wanted = frozenset(name for name in agent_names if name)
    if not wanted:
        return None
    info = _read_process(os.getpid() if start_pid is None else start_pid)
    for _ in range(MAX_WALK):
        if info is None or info[0] <= 1:
            return None
        info = _read_process(info[0])
        if info is None:
            return None
        if not info[3] and _is_named(info[2], wanted):
            return get_birth_epoch(info[1])
    return None


def should_yield(service_dir, harness: str, home, agent_names, env=None, start_pid=None) -> bool:
    """Does a legacy hook of this agent step aside, because its process is plainly the shim's? False
    inside the service's or the runner's own run (VIA_ENV), with no ledger, and whenever unsure."""
    values = os.environ if env is None else env
    if values.get(VIA_ENV):
        return False
    path = get_ledger_path(service_dir)
    if not os.path.isfile(path):
        return False
    switches = get_switches(load_ledger(path), harness, get_config_root(home, values) or "")
    if not switches:
        return False
    return is_shim_owned(switches, find_agent_birth(agent_names, start_pid))


def _load_by_path(name: str, path: str):
    import importlib.util  # noqa: PLC0415

    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list) -> int:
    """`python3 -S -B hook_switches.py yield <paths.py> <harnesses.json> <harness id>`, for a shell hook:
    exit 0 when it steps aside (the shim answers this process), else 1, failures included. `paths.py`:
    the service's own path rules (its machine key); `harnesses.json`: the harness registry."""
    try:
        if len(argv) != 4 or argv[0] != "yield" or os.environ.get(VIA_ENV):
            return 1
        paths = _load_by_path("_probe_hook_paths", argv[1])
        if not has_any_ledger(paths.root_dir()):
            return 1
        with open(argv[2], encoding="utf-8") as handle:
            rows = json.load(handle).get("harnesses") or []
        row = next((raw for raw in rows if isinstance(raw, dict) and raw.get("id") == argv[3]), None)
        if row is None:
            return 1
        names = (row.get("binary"), row.get("process_title"))
        return 0 if should_yield(paths.service_dir(), argv[3], row.get("home"), names) else 1
    except Exception:  # noqa: BLE001 -- a hook never breaks the session: run as before
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
