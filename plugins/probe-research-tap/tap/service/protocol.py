"""Probe service API v1: the wire contract, pinned in code.

The prose contract is `probe-service-api-v1.md` (shared with prbe-ai/desktop,
which mirrors this module in `src-tauri/src/service.rs`); this module is its
source of truth once merged. Unix stream socket, one JSON object per line
(UTF-8, `\\n`), JSON-RPC 2.0, first request on a connection `service.hello`.

Stdlib only: the service's start path imports nothing heavier (plan S1, Q3).
"""

from __future__ import annotations

import json
from typing import Any

API_VERSION = 1
JSONRPC = "2.0"
#: One line, either direction. A longer line is refused and the connection closed.
MAX_LINE_BYTES = 1024 * 1024


class ErrorCode:
    INVALID_REQUEST = -32600
    UNKNOWN_METHOD = -32601
    INVALID_PARAMS = -32602
    HELLO_REQUIRED = -32002
    BUSY = -32010
    NOT_FOUND = -32011
    REFUSED = -32012
    UNSUPPORTED_API = -32013


ERROR_MESSAGES = {
    ErrorCode.INVALID_REQUEST: "invalid request",
    ErrorCode.UNKNOWN_METHOD: "unknown method",
    ErrorCode.INVALID_PARAMS: "invalid params",
    ErrorCode.HELLO_REQUIRED: "hello_required",
    ErrorCode.BUSY: "busy",
    ErrorCode.NOT_FOUND: "not_found",
    ErrorCode.REFUSED: "refused",
    ErrorCode.UNSUPPORTED_API: "unsupported_api",
}


class Method:
    HELLO = "service.hello"
    STATUS = "service.status"
    STOP = "service.stop"
    SESSION_REGISTER = "session.register"
    SESSION_END = "session.end"
    SESSION_LIST = "session.list"
    EVENTS_SUBSCRIBE = "events.subscribe"
    STATUS_GET = "status.get"
    ACTION_RUN = "action.run"
    ACTION_STATUS = "action.status"
    ACTION_EVENTS = "action.events"
    ACTION_CANCEL = "action.cancel"
    UPDATE_STATUS = "update.status"
    UPDATE_CHECK_NOW = "update.check_now"


#: The methods this service answers. Every other name is `-32601`.
METHODS = frozenset(
    {
        Method.HELLO,
        Method.STATUS,
        Method.STOP,
        Method.SESSION_REGISTER,
        Method.SESSION_END,
        Method.SESSION_LIST,
        Method.EVENTS_SUBSCRIBE,
        Method.STATUS_GET,
        Method.ACTION_RUN,
        Method.ACTION_STATUS,
        Method.ACTION_EVENTS,
        Method.ACTION_CANCEL,
        Method.UPDATE_STATUS,
        Method.UPDATE_CHECK_NOW,
    }
)


class Action:
    """`action.run`'s `action`: the `python -m probe.core.run` actions that
    change the device (`probe.core.requests.ActionName`, its reads excepted).
    Every one of them is mutating, so they run one at a time."""

    CONFIGURE = "configure"
    RECORDER = "recorder"
    LOGOUT = "logout"
    UNINSTALL = "uninstall"
    UPDATE = "update"
    SESSION_DEFAULT = "session_default"


ACTIONS = frozenset(
    {Action.CONFIGURE, Action.RECORDER, Action.LOGOUT, Action.UNINSTALL, Action.UPDATE, Action.SESSION_DEFAULT}
)


class ActionState:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"


class CheckTrigger:
    """What asked for an update cycle (`update.check_now`'s `trigger`, and what
    the updater records)."""

    APP = "app"  # the desktop app (its launch check)
    CLI = "cli"  # the CLI on its own: the every-command check, a run's end
    SESSION = "session"  # a coding agent's session start (the plugin hook, pi)
    SCHEDULE = "schedule"  # the service's own 6-hourly check


CHECK_TRIGGERS = frozenset({CheckTrigger.APP, CheckTrigger.CLI, CheckTrigger.SESSION, CheckTrigger.SCHEDULE})


