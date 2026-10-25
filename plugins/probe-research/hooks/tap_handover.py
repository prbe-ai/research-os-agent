#!/usr/bin/env python3
"""Move an open session off a `tap watch` that can never hand it to the Probe service (plan C0).

A session's transcript is recorded either by the Probe service (one thread of
it, `tap record`) or, where no service answered at its start, by a `tap watch`
of its own (`<watcher dir>/<prefix>-watcher-<sid>.pid`, `.version` beside it;
the watcher dir is `/tmp`, or PROBE_TAP_WATCHER_DIR). From tap 0.10.13 such a
watcher hands the session to the service once one runs (`tap/handback.py`).
One an older tap started never does: the session stays on that tap's code for
good, gets no hints, and keeps the service starting and quitting (live test
bug 1, 2026-10-24). Its own tap hooks cannot fix it (Claude Code and Codex pin
a session's plugin folder at its start), but this plugin's hooks run the
INSTALLED copy at every event (`dispatch.hand_over`), so this route does, at a
prompt:

    fast path     the route's shell: no pid file for the session and it was
                  never moved, a pid file whose wrapper was handled already, a
                  moved session the service's `.hosted` marker names a live
                  pid for, a look made less than LATER_S ago, or the session
                  given up on: nothing runs
    proof         the pid is alive and its command line is THIS session's
                  `tap watch` (`*"tap watch"*"<sid>"*`, as the tap's
                  session-start.sh proves it on resume); a version below
                  HANDBACK_TAP (its `.version`, else the versioned folder in
                  that command line) and at least PROTOCOL2_TAP; and a newer
                  tap installed for this agent that carries the hand-back
                  (Claude Code's install ledger names it; Codex's cache keeps
                  the one version folder). A pid that is dead or not proven:
                  looked at again after LATER_S. Anything else unproven: left
                  alone
    the swap      in a detached child, never on the prompt's clock: `tap record`
                  with the NEWEST installed tap (the service takes the session;
                  its recorder waits for the old watcher's claim on the
                  session's journal), then the session's daemon worker is asked
                  to hand over (`<state>/probe/daemon/<sid>.handover`, dated
                  ahead, as `RecorderHost._hand_over` does: the old wrapper's
                  stop would otherwise end it as stopped, and a prompt in the
                  seconds before the service's worker takes the lease would
                  read "recording is back with you"; never when the service
                  recorded the session already, whose own worker holds the
                  lock and nothing stops), then the old wrapper's
                  group is stopped (SIGTERM, at most STOP_WAIT_S; its last
                  pass ships what it read) and its pid and version files
                  removed. The service's recorder takes the claim and goes on
                  from the journal's offsets: no line lost, none sent twice.
                  If the service will not record it (none can start here, a
                  sandbox, an agent already gone), the old watcher is left
                  recording and the swap is tried again after LATER_S, at
                  most MAX_TRIES times
    the old tap   its hooks still run in the session. Its prompt healer
                  (`ensure-daemon.sh`, taps 0.5.0 to 0.10.12) respawns a
                  wrapper when no pid file names a live one and its heal stamp
                  (`<tap state>/heal/<sid>`) is ten minutes old: the swap dates
                  that stamp HEAL_QUIET_S ahead, so it stays quiet (0.10.1+
                  still asks the service to record, never a wrapper, when the
                  service that recorded it died). Its SessionStart on a
                  compact (taps before 0.10.0: they know no service) starts a
                  wrapper that cannot take the journal's claim from the
                  service's recorder and gives up within a minute; one alive
                  at a prompt is a new pid, moved again as above, and the pid
                  file of one that gave up is removed (it would keep the heal
                  below from ever running)
    heal          a moved session nothing records any more (no `.hosted`
                  marker naming a live service: `probe capture off` then on,
                  a service gone): `tap record` again, at most every LATER_S,
                  as the old tap's healer would have (never while capture is
                  off, its `.disabled`)
    off           neither a move nor a heal for a session whose Probe switch
                  is `off` (`dispatch.session_off`): off means not captured
                  (owner, 2026-10-24). Looked at again after LATER_S

Known cost: a tap below 0.10.13 finalizes on the way out, so the stop queues
one FINALIZE for a session that is still live; the engine mines the session
once then and again at its real end, the same cost a resume onto a newer tap
pays (the tap's own session-start.sh replaces an older watcher the same way).

Markers in `<state>/probe/hooks/tap-handover/`: `<sid>` (never again: no
installed tap is looked for, a watcher that needs no move, PROBE_TAP_HOSTED=0,
Windows, MAX_TRIES spent), `<sid>.<pid>` (that wrapper was moved, written
before the child starts), `<sid>.later` (look again after LATER_S),
`<sid>.tries` (failed `tap record`s), `<sid>.moved` (the service took it).
Claude Code, Codex and Kimi Code (whose entries run this route through
`harness_io.py`, the installed copy's since plugin 0.175.0); another agent's
sessions are left alone. Silent always
(a prompt hook's stdout reaches the model), exit 0 always. Stdlib only,
Python 3.9.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

#: The first tap whose `tap watch` hands its session to the Probe service.
HANDBACK_TAP = (0, 10, 13)
#: The first tap on protocol 2: a session an older one records cannot be taken
#: over (the tap's session-start.sh PROTOCOL2_TAP).
PROTOCOL2_TAP = (0, 4, 5)
#: The capture plugin, in every agent's plugin cache.
TAP_PLUGIN = "probe-research-tap"
#: Where the tap keeps a session's watcher files (`tap/config.py`
#: WATCHER_DIR_ENV): `/tmp` unless this names another folder.
WATCHER_DIR_ENV = "PROBE_TAP_WATCHER_DIR"
DEFAULT_WATCHER_DIR = "/tmp"
#: "0": the service never records a session here (`tap/record_client.py`
#: HOSTED_ENV): a definite no, never asked again.
HOSTED_ENV = "PROBE_TAP_HOSTED"
#: How long the old wrapper's group gets to make its last pass and go (the
#: tap's session-start.sh waits 3 s inside its own 10 s budget).
STOP_WAIT_S = 3.0
#: `tap record`'s own bound is 6 s (its TOTAL_S); this is the child's.
RECORD_TIMEOUT_S = 20.0
#: A look that could not act yet (no newer tap installed, a dead pid file, a
#: failed `tap record`, a moved session to heal) is made again this often. The
#: route's shell reads the same bound (`find -mmin -10`).
LATER_S = 10 * 60
#: Failed `tap record`s before the session is left on its watcher for good.
MAX_TRIES = 3
#: How far ahead the swap dates the old tap's heal stamp (its healer is quiet
#: while the stamp is under ten minutes old); dated again at every swap and heal.
HEAL_QUIET_S = 30 * 24 * 3600
#: The session's daemon worker is asked to hand over this far ahead
#: (`probe.service.recorders.HANDOVER_AHEAD_S`).
HANDOVER_AHEAD_S = 180.0
#: What the child does: move a live old watcher, or record a moved session again.
SWAP, HEAL = "swap", "heal"
#: A versioned plugin folder in an old wrapper's command line (the tap's
#: session-start.sh reads the same, for taps that wrote no `.version`).
#: Kimi Code's folders are `managed/probe-research-tap@<version>`.
_VERSIONED = re.compile(r"/(probe-research-tap|prbe-cc-tap-plugin|prbe-codex-tap-plugin)[/@](\d+\.\d+\.\d+)(?:/|\s|$)")
_SID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
#: How each agent's installed tap is found, by the plugin-root variable its
#: registry row names (`plugin.root_env`): Claude Code's install ledger,
#: Codex's plugin cache (one version folder per install), or Kimi Code's plugin
#: list (`installed.json` beside its `managed/<id>@<version>` folders). Another
#: agent's sessions are left alone.
LEDGER, CODEX_CACHE, KIMI_LIST = "ledger", "codex-cache", "kimi-list"
_FINDS = {"CLAUDE_PLUGIN_ROOT": LEDGER, "PLUGIN_ROOT": CODEX_CACHE, "KIMI_PLUGIN_ROOT": KIMI_LIST}
#: No pid here proves anything: Git Bash's pids are not Windows's, and
#: `os.kill(pid, 0)` there sends a console control event.
_WINDOWS = os.name == "nt"

HERE = os.path.dirname(os.path.abspath(__file__))


def _sibling(name: str):
    """A module beside this file, by explicit path."""
    import importlib.util

    key = f"_probe_hooks.{name}"
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def numbers(version: object) -> tuple:
    if not isinstance(version, str) or not re.match(r"^\d+(\.\d+)+$", version.strip()):
        return ()
    return tuple(int(p) for p in version.strip().split("."))


def watcher_dir(env=None) -> str:
    values = os.environ if env is None else env
    given = values.get(WATCHER_DIR_ENV) or ""
    return given if os.path.isabs(given) else DEFAULT_WATCHER_DIR


def marker_path(sid: str, env=None) -> str:
    values = os.environ if env is None else env
    base = values.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "probe", "hooks", "tap-handover", sid)


def _cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as handle:
            return handle.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        pass
    import subprocess

    try:
        return subprocess.run(  # noqa: S603 - a fixed binary, no shell
            ["ps", "-ww", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def _ours(command: str, sid: str) -> bool:
    """Is this command line this session's `tap watch` wrapper (the tap's own
    `*"tap watch"*"<sid>"*`)? The wrapper is `bash -c '<script>' wrapper <sid>
    ...`, and its script spans lines: matched across them."""
    at = command.find("tap watch")
    return at >= 0 and sid in command[at:]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _own_file(path: str) -> bool:
    """A regular file of this user's own (never followed through a link)."""
    import stat

    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode) and (not hasattr(os, "getuid") or info.st_uid == os.getuid())


