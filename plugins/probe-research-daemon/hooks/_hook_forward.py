"""A hook event's way to the Probe service when a harness runs this plugin's dispatcher itself (service-only task 13).

Plumbing only (the plan's rule): package the event, send it as `hooks.run {plugin_root, lane,
harness, event_id}`, start the service when none answers (`python -I -m probe.service.client
ensure`, at most every ENSURE_EVERY_S, then one more send), print the answer. No route ever runs
here. What happens when no answer comes:

  unreachable      no service took the connection (after `ensure`), PROBE_HOOK_SERVICE=off, or an
                   older service that cannot run it (an error answer, a refusal with no reason):
                   every lane goes on (D4); a record-keeping one is spooled (below), unless off
  out of time      the service took it and gave no answer within the lane's budget: every lane
                   goes on (a slow service never blocks a person's prompt); a record-keeping one
                   is spooled under the id it was sent with (the service may have run it)
  not_harness      the service's word that no hook of a running harness sent it (a forgery):
                   a gate (GATE_LANES: a tool call, a typed prompt) is DENIED, exit 2; it only
                   ever stops the forger. Nothing is spooled for it
  too big          a tool call too big to check (below) is DENIED
  other refusals   the plugin folder is incomplete or broken, or another reason: every lane goes
                   on, with one line on stderr (`probe doctor` for a broken folder); a
                   record-keeping one is spooled

The spool is the service's queue folder (`<service dir>/hooks-queue/<event_id>.json`, 0600, in a
private folder), which the next service drains. The event goes there under the id it was sent
with, marked `sent`, so the drain runs it only if it can still claim that id: an event the service
ran when it was sent never runs twice. Written only where a service will drain it: a Probe CLI is
installed here (`cli_here`), fewer than MAX_QUEUED wait, and, when no service was reached, the
folder's `drained-by` stamp is the CLI version the next service starts as. The spooled environment
keeps no secret but Probe's own key (`PROBE_TOKEN`, which the routes need to reach Probe).

A tool call is sent with the fields Probe's guard reads only: the bulk of an edit or a write
(GATE_BULK_FIELDS) never travels, and a call still too big to send is denied, never let pass.

A session start whose service will not start runs the lifeboat, the one repair a dead service
cannot make itself. A Codex session's start and prompts touch its hooks' heartbeat (`heartbeat`),
which the capture plugin reads to tell whether Codex runs Probe's hooks at all.

Stdlib only, Python 3.9, loaded by explicit path (`dispatch.forward`). Never raises.
"""

from __future__ import annotations

import json
import os
import sys
import time

#: The gates: what the agent waits on. Denied when the service was reached and did not run them.
GATE_LANES = frozenset({"pre-tool", "prompt"})
#: Lanes never spooled: nothing to record late (the frozen shim's NO_QUEUE_EVENTS).
NEVER_SPOOLED = frozenset({"pre-tool"})
#: The service's refusals with a reason (`probe.service.hooks.hooks`); one with no reason is an older
#: service's: unreachable. Only `not_harness` (no hook of a running harness sent it: a forgery) denies
#: a gate, and is never spooled; a broken folder lets every lane go on, naming `probe doctor`.
NOT_HARNESS = "not_harness"
INCOMPLETE_FOLDER = "incomplete_folder"
DENY_REASONS = frozenset({NOT_HARNESS})
#: A tool call's input fields Probe's guard never reads (`_session_marker.touches_*`,
#: `tracking_guard`): the body of a write or an edit. Dropped before the event is sent.
GATE_BULK_FIELDS = frozenset({"content", "old_string", "new_string", "edits", "new_source", "oldText", "newText",
                              "old_str", "new_str", "file_text"})