class ReportTrigger:
    """The server's update-report words (`app/client_version/
    update_report_router.py` `UpdateTrigger`): what an update child reports
    under (`PROBE_AUTOUPDATE_TRIGGER`), so the report lands under a trigger the
    server knows. `action.run`'s `trigger` takes these."""

    SESSION_START = "session_start"
    CLI = "cli"
    MANUAL = "manual"


REPORT_TRIGGERS = frozenset({ReportTrigger.SESSION_START, ReportTrigger.CLI, ReportTrigger.MANUAL})

#: A cycle's trigger as the server's report names it, until the server learns
#: `app` and `schedule`: both are automatic and tied to no agent session.
REPORT_FOR_CHECK = {
    CheckTrigger.APP: ReportTrigger.CLI,
    CheckTrigger.CLI: ReportTrigger.CLI,
    CheckTrigger.SESSION: ReportTrigger.SESSION_START,
    CheckTrigger.SCHEDULE: ReportTrigger.CLI,
}


class CheckOutcome:
    """`update.check_now`'s `outcome`."""

    STARTED = "started"  # a cycle starts now
    SCHEDULED = "scheduled"  # the service is discovering: the cycle starts when that ends
    RUNNING = "running"  # a cycle is already running
    RECENT = "recent"  # a cycle finished less than the cooldown ago
    DEFERRED = "deferred"  # not now: `reason` says why (DeferReason)


class DeferReason:
    """Why an update cycle did not apply anything (`update.deferred`)."""

    AUTO_UPDATE_OFF = "auto_update_off"
    #: A Probe daemon worker holds its session lock (`<state>/daemon/<sid>.lock`).
    DAEMON_RECORDING = "daemon_recording"
    #: A training run holds a run lock (`<state>/runs/`).
    RUN_IN_FLIGHT = "run_in_flight"
    #: A terminal `probe wizard` (or the update child's own lock) holds `wizard.lock`.
    SETUP_RUNNING = "setup_running"
    #: An `action.run` of this service is running.
    ACTION_RUNNING = "action_running"


class UpdateStep:
    """An update cycle's steps, in order (plan D3, X1, R2)."""

    #: `python -m probe.core.run update`: the CLI's uv tool env (CLI, daemon,
    #: SDK) and every coding agent's plugins and pi's package, in one run.
    CLI_AND_PLUGINS = "cli_and_plugins"
    #: The desktop app, through its own signed updater (`--background-update`).
    APP = "app"
    #: The service's own code: a new CLI version means a restart.
    SELF = "self"


class StepState:
    STARTED = "started"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    DEFERRED = "deferred"


class StopReason:
    """`service.stopping`'s `reason`."""

    IDLE = "idle"
    STOP = "stop"
    SIGNAL = "signal"
    #: An update cycle installed a new CLI: the next `ensure` starts its code.
    UPDATE = "update"


#: What a request may carry as its `context` (plan X2): the environment the
#: action's (or `status.get`'s) child runs with, on top of the service's own
#: clean one. Where Probe and the agents keep their files, and how the CLI,
#: uv and what they run reach the network -- the desktop app's IMPORTED_ENV
#: (prbe-ai/desktop `src-tauri/src/wizard.rs`), the variables a Finder-started
#: app imports from the login shell. Never a credential by name: no token, no
#: credentials, no PYTHONPATH. `PROBE_BASE_URL` and `base_url` both select only
#: the endpoint, matching Finder's fixed imported environment. A name outside this set is
#: refused, never dropped. The service also allows each coding agent's
#: session-id variable, read from the harness registry (`session_env`; not
#: written here: this file is the tap's too, and names no harness): a request
#: from inside an agent's session names it, so `probe.core.run` refuses what it
#: refuses there (`probe session default`, the wizard actions in a
#: daemon-recorded session). An id, never a credential.
#: `tests/test_service_actions.py` keeps both covering every harness.
CONTEXT_ENV = frozenset(
    {
        "PROBE_CONFIG_PATH",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "PI_CODING_AGENT_DIR",
        "KIMI_CODE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "UV_TOOL_DIR",
        "UV_TOOL_BIN_DIR",
        "UV_CACHE_DIR",
        "PIPX_HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
        "UV_DEFAULT_INDEX",
        "UV_INDEX",
        "UV_NATIVE_TLS",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "NPM_CONFIG_REGISTRY",
        "PROBE_SESSION_STATE",
        "PROBE_SESSION_TRACKING",
        "PROBE_TELEMETRY",
        "PROBE_BASE_URL",
    }
)
#: `base_url` reaches the child as this variable (`probe.sdk.config.resolve`).
BASE_URL_ENV = "PROBE_BASE_URL"
MAX_CONTEXT_VALUE = 4096