def _pid_in(path: str) -> "int | None":
    """The pid a watcher file of this user's own names, else None."""
    if not _own_file(path):
        return None  # the watcher dir is shared: another user's file names nothing of ours
    try:
        with open(path, encoding="utf-8") as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 1 else None


def _gone(pid: int, sid: str) -> bool:
    """Is `pid` provably not this session's `tap watch`: dead, or alive with a
    readable command line that is something else (a pid reused since)?"""
    if not _alive(pid):
        return True
    command = _cmdline(pid)
    return bool(command.strip()) and not _ours(command, sid)


def watcher(prefix: str, sid: str, env=None) -> dict | None:
    """This session's live `tap watch` wrapper, proven: `{pid, version, pid_file,
    version_file}`, or None (no pid file, a dead pid, a command line that is
    not this session's `tap watch`)."""
    stem = os.path.join(watcher_dir(env), f"{prefix}-watcher-{sid}")
    pid = _pid_in(stem + ".pid")
    if pid is None or not _alive(pid):
        return None
    command = _cmdline(pid)
    if not _ours(command, sid):
        return None
    try:
        version = open(stem + ".version", encoding="utf-8").read().strip() if _own_file(stem + ".version") else ""
    except OSError:
        version = ""
    if not numbers(version):
        found = _VERSIONED.search(command)
        version = found.group(2) if found else ""
    return {"pid": pid, "version": version, "pid_file": stem + ".pid", "version_file": stem + ".version"}