#: A gate's payload after that, at most; and the whole request line, at most (`wire.MAX_LINE_BYTES`).
MAX_GATE_PAYLOAD = 256 * 1024
#: A typed prompt is sent cut to its first this many characters: the switch is read from its start,
#: and a pasted megabyte never blocks the person's prompt.
MAX_PROMPT_CHARS = 128 * 1024
MAX_LINE = 1024 * 1024
#: Kept back from the event's budget for the answer to travel and print.
MARGIN_S = 0.1
#: An ensure is not started with less time than this left, and waits at most ENSURE_WAIT_S.
ENSURE_MIN_S = 0.5
ENSURE_WAIT_S = 2.0
#: An ensure is started at most this often (its `ensured` mark); a connect that found no service is
#: not tried again for DOWN_RETRY_S (its `down` mark).
ENSURE_EVERY_S = 10.0
DOWN_RETRY_S = 2.0
#: The connect and hello, at most.
CONNECT_S = 0.3
#: What of the environment the service is handed (the frozen shim's allowlist, `probe-hooks/hooks/hook.py`).
ENV_NAMES = frozenset((
    "HOME", "PATH", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LANGUAGE", "TZ", "TERM",
    "VIRTUAL_ENV", "CONDA_PREFIX", "PLUGIN_ROOT", "PLUGIN_DATA",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "GIT_SSL_CAINFO", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR", "PIPX_HOME",
))
ENV_PREFIXES = ("PROBE_", "PRBE_", "CLAUDE", "CODEX_", "KIMI_", "PI_", "XDG_", "LC_")
#: What the dispatcher sets for itself: never the event's (the service sets its own).
OWNED_ENV = frozenset(("PROBE_HOOKS_HANDED_OVER", "PROBE_HOOK_DEADLINE", "PROBE_HOOK_PARENT_PID", "PROBE_HOOK_VIA",
                       "PROBE_HOOK_HOST", "PROBE_HOOK_HOSTED"))
MAX_ENV = 200
MAX_ENV_VALUE = 8192
#: A spooled event is a file on disk: no name that looks like a secret but Probe's own key,
#: no proxy URL with a user in it (`protocol.hook_queue_env` keeps less still).
QUEUE_SECRET_WORDS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "COOKIE", "AUTH")
QUEUE_CREDENTIAL = "PROBE_TOKEN"
MAX_QUEUED_BYTES = 256 * 1024
#: Events waiting in the queue at once, at most (`hookqueue.MAX_QUEUED`).
MAX_QUEUED = 128
#: Where a Probe CLI install shows, under $HOME (`paths.runtime_path`, `paths.fallback_python`):
#: with neither, no service ever starts here to drain a spooled event.
CLI_MARKS = (
    (".local", "state", "probe", "service", "runtime.json"),
    (".local", "share", "uv", "tools", "probe-research", "bin", "python"),
)
#: `PROBE_HOOK_SERVICE=off` (or the older `PROBE_READS_SERVICE=off`): never ask, start or spool.
SERVICE_ENVS = ("PROBE_HOOK_SERVICE", "PROBE_READS_SERVICE")
#: The lifeboat (`probe.service.lifeboat`): its frozen wrapper under $HOME, run detached at most
#: every LIFEBOAT_EVERY_S (its stamp). tests/test_hook_miss.py holds these to the service's own.
LIFEBOAT_DIR = (".local", "state", "probe", "lifeboat")
LIFEBOAT_WRAPPER = "lifeboat.sh"
LIFEBOAT_STAMP = "session.stamp"
LIFEBOAT_EVERY_S = 3600.0
#: A Codex session's hooks run (`hooks/probe-hooks-trust.sh` of the capture plugin reads it): its
#: start and prompts touch `$XDG_STATE_HOME/probe/hooks/alive/<session id>`, kept HEARTBEAT_KEEP_S.
HEARTBEAT_LANES = ("session-start", "prompt")
HEARTBEAT_KEEP_S = 14 * 24 * 3600

_HERE = os.path.dirname(os.path.abspath(__file__))