class Client:
    APP = "app"
    TAP = "tap"
    PI = "pi"  # harness-literal-ok: an API v1 client kind (the contract), not a harness lookup
    CLI = "cli"


CLIENTS = frozenset({Client.APP, Client.TAP, Client.PI, Client.CLI})


class Mode:
    DAEMON = "daemon"
    INLINE = "inline"
    OFF = "off"


MODES = frozenset({Mode.DAEMON, Mode.INLINE, Mode.OFF})


class State:
    DISCOVERING = "discovering"
    RUNNING = "running"
    GRACE = "grace"


class Topic:
    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"
    ACTION_EVENT = "action.event"
    ACTION_DONE = "action.done"
    UPDATE_STARTED = "update.started"
    UPDATE_STEP = "update.step"
    UPDATE_DONE = "update.done"
    UPDATE_DEFERRED = "update.deferred"
    SERVICE_STOPPING = "service.stopping"


#: Every topic API v1 names. A subscription may name any of them.
TOPICS = frozenset(
    {
        Topic.SESSION_STARTED,
        Topic.SESSION_ENDED,
        Topic.ACTION_EVENT,
        Topic.ACTION_DONE,
        Topic.UPDATE_STARTED,
        Topic.UPDATE_STEP,
        Topic.UPDATE_DONE,
        Topic.UPDATE_DEFERRED,
        Topic.SERVICE_STOPPING,
    }
)


class EndReason:
    """Why a session left the registry (`session.ended` `reason`)."""

    END = "end"  # session.end from its registrant
    OWNER_EXIT = "owner_exit"  # its owner process exited
    EXPIRED = "expired"  # a heartbeat-only owner went quiet past the TTL


class RefusedReason:
    STALE_GENERATION = "stale_generation"
    STATUS_UNAVAILABLE = "status_unavailable"
    #: A bug in the service answering one request; the service keeps serving.
    INTERNAL = "internal"
    #: `action.cancel` of an action that is not waiting for a browser approval:
    #: it may be writing, and stopping it there could leave a half-written file.
    WRITING = "writing"
    #: `action.cancel` of an action that already finished.
    FINISHED = "finished"
    #: `service.stop` while an update step or an action runs.
    UPDATING = "updating"
    ACTION_RUNNING = "action_running"


class BusyHolder:
    """`busy`'s `data.holder.kind`: what holds the one mutating slot."""

    ACTION = "action"
    UPDATE = "update"


class RpcError(Exception):
    def __init__(self, code: int, message: str | None = None, data: dict | None = None) -> None:
        super().__init__(message or ERROR_MESSAGES.get(code, "error"))
        self.code = code
        self.message = message or ERROR_MESSAGES.get(code, "error")
        self.data = data


def invalid_params(detail: str) -> RpcError:
    return RpcError(ErrorCode.INVALID_PARAMS, f"invalid params: {detail}")