def watchers_movable(prefix: str, env=None) -> bool:
    """Can this route move every live `tap watch` of this agent's sessions
    (each one's tap known and on protocol 2), so an update needs no restart
    of them? Asked by the update notice (`version_check.py`) before it says
    so. Never on Windows (no pid proven there); a dead or unproven pid file
    names no session. Never raises."""
    if _WINDOWS:
        return False
    import glob

    try:
        found = glob.glob(os.path.join(glob.escape(watcher_dir(env)), f"{glob.escape(prefix)}-watcher-*.pid"))
    except OSError:
        return True
    head = f"{prefix}-watcher-"
    for path in found:
        sid = os.path.basename(path)[len(head):-len(".pid")]
        if not _SID.match(sid):
            continue
        live = watcher(prefix, sid, env)
        if live is not None and (not numbers(live["version"]) or numbers(live["version"]) < PROTOCOL2_TAP):
            return False
    return True


def _service_records(prefix: str, sid: str, env=None) -> bool:
    """Does the session's `.hosted` marker name a live Probe service (its
    command line says `probe.service`, as `tap/record_client.py` proves it)?"""
    pid = _pid_in(os.path.join(watcher_dir(env), f"{prefix}-watcher-{sid}.hosted"))
    return pid is not None and _alive(pid) and "probe.service" in _cmdline(pid)