def _sibling(name: str):
    """A module beside this file, by explicit path (never a bare import: a project may carry one)."""
    import importlib.util  # noqa: PLC0415

    key = "_probe_hooks." + name
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, os.path.join(_HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def service_off(environ) -> bool:
    return any(environ.get(name) == "off" for name in SERVICE_ENVS)


def pick_env(environ) -> dict:
    """The environment the service is handed: ENV_NAMES and ENV_PREFIXES, within its caps."""
    kept = {}
    for name in sorted(environ):
        value = environ[name]
        if name in OWNED_ENV or not (name in ENV_NAMES or name.startswith(ENV_PREFIXES)):
            continue
        if "=" not in name and len(name) <= 256 and len(value) <= MAX_ENV_VALUE and "\0" not in value:
            kept[name] = value
            if len(kept) >= MAX_ENV:
                break
    return kept


def queue_env(env: dict) -> dict:
    """`env` as a spooled event may keep it."""
    kept = {}
    for name, value in env.items():
        if name.upper().endswith("_PROXY") and "@" in value.split("://", 1)[-1].split("/", 1)[0]:
            continue
        if name != QUEUE_CREDENTIAL and any(word in name.upper() for word in QUEUE_SECRET_WORDS):
            continue
        kept[name] = value
    return kept


def trimmed(lane: str, payload: dict) -> dict:
    """The event as it is sent: a tool call's without the bulk of its input (GATE_BULK_FIELDS: what the
    guard never reads), a prompt's cut to MAX_PROMPT_CHARS."""
    if lane == "prompt":
        prompt = payload.get("prompt")
        return {**payload, "prompt": prompt[:MAX_PROMPT_CHARS]} if isinstance(prompt, str) \
            and len(prompt) > MAX_PROMPT_CHARS else payload
    if lane not in ("pre-tool", "post-tool"):
        return payload
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict) or not GATE_BULK_FIELDS & set(tool_input):
        return payload
    return {**payload, "tool_input": {k: v for k, v in tool_input.items() if k not in GATE_BULK_FIELDS}}


class Reach:
    """`ask`'s outcomes besides an answer."""

    DOWN = "down"  # no service took the connection: start one
    OLDER = "older"  # a service answered that cannot run it (an error answer, a refusal with no reason)
    NO_ANSWER = "no_answer"  # sent, and no answer by the deadline (or the line broke): it may have run


class Refused:
    """The service would not run it, and said why (`reason`: NOT_HARNESS, INCOMPLETE_FOLDER, ...)."""

    def __init__(self, why: str, reason: str) -> None:
        self.why = why
        self.reason = reason


def ask(params: dict, deadline: float):
    """`hooks.run` on this machine's service: its answer (a dict), a Refused, or a Reach."""
    rpc_module = _sibling("_service_rpc")
    rpc = rpc_module.Rpc.connect(max(min(CONNECT_S, deadline - time.monotonic()), 0.01))
    if rpc is None:
        return Reach.DOWN
    try:
        params = {**params, "timeout_ms": max(1, min(int((deadline - time.monotonic()) * 1000), 60000))}
        answer = rpc.call("hooks.run", params, timeout=max(deadline - time.monotonic(), 0.01))
    except rpc_module.Failed as failed:
        return Reach.OLDER if failed.answered else Reach.NO_ANSWER
    finally:
        rpc.close()
    if not isinstance(answer, dict):
        return Reach.NO_ANSWER
    if "refused" in answer:
        reason = answer.get("reason")
        if isinstance(reason, str) and reason:
            return Refused(str(answer.get("refused")), reason)
        return Reach.OLDER
    return answer


def service_mark(kind: str) -> str:
    """`/tmp/probe-<uid>/service-<hash of $HOME>.<kind>`: this client's own marks (`down`,
    `ensured`), in the hosts' private folder, one set per HOME, read with a stat."""
    import hashlib  # noqa: PLC0415

    home = os.path.realpath(os.path.expanduser("~"))
    digest = hashlib.sha256(os.fsencode(home)).hexdigest()[:16]
    return os.path.join("/tmp", "probe-%d" % os.getuid(), "service-%s.%s" % (digest, kind))


def _fresh(path: str, seconds: float) -> bool:
    try:
        return time.time() - os.stat(path).st_mtime < seconds
    except OSError:
        return False


def _touch(path: str) -> bool:
    """Mark `path` now, in a private folder only (made if missing)."""
    folder = os.path.dirname(path)
    try:
        if not os.path.isdir(folder):
            os.makedirs(folder, mode=0o700, exist_ok=True)
        if not _private(folder):
            return False
        with open(path, "a"):
            pass
        os.utime(path, None)
        return True
    except OSError:
        return False