def encode(message: dict) -> bytes:
    """One message as one wire line."""
    return (json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def request(id_: Any, method: str, params: dict | None = None) -> dict:
    return {"jsonrpc": JSONRPC, "id": id_, "method": method, "params": params or {}}


def result(id_: Any, value: Any) -> dict:
    return {"jsonrpc": JSONRPC, "id": id_, "result": value}


def error(id_: Any, exc: RpcError) -> dict:
    body: dict[str, Any] = {"code": exc.code, "message": exc.message}
    if exc.data is not None:
        body["data"] = exc.data
    return {"jsonrpc": JSONRPC, "id": id_, "error": body}


def event(topic: str, payload: dict) -> dict:
    return {"jsonrpc": JSONRPC, "method": "event", "params": {"topic": topic, **payload}}


def parse_request(line: bytes) -> tuple[Any, str, dict]:
    """`(id, method, params)` from one line, or RpcError(-32600) with `data.id`.

    A line that is not a JSON object, has no `id`, or names no method is an
    invalid request: every v1 method answers, so a notification from a client
    is a client bug, and saying so beats silence.
    """
    try:
        message = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise RpcError(ErrorCode.INVALID_REQUEST, "invalid request: not a JSON object") from None
    if not isinstance(message, dict):
        raise RpcError(ErrorCode.INVALID_REQUEST, "invalid request: not a JSON object")
    id_ = message.get("id")
    if message.get("jsonrpc") != JSONRPC or not isinstance(id_, (int, str)) or isinstance(id_, bool):
        raise RpcError(ErrorCode.INVALID_REQUEST, "invalid request: needs jsonrpc 2.0 and an id", {"id": id_})
    method = message.get("method")
    if not isinstance(method, str) or not method:
        raise RpcError(ErrorCode.INVALID_REQUEST, "invalid request: no method", {"id": id_})
    params = message.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise RpcError(ErrorCode.INVALID_PARAMS, "invalid params: params must be an object", {"id": id_})
    return id_, method, params


# -- param checks -------------------------------------------------------------


def need_str(params: dict, key: str, *, max_len: int = 256, optional: bool = False) -> str | None:
    value = params.get(key)
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or len(value) > max_len:
        raise invalid_params(f"{key} must be a non-empty string of at most {max_len} characters")
    return value


def need_int(params: dict, key: str, *, minimum: int = 0) -> int:
    value = params.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise invalid_params(f"{key} must be an integer >= {minimum}")
    return value


def need_bool(params: dict, key: str, *, default: bool = False) -> bool:
    value = params.get(key, default)
    if not isinstance(value, bool):
        raise invalid_params(f"{key} must be a boolean")
    return value


def need_choice(params: dict, key: str, choices: frozenset) -> str:
    value = params.get(key)
    if value not in choices:
        raise invalid_params(f"{key} must be one of {sorted(choices)}")
    return value


def need_context(params: dict, also: frozenset = frozenset()) -> dict:
    """The request's `context` (CONTEXT_ENV names, and `also`'s, to strings)
    and `base_url`, as the environment variables a child gets. `{}` when
    neither is given."""
    env: dict = {}
    allowed = CONTEXT_ENV | also
    context = params.get("context")
    if context is not None:
        if not isinstance(context, dict):
            raise invalid_params("context must be an object of environment variables")
        unknown = sorted(name for name in context if name not in allowed)
        if unknown:
            raise invalid_params(f"context may not set {unknown}; it takes {sorted(allowed)}")
        for name, value in context.items():
            if not isinstance(value, str) or len(value) > MAX_CONTEXT_VALUE or "\0" in value:
                raise invalid_params(f"context.{name} must be a string of at most {MAX_CONTEXT_VALUE} characters")
            env[name] = value
    base_url = params.get("base_url")
    if base_url is not None:
        if (
            not isinstance(base_url, str)
            or len(base_url) > 2048
            or not base_url.startswith(("https://", "http://"))
            or any(ch.isspace() for ch in base_url)
        ):
            raise invalid_params("base_url must be an http(s) URL")
        env[BASE_URL_ENV] = base_url
    return env


def need_owner(params: dict) -> dict | None:
    """`{pid, start_time}` or null (a heartbeat-only owner)."""
    if "owner" not in params:
        raise invalid_params("owner is required ({pid, start_time} or null)")
    owner = params["owner"]
    if owner is None:
        return None
    if not isinstance(owner, dict):
        raise invalid_params("owner must be {pid, start_time} or null")
    pid = owner.get("pid")
    start = owner.get("start_time")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
        raise invalid_params("owner.pid must be an integer > 1")
    if not isinstance(start, str) or not start or len(start) > 64:
        raise invalid_params("owner.start_time must be a non-empty string")
    return {"pid": pid, "start_time": start}