def _manifest_version(root: str, manifest_dir: str) -> str:
    try:
        with open(os.path.join(root, manifest_dir, "plugin.json"), encoding="utf-8") as handle:
            version = json.load(handle).get("version")
    except (OSError, ValueError, AttributeError):
        version = None
    return version if isinstance(version, str) and numbers(version) else os.path.basename(root)


def _hands_back(root: str) -> bool:
    tap = os.path.join(root, "tap")
    return all(os.path.isfile(os.path.join(tap, name)) for name in ("handback.py", "record_client.py", "hosted.py"))


def installed_tap(finds: str, env=None) -> tuple[str, str] | None:
    """`(root, version)` of the tap this agent has installed, when it carries
    the hand-back; else None. LEDGER (Claude Code): the one folder its install
    ledger keeps under `probe-research-tap@<marketplace>` (this plugin's
    marketplace when this copy sits in its cache, else the only such key).
    CODEX_CACHE: the newest version folder of the tap in Codex's plugin cache.
    KIMI_LIST: the one folder Kimi's plugin list records for the tap, in the
    `managed/` folder this plugin's own copy sits in (`dispatch.kimi_installed_root`'s
    anchor: no variable decides where to look)."""
    values = os.environ if env is None else env
    dispatch = _sibling("dispatch")
    root_here = os.path.dirname(HERE)
    if finds == LEDGER:
        copies = dispatch.installed_copies(values) or {}
        key = TAP_PLUGIN + "@" + dispatch.ledger_key(root_here).split("@", 1)[-1]
        keys = [key] if key in copies else [k for k in copies if k.split("@", 1)[0] == TAP_PLUGIN]
        if len(keys) != 1 or len(copies[keys[0]]) != 1:
            return None
        (root,) = copies[keys[0]]
        manifest = ".claude-plugin"
    elif finds == CODEX_CACHE:
        cache = os.path.realpath(dispatch._codex_cache(values))
        here = os.path.realpath(root_here)
        markets = [os.path.dirname(os.path.dirname(here))] if os.path.dirname(os.path.dirname(os.path.dirname(here))) == cache else []
        markets = markets or [os.path.join(cache, name) for name in dispatch._listdir(cache)]
        found = []
        for market in markets:
            folder = os.path.join(market, TAP_PLUGIN)
            for name in dispatch._listdir(folder):
                version = dispatch._version(name)
                path = os.path.join(folder, name)
                if version is not None and os.path.isfile(os.path.join(path, ".codex-plugin", "plugin.json")):
                    found.append((version, path))
        if not found:
            return None
        root = max(found)[1]
        manifest = ".codex-plugin"
    elif finds == KIMI_LIST:
        found = _kimi_recorded(os.path.realpath(root_here), dispatch)
        if found is None:
            return None
        root = found
        manifest = ".kimi-plugin"
    else:
        return None
    if not _hands_back(root):
        return None
    return root, _manifest_version(root, manifest)