def _runtime() -> dict:
    """runtime.json (`client.write_runtime`): the CLI's interpreter and version."""
    try:
        with open(_sibling("_service_paths").runtime_path(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, AttributeError):
        return {}
    return data if isinstance(data, dict) else {}


def runtime_python() -> "str | None":
    """The CLI's interpreter (runtime.json's, else the uv tool env's): what `ensure` runs on."""
    candidates = []
    python = _runtime().get("python")
    if isinstance(python, str):
        candidates.append(python)
    candidates.append(str(_sibling("_service_paths").fallback_python()))
    for candidate in candidates:
        if os.path.isabs(candidate) and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def cli_here() -> bool:
    """Is a Probe CLI installed for this user? Two stats: with none, no service ever starts here."""
    home = os.path.expanduser("~")
    return any(os.path.exists(os.path.join(home, *mark)) for mark in CLI_MARKS)


def ensure(deadline: float) -> bool:
    """Start the service the CLI's own way (`probe.service.client ensure`), at most every
    ENSURE_EVERY_S, waiting for it within the event's budget. True when it says one runs."""
    left = deadline - time.monotonic()
    python = runtime_python()
    if python is None or left < ENSURE_MIN_S:
        return False
    marker = service_mark("ensured")
    if _fresh(marker, ENSURE_EVERY_S) or not _touch(marker):
        return False
    import subprocess  # noqa: PLC0415

    wait = min(ENSURE_WAIT_S, max(left - 0.3, 0.0))
    env = {k: v for k, v in os.environ.items() if k not in OWNED_ENV}
    try:
        done = subprocess.run(  # noqa: S603 -- the CLI's own interpreter and client
            [python, "-I", "-m", "probe.service.client", "ensure", "--wait", "%.2f" % wait],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=env, cwd="/", timeout=max(deadline - time.monotonic(), 0.01),
        )
        return json.loads(done.stdout.decode("utf-8", "replace") or "{}").get("ensured") == "running"
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError):
        return False


def _queue_folder(sent: bool) -> "str | None":
    """The service's queue folder, when an event written there will run: it is there and private (a
    service that drains it made it), a CLI is installed here, and, unless the event was `sent` (a
    running service saw it), its `drained-by` stamp is the CLI version the next service starts as."""
    if not cli_here():
        return None
    paths = _sibling("_service_paths")
    folder = str(paths.hook_queue_dir())
    if not _private(folder):
        return None
    if sent:
        return folder
    try:
        with open(os.path.join(folder, paths.HOOK_QUEUE_DRAINER), encoding="utf-8") as handle:
            drainer = handle.read(256).strip()
    except OSError:
        return None
    return folder if drainer and drainer == _runtime().get("cli_version") else None


def spool(event_id: str, root: str, lane: str, payload: bytes, env: dict, cwd: str, *, sent: bool) -> bool:
    """The event as a `hooks.queue` item in the service's queue folder (`_queue_folder`), under the
    id it was sent with, for the next service to run its record-keeping routes: written whole (a
    private temporary file renamed into place), while fewer than MAX_QUEUED wait."""
    item = {
        "id": event_id, "plugin_root": os.path.realpath(root), "lane": lane,
        # surrogateescape: bytes that are not UTF-8 come back as they were.
        "payload": payload.decode("utf-8", "surrogateescape"), "env": queue_env(env), "cwd": cwd,
        "at": time.time(), "sent": sent,
    }
    data = json.dumps(item).encode("utf-8", "surrogateescape")
    if len(data) > MAX_QUEUED_BYTES:
        return False
    folder = _queue_folder(sent)
    if folder is None:
        return False
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    if event_id + ".json" in names or event_id + ".done" in names:
        return True  # there already (the service kept it, `hooks.run` could not start it): one id runs once
    if sum(1 for name in names if name.endswith(".json") and not name.startswith(".")) >= MAX_QUEUED:
        return False
    temporary = os.path.join(folder, ".%s.%d.tmp" % (event_id, os.getpid()))
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
        finally:
            os.close(fd)
        os.replace(temporary, os.path.join(folder, event_id + ".json"))
        return True
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False


def _private(folder: str) -> bool:
    import stat  # noqa: PLC0415

    try:
        st = os.lstat(folder)
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.getuid() and not st.st_mode & 0o022


