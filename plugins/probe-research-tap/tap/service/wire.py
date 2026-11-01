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
    #: The desktop app reports the update check the service handed it
    #: (`update.check_now`'s `app`, or an `app.check` event): what its own
    #: signed updater found and did. The service records it as the cycle's
    #: `app` step; it never launches the app itself.
    UPDATE_APP_RESULT = "update.app_result"
    #: A session's dispatcher asks for a warm host for its plugin folder (plan A0).
    HOOKS_HOST = "hooks.host"
    #: A harness with no hooks of its own (pi) has one hook event run for a
    #: plugin folder, and gets the routes' merged output back (plan D38).
    HOOKS_RUN = "hooks.run"
    #: A session's dispatcher that no warm host took an event from hands the
    #: service that event's record-keeping routes, to run once by its id
    #: (plan D36): `hooks.queue`.
    HOOKS_QUEUE = "hooks.queue"
    #: The tap asks the service to record a session itself (plan A2): register
    #: it, and run its recorder on a thread here instead of a `tap watch`.
    SESSION_RECORD = "session.record"
    SESSION_RECORDING = "session.recording"
    SESSION_FINISH = "session.finish"
    #: `probe capture off` for one agent: finish every recorder this service
    #: runs for that harness, as `session.finish` does for one session.
    RECORDERS_FINISH = "recorders.finish"
    #: A `probe exec` names the run it wraps, and itself as its owner: the
    #: updater's quiet check sees a run in flight without a lock-file scan.
    RUN_HOLD = "run.hold"
    RUN_RELEASE = "run.release"
    #: A CLI whose cached release manifest is stale asks the service to
    #: refresh it, instead of spawning a refresher of its own.
    MANIFEST_REFRESH = "manifest.refresh"
    #: The service owns the reads mailbox and the approvals board (plan Q2):
    #: a worker posts a message or a held question, a hook or the app claims
    #: messages and answers a held question. The files stay the store
    #: (`probe.shared.mailbox`), written with the same atomic operations, so a
    #: caller with no service writes and claims them itself beside it.
    MAILBOX_POST = "mailbox.post"
    MAILBOX_CLAIM = "mailbox.claim"
    MAILBOX_ANSWER = "mailbox.answer"
    #: The desktop app's management requests (`probe desktop request --json`'s
    #: request in, its v1 envelope out), answered by a child of the service.
    DESKTOP_REQUEST = "desktop.request"
    #: A training process's SDK hands the service its background work (plan
    #: Q5) instead of forking a detached process of its own: an outbox's
    #: drainer, this box's watcher (both run as the service's children, on the
    #: caller's interpreter), a batch of client telemetry (sent from a thread).
    OUTBOX_DRAIN = "outbox.drain"
    BOX_WATCH = "box.watch"
    TELEMETRY_SEND = "telemetry.send"
    #: A session's status line wants its server read (plan Q4): the service
    #: runs the plugin's own `hooks/statusline_refresh.py` for every session
    #: that asked, one child per plugin folder at most every few seconds,
    #: instead of a detached fetch per tool call.
    STATUSLINE_REFRESH = "statusline.refresh"
    #: The team note's sync (`probe notes sync`, and a session start's `probe
    #: agent-rules refresh`): once per machine, debounced, instead of a
    #: detached `probe` per session per hook event.
    NOTES_SYNC = "notes.sync"
    #: An approved import's worker (`probe.cli.import_jobs`), started and
    #: watched by the service instead of detached from the CLI.
    IMPORT_START = "import.start"


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
        Method.UPDATE_APP_RESULT,
        Method.HOOKS_HOST,
        Method.HOOKS_RUN,
        Method.HOOKS_QUEUE,
        Method.SESSION_RECORD,
        Method.SESSION_RECORDING,
        Method.SESSION_FINISH,
        Method.RECORDERS_FINISH,
        Method.RUN_HOLD,
        Method.RUN_RELEASE,
        Method.MANIFEST_REFRESH,
        Method.MAILBOX_POST,
        Method.MAILBOX_CLAIM,
        Method.MAILBOX_ANSWER,
        Method.DESKTOP_REQUEST,
        Method.OUTBOX_DRAIN,
        Method.BOX_WATCH,
        Method.TELEMETRY_SEND,
        Method.STATUSLINE_REFRESH,
        Method.NOTES_SYNC,
        Method.IMPORT_START,
    }
)

#: `recorders.finish`'s `wait`, and `manifest.refresh`'s, at most (seconds).
MAX_WAIT_S = 60.0


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