def _kimi_recorded(here: str, dispatch) -> "str | None":
    """The tap folder Kimi's plugin list records (`<kimi>/plugins/installed.json`,
    one level above the `managed/` folder `here` sits in), when it is a
    `managed/probe-research-tap@<version>` folder with Kimi's manifest; else None."""
    managed = os.path.dirname(here)
    if os.path.basename(managed) != dispatch.KIMI_MANAGED:
        return None
    try:
        with open(os.path.join(os.path.dirname(managed), dispatch.KIMI_LIST), encoding="utf-8") as handle:
            records = json.load(handle)["plugins"]
        roots = {os.path.realpath(r["root"]) for r in records
                 if isinstance(r, dict) and r.get("id") == TAP_PLUGIN and isinstance(r.get("root"), str)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    if len(roots) != 1:
        return None
    root = roots.pop()
    if os.path.dirname(root) != managed or not os.path.basename(root).startswith(TAP_PLUGIN + "@"):
        return None
    return root if os.path.isfile(os.path.join(root, ".kimi-plugin", "plugin.json")) else None


def decide(payload: dict, env=None) -> dict | None:
    """What to do for this prompt's session, or None. Never raises. A plan
    with `skip` is never looked at again; with `later`, again after LATER_S;
    else `kind` is SWAP or HEAL, for the detached child."""
    values = os.environ if env is None else env
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not _SID.match(sid):
        return None
    if _WINDOWS:
        return {"sid": sid, "skip": "Windows: no pid of a Git Bash wrapper can be proven here"}
    harness = _sibling("_hook_harness").current(values)
    root_env = (harness.plugin or {}).get("root_env")
    finds = _FINDS.get(root_env)
    capture = getattr(harness, "capture", None)
    prefix = getattr(capture, "watcher_prefix", None)
    if finds is None or not prefix:
        return {"sid": sid, "skip": f"{harness.id}'s installed tap is not looked for"}
    if values.get(HOSTED_ENV) == "0":
        return {"sid": sid, "skip": f"{HOSTED_ENV}=0: the Probe service records no session here"}
    stem = os.path.join(watcher_dir(values), f"{prefix}-watcher-{sid}")
    named = _pid_in(stem + ".pid")
    state_dir = str(capture.state_dir(values))
    moved = os.path.exists(marker_path(sid, values) + ".moved")
    live = watcher(prefix, sid, values) if named is not None else None
    if named is not None and live is None and moved and _gone(named, sid):
        # A wrapper the old tap started again in a moved session (a compact)
        # that gave up, or a pid reused since: its pid file names nothing, and
        # the quieted old healer never removes it. Gone, so it cannot keep the
        # heal below from ever running. A live pid whose command line cannot be
        # read is kept (one look per LATER_S below): it may be that wrapper.
        _remove_if(stem + ".pid", named)
        named = None
    if named is None:
        if not moved or _service_records(prefix, sid, values):
            return None
        if os.path.exists(os.path.join(state_dir, ".disabled")):
            return {"sid": sid, "later": "capture is off"}
        kind = HEAL
    else:
        kind = SWAP
        if live is None:
            # A wrapper that gave up, or a pid reused since: one look per LATER_S.
            return {"sid": sid, "later": f"pid {named} is no live tap watch of this session"}
        version = numbers(live["version"])
        if not version or version >= HANDBACK_TAP or version < PROTOCOL2_TAP:
            return {"sid": sid, "skip": f"tap {live['version'] or 'unknown'} needs no hand-over"}
    # Off means not captured (owner, 2026-10-24): neither a move nor a heal for
    # a session whose Probe switch is `off` (`dispatch.session_off`, the
    # machine's default when the session has none; unreadable reads as off),
    # as the tap's `.disabled` above stops the heal. Looked at again later: one
    # switched back on is moved then.
    if _sibling("dispatch").session_off(payload):
        return {"sid": sid, "later": "this session's Probe switch is off"}
    newest = installed_tap(finds, values)
    if newest is None or (live is not None and numbers(newest[1]) <= numbers(live["version"])):
        # Not yet, maybe: the tap's own update can land after this plugin's.
        return {"sid": sid, "later": "no newer tap with the hand-back is installed"}
    transcript = payload.get("transcript_path")
    if finds != CODEX_CACHE and not (isinstance(transcript, str) and os.path.isabs(transcript)):
        # Only Codex's rollout is found from its folder (`_record`); Claude Code
        # and Kimi give the transcript in the payload (Kimi's adapter, harness_io).
        return {"sid": sid, "later": "no transcript path in this event"}
    cwd = payload.get("cwd")
    plan = {
        "kind": kind, "sid": sid, "agent": harness.id, "root_env": root_env, "prefix": prefix,
        "root": newest[0], "version": newest[1], "state_dir": state_dir,
        "cwd": cwd if isinstance(cwd, str) and os.path.isabs(cwd) else os.getcwd(),
        "transcript": transcript if isinstance(transcript, str) and os.path.isabs(transcript) else None,
        "log": os.path.join(state_dir, "logs", f"{sid}.log"),
        "markers": os.path.dirname(marker_path(sid, values)),
    }
    if live is not None:
        plan.update({"pid": live["pid"], "old": live["version"], "pid_file": live["pid_file"],
                     "version_file": live["version_file"],
                     # The service records the session already (a wrapper the old
                     # tap started again): the worker holding its lock is the
                     # service's own, never asked to hand over.
                     "hosted_before": _service_records(prefix, sid, values)})
    return plan


def _create(path: str) -> bool:
    """Create `path` (its folder too); False when it is there already."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False  # FileExistsError: another prompt of this session got here first
    os.close(fd)
    return True


def _later(sid: str, mark: bool = False, base: "str | None" = None) -> bool:
    """A look that could not act yet is made again at most every LATER_S:
    `<marker>.later`'s age. `mark`: start that wait now."""
    path = (os.path.join(base, sid) if base else marker_path(sid)) + ".later"
    if mark:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8"):
                pass
        except OSError:
            pass
        return True
    try:
        return time.time() - os.stat(path).st_mtime < LATER_S
    except OSError:
        return False


def _child_env() -> dict:
    """This environment without what the hook dispatcher set for its own call
    (`dispatch.OWNED_ENV`: a deadline, a host): never the detached child's,
    nor the session's environment the service saves from `tap record`."""
    try:
        owned = set(_sibling("dispatch").OWNED_ENV)
    except Exception:  # noqa: BLE001 - a broken sibling: the names it has today
        owned = {"PROBE_HOOKS_HANDED_OVER", "PROBE_HOOK_HOSTED", "PROBE_HOOK_HOST", "PROBE_HOOK_DEADLINE",
                 "PROBE_HOOK_PARENT_PID"}
    return {k: v for k, v in os.environ.items() if k not in owned}


def hook(stdin) -> int:
    try:
        payload = json.load(stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(payload, dict):
        return 0
    sid = payload.get("session_id")
    if isinstance(sid, str) and _SID.match(sid) and (os.path.exists(marker_path(sid)) or _later(sid)):
        return 0
    try:
        plan = decide(payload)
    except Exception:  # noqa: BLE001 - a prompt hook never fails the prompt
        return 0
    if plan is None:
        return 0
    if "later" in plan:
        _later(plan["sid"], mark=True)
        return 0
    if "skip" in plan:
        _create(marker_path(plan["sid"]))
        return 0
    if plan["kind"] == SWAP:
        if not _create(f"{marker_path(plan['sid'])}.{plan['pid']}"):
            return 0
    else:
        _later(plan["sid"], mark=True)  # a heal is tried at most every LATER_S
    import subprocess

    try:
        subprocess.Popen(  # noqa: S603 - this interpreter, this file
            [sys.executable, os.path.abspath(__file__), "swap", json.dumps(plan)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=_child_env(), cwd="/", close_fds=True, start_new_session=True,
        )
    except OSError:
        pass
    return 0


# -- the detached child -----------------------------------------------------------


def _log(plan: dict, message: str) -> None:
    try:
        os.makedirs(os.path.dirname(plan["log"]), exist_ok=True)
        with open(plan["log"], "a", encoding="utf-8") as handle:
            handle.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}] tap-handover: {message}\n")
    except OSError:
        pass


def tap_python(root: str) -> str | None:
    """The interpreter the tap runs on (3.11+): the newest tap's own choice
    (`hooks/tap-python.sh`), else this one, else python3.14 .. python3.11."""
    import shutil
    import subprocess

    script = os.path.join(root, "hooks", "tap-python.sh")
    if os.path.isfile(script):
        try:
            found = subprocess.run(  # noqa: S603 - the installed tap's own script
                ["/bin/bash", "-c", '. "$1/hooks/tap-python.sh" && tap_python "$1"', "_", root],
                capture_output=True, text=True, timeout=30, check=False, stdin=subprocess.DEVNULL,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            found = ""
        if found and os.access(found, os.X_OK):
            return found
    if sys.version_info >= (3, 11):
        return sys.executable
    for minor in (14, 13, 12, 11):
        found = shutil.which(f"python3.{minor}")
        if found:
            return found
    return None


def _group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop_wrapper(pid: int, sid: str, wait: float = STOP_WAIT_S) -> bool:
    """SIGTERM this session's wrapper (its whole group when it leads one: its
    TERM trap forwards to `tap watch`, which makes its last pass), then wait up
    to `wait` for it. Proven again first: a pid reused since is never touched.
    True when it is gone."""
    import signal

    if not _alive(pid) or not _ours(_cmdline(pid), sid):
        return not _alive(pid)
    try:
        leads = os.getpgid(pid) == pid
    except OSError:
        leads = False
    try:
        if leads:
            os.killpg(pid, signal.SIGTERM)
        else:
            os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    until = time.monotonic() + wait
    while time.monotonic() < until:
        if not (_group_alive(pid) if leads else _alive(pid)):
            return True
        time.sleep(0.1)
    return not _alive(pid)


def _remove_if(path: str, pid: "int | None" = None) -> None:
    """Remove `path` (only while it still names `pid`, when given)."""
    try:
        if pid is not None and open(path, encoding="utf-8").read().strip() != str(pid):
            return
        os.unlink(path)
    except (OSError, ValueError):
        pass


def _flock_held(path: str) -> bool:
    """Is this lock file held by anyone (`probe.service.updater._flock_held`)?
    A shared, non-blocking probe released at once; absent: no."""
    try:
        handle = open(path, "rb")  # noqa: SIM115 -- closed below
    except FileNotFoundError:
        return False
    except OSError:
        return True
    try:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def ask_worker_to_hand_over(sid: str, env=None) -> bool:
    """Before the old wrapper is stopped: its companion stops the session's
    daemon worker, which would end the session as stopped (its lease released
    `stopped`: "recording is back with you"). Dated HANDOVER_AHEAD_S ahead, the
    `.handover` marker beside its lock has it hand over instead, as at a
    service's restart (`RecorderHost._hand_over`); the service's own worker
    takes the lock and clears it. Only while a worker holds the lock."""
    values = os.environ if env is None else env
    base = values.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    lock = os.path.join(base, "probe", "daemon", f"{sid}.lock")
    try:
        if not _flock_held(lock):
            return False
        marker = lock[: -len(".lock")] + ".handover"
        with open(marker, "a", encoding="utf-8"):
            pass
        ahead = time.time() + HANDOVER_AHEAD_S
        os.utime(marker, (ahead, ahead))
        return True
    except OSError:
        return False


def quiet_old_healer(state_dir: str, sid: str) -> None:
    """Date the old tap's heal stamp (`<tap state>/heal/<sid>`, its
    `config.heal_marker`) HEAL_QUIET_S ahead: its `ensure-daemon.sh` respawns
    no wrapper while the stamp is under ten minutes old. The same folder its
    hooks compute (`capture.state_dir`: the override, else the current folder,
    else a legacy one): never one that would turn a legacy folder off."""
    path = os.path.join(state_dir, "heal", sid)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
        ahead = time.time() + HEAL_QUIET_S
        os.utime(path, (ahead, ahead))
    except OSError:
        pass


def _record(plan: dict, python: str) -> "int | None":
    """`tap record` with the newest tap, as its own hooks run it: its exit code,
    None when it could not run."""
    import subprocess

    if plan.get("transcript"):
        source = ["--transcript", plan["transcript"]]
    else:
        source = ["--transcript-dir", os.environ.get("PRBE_CODEX_SESSIONS_DIR")
                  or os.path.join(os.path.expanduser("~"), ".codex", "sessions")]
    env = _child_env()
    env.update({"PYTHONPATH": plan["root"], "PROBE_TAP_SOURCE": plan["agent"]})
    # As the tap's own hooks run: its folder is the agent's plugin root.
    env[plan["root_env"]] = plan["root"]
    try:
        return subprocess.run(  # noqa: S603 - the installed tap, our own arguments
            [python, "-m", "tap", "record", "--session-id", plan["sid"], "--cwd", plan["cwd"], *source,
             "--plugin-root", plan["root"]],
            env=env, cwd=plan["cwd"] if os.path.isdir(plan["cwd"]) else "/", capture_output=True, text=True,
            timeout=RECORD_TIMEOUT_S, check=False, stdin=subprocess.DEVNULL,
        ).returncode
    except (OSError, subprocess.SubprocessError) as exc:
        _log(plan, f"tap record could not run: {exc}")
        return None


def _tries(base: str, sid: str, add: int = 0) -> int:
    path = os.path.join(base, sid) + ".tries"
    try:
        with open(path, encoding="utf-8") as handle:
            count = int(handle.read().strip() or 0)
    except (OSError, ValueError):
        count = 0
    if add:
        count += add
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(str(count))
        except OSError:
            pass
    return count


def _not_taken(plan: dict, why: str) -> None:
    """The service would not record it: the old watcher keeps recording, and
    the swap is tried again after LATER_S, MAX_TRIES times in all (a heal, at
    every LATER_S: its wait started at the prompt)."""
    base, sid = plan["markers"], plan["sid"]
    if plan["kind"] != SWAP:
        _log(plan, f"this moved session is recorded by nothing: {why}; tried again within {LATER_S // 60} min")
        return
    _remove_if(os.path.join(base, f"{sid}.{plan['pid']}"))
    if _tries(base, sid, add=1) >= MAX_TRIES:
        _create(os.path.join(base, sid))
        _log(plan, f"tap {plan['old']} watcher (pid {plan['pid']}) kept for good: {why} ({MAX_TRIES} tries)")
        return
    _later(sid, mark=True, base=base)
    _log(plan, f"tap {plan['old']} watcher (pid {plan['pid']}) kept: {why}; tried again within {LATER_S // 60} min")


def swap(plan: dict) -> int:
    python = tap_python(plan["root"])
    if python is None:
        _not_taken(plan, f"no Python 3.11+ for tap {plan['version']}")
        return 0
    started = time.monotonic()
    code = _record(plan, python)
    if code != 0:
        _not_taken(plan, f"the Probe service would not record this session (tap record exit {code})")
        return 0
    quiet_old_healer(plan["state_dir"], plan["sid"])
    base, sid = plan["markers"], plan["sid"]
    if plan["kind"] == HEAL:
        _log(plan, f"the Probe service records this moved session again, on tap {plan['version']}")
        return 0
    if not plan.get("hosted_before"):
        ask_worker_to_hand_over(sid)
    gone = stop_wrapper(int(plan["pid"]), sid)
    _remove_if(plan["pid_file"], int(plan["pid"]))
    _remove_if(plan["version_file"])
    # Once more: the service's recorder stepped back while the old watcher held
    # the session, and this ask has it take the session now (a service before
    # it takes it at its next look, within 15 s).
    _record(plan, python)
    _create(os.path.join(base, sid) + ".moved")
    for name in (".later", ".tries"):
        _remove_if(os.path.join(base, sid) + name)
    _log(plan, f"the Probe service records this session on tap {plan['version']}; the tap {plan['old']} watcher "
               f"(pid {plan['pid']}) {'stopped' if gone else 'was told to stop'} "
               f"({time.monotonic() - started:.1f} s)")
    return 0


def main(argv: list | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["swap"] and len(argv) > 1:
        try:
            return swap(json.loads(argv[1]))
        except Exception:  # noqa: BLE001 - detached and silent
            return 0
    try:
        return hook(sys.stdin)
    except Exception:  # noqa: BLE001 - a prompt hook never fails the prompt
        return 0


if __name__ == "__main__":
    sys.exit(main())