def launch_lifeboat() -> bool:
    """Run the lifeboat's wrapper (`/bin/sh <it> session`), detached, at most every
    LIFEBOAT_EVERY_S: a service that will not start may be an install it must repair."""
    try:
        folder = os.path.join(os.path.expanduser("~"), *LIFEBOAT_DIR)
        wrapper = os.path.join(folder, LIFEBOAT_WRAPPER)
        stamp = os.path.join(folder, LIFEBOAT_STAMP)
        if not os.path.isfile(wrapper) or not _private(folder) or _fresh(stamp, LIFEBOAT_EVERY_S):
            return False
        with open(stamp, "a"):
            pass
        os.utime(stamp, None)
        import subprocess  # noqa: PLC0415

        env = {k: v for k, v in os.environ.items() if k not in OWNED_ENV}
        subprocess.Popen(  # noqa: S603 -- the lifeboat's own frozen wrapper
            ["/bin/sh", wrapper, "session"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, cwd="/", close_fds=True, start_new_session=True,
        )
        return True
    except Exception:  # noqa: BLE001 -- fail open: the session goes on
        return False


def heartbeat(lane: str, payload: dict) -> None:
    """Prove to the capture plugin that Probe's hooks run in this Codex session (its `PLUGIN_ROOT`):
    `$XDG_STATE_HOME/probe/hooks/alive/<session id>`, touched; a session start prunes old ones."""
    if lane not in HEARTBEAT_LANES or not os.environ.get("PLUGIN_ROOT"):
        return
    sid = "".join(c for c in str(payload.get("session_id") or "") if c.isascii() and (c.isalnum() or c in "_-"))
    if not sid:
        return
    try:
        base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
        folder = os.path.join(base, "probe", "hooks", "alive")
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, sid)
        with open(path, "a", encoding="utf-8"):
            pass
        os.utime(path, None)
        if lane == "session-start":
            cutoff = time.time() - HEARTBEAT_KEEP_S
            for entry in os.scandir(folder):
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    os.unlink(entry.path)
    except OSError:
        pass


def _write_all(fd: int, text) -> None:
    if not isinstance(text, str) or not text:
        return
    data = text.encode("utf-8", "surrogateescape")
    try:
        while data:
            data = data[os.write(fd, data):]
    except OSError:
        pass


def emit(answer: dict) -> int:
    """Print an answer's stdout and stderr; its exit code (0 when it names none: fail open)."""
    sys.stdout.flush()
    _write_all(1, answer.get("stdout"))
    _write_all(2, answer.get("stderr"))
    code = answer.get("exit_code")
    return code if isinstance(code, int) and not isinstance(code, bool) and 0 <= code < 256 else 0


#: What a denied gate says (exit 2, on stderr: the agent reads it for a tool call, the person for a prompt).
DENIED = {
    "too_big": "Probe refused this {what}: it is too large for Probe's guard to check ({size} KiB; the limit is "
               "{limit} KiB). Split it into smaller steps.",
    "not_harness": "Probe refused this {what}: the Probe service found no running agent this hook belongs to "
                   "({why}).",
}
#: What an event that went on without Probe's hooks says (one line on stderr, exit 0).
WENT_ON = {
    INCOMPLETE_FOLDER: "Probe: Probe's plugin folder is incomplete or broken, so its hooks did not run for this "
                       "event; run `probe doctor`.",
    "other": "Probe: the Probe service did not run its hooks for this event ({why}).",
}


def deny(lane: str, key: str, **values) -> int:
    what = "tool call" if lane == "pre-tool" else "prompt"
    _write_all(2, DENIED[key].format(what=what, **values) + "\n")
    return 2


def went_on(refused: Refused) -> int:
    """A refusal that lets the event go on (exit 0): its one line on stderr."""
    _write_all(2, WENT_ON.get(refused.reason, WENT_ON["other"]).format(why=refused.why) + "\n")
    return 0


def forward(root: str, lane: str, options: list, payload: bytes, *, deadline: float, harness: "str | None",
            root_env: "str | None" = None) -> int:
    """One event to the service: its exit status. See the module docstring."""
    try:
        parsed = json.loads(payload.decode("utf-8", "replace") or "{}")
    except ValueError:
        parsed = {}
    parsed = parsed if isinstance(parsed, dict) else {}
    heartbeat(lane, parsed)
    if service_off(os.environ):
        return 0  # never ask, start or spool: Probe's hooks are switched off here
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = "/"
    gate = lane in GATE_LANES
    sent_payload = trimmed(lane, parsed)
    if lane == "pre-tool":
        size = len(json.dumps(sent_payload).encode("utf-8", "surrogateescape"))
        if size > MAX_GATE_PAYLOAD:
            return deny(lane, "too_big", size=size // 1024, limit=MAX_GATE_PAYLOAD // 1024)
    event_id = os.urandom(16).hex()
    env = pick_env(os.environ)
    params = {"plugin_root": os.path.realpath(root), "lane": lane, "options": list(options), "payload": sent_payload,
              "env": env, "cwd": cwd, "event_id": event_id}
    if harness:
        params["harness"] = harness
        if root_env:
            params["root_env"] = root_env
    size = len(json.dumps({"jsonrpc": "2.0", "id": 9, "method": "hooks.run", "params": params}).encode(
        "utf-8", "surrogateescape"))
    deadline -= MARGIN_S
    if size > MAX_LINE:
        answer = Refused("too large to send", "too_big")
    else:
        answer = Reach.DOWN if _fresh(service_mark("down"), DOWN_RETRY_S) else ask(params, deadline)
        if answer == Reach.DOWN:
            _touch(service_mark("down"))
            if ensure(deadline):
                answer = ask(params, deadline)
    if isinstance(answer, dict):
        if answer.get("exit_code") is None and answer.get("timed_out"):
            # Out of time: every lane goes on (a slow service never blocks a prompt); a record-keeping
            # event the service could not start is kept under its id (the service keeps it too).
            if lane not in NEVER_SPOOLED:
                spool(event_id, root, lane, payload, env, cwd, sent=True)
            return 0
        return emit(answer)
    if isinstance(answer, Refused):
        if answer.reason == "too_big":
            if gate:
                return deny(lane, "too_big", size=size // 1024, limit=MAX_LINE // 1024)
            if lane not in NEVER_SPOOLED:
                spool(event_id, root, lane, payload, env, cwd, sent=False)
            return 0
        if answer.reason in DENY_REASONS:
            # A forgery's (no hook of a running harness): denied as a gate, never recorded later.
            return deny(lane, NOT_HARNESS, why=answer.why) if gate else 0
        if lane not in NEVER_SPOOLED:
            spool(event_id, root, lane, payload, env, cwd, sent=True)
        return went_on(answer)
    if answer == Reach.DOWN and lane == "session-start":
        launch_lifeboat()
    if lane not in NEVER_SPOOLED:
        spool(event_id, root, lane, payload, env, cwd, sent=answer != Reach.DOWN)
    return 0


#: A held wait's slice, at most (`protocol.MAX_HOOK_WAIT_MS`), and the pause after a lost connection.
WAIT_SLICE_S = 60.0
WAIT_RETRY_S = 2.0
#: Lost connections in a row past which the wake gives up (a restart is back well within them).
WAIT_LOST_MAX = 5


def forward_wait(root: str, payload: bytes, *, until: float, harness: "str | None", root_env: "str | None") -> int:
    """Claude Code's Stop wake from the plugin's own `claude-reads.json` (service-only task 13): the
    service holds it as slices (`hooks.run {wait: "reads"}`, `reads_hook.py` from this folder), and
    this asks again while a slice says "nothing yet" or the connection was lost (an update's restart),
    as the frozen shim does, until `until` (monotonic). Its exit status: the wake's (2 with its text
    on stderr), else 0. Nothing waits here but the connection: no route runs outside the service."""
    if service_off(os.environ) or not harness:
        return 0
    try:
        parsed = json.loads(payload.decode("utf-8", "replace") or "{}")
    except ValueError:
        parsed = {}
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = "/"
    params = {"plugin_root": os.path.realpath(root), "lane": "stop", "payload": parsed if isinstance(parsed, dict) else {},
              "env": pick_env(os.environ), "cwd": cwd, "harness": harness, "wait": "reads"}
    if root_env:
        params["root_env"] = root_env
    lost = 0
    while time.monotonic() < until:
        slice_end = min(until, time.monotonic() + WAIT_SLICE_S)
        answer = ask(params, slice_end)
        if answer == Reach.DOWN:
            ensure(slice_end)
        if answer in (Reach.DOWN, Reach.NO_ANSWER):
            lost += 1
            if lost > WAIT_LOST_MAX:
                return 0  # no service came back: the message waits for the next prompt
            time.sleep(max(min(WAIT_RETRY_S, until - time.monotonic()), 0.0))
            continue
        lost = 0
        if not isinstance(answer, dict):
            return 0  # refused, or an older service that holds no wait: nothing to wait for here
        if answer.get("exit_code") is None and answer.get("timed_out"):
            continue  # nothing yet: the next slice
        return emit(answer)
    return 0
