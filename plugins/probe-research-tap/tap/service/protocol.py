"""Probe service API v1: the wire contract, pinned in code.

The prose contract is `probe-service-api-v1.md` (shared with prbe-ai/desktop,
which mirrors this module in `src-tauri/src/service.rs`); this module is its
source of truth once merged. Unix stream socket, one JSON object per line
(UTF-8, `\\n`), JSON-RPC 2.0, first request on a connection `service.hello`.

Stdlib only: the service's start path imports nothing heavier (plan S1, Q3).
"""

from __future__ import annotations

import os

import json
import re
import time
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
    #: (`probe.daemon.mailbox`), written with the same atomic operations, so a
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


class DesktopOp:
    """`desktop.request`'s `request.op`: the ops of `probe desktop request
    --json` (`probe.cli.desktop.FIELDS`; tests/test_service_desktop_request.py
    holds the two equal)."""

    JOBS = "jobs"
    JOB = "job"
    DISCOVER_SESSIONS = "discover_sessions"
    PREVIEW_BACKFILL = "preview_backfill"
    START_BACKFILL = "start_backfill"
    RESUME_JOB = "resume_job"
    CANCEL_JOB = "cancel_job"
    DELIVERY = "delivery"
    PROFILE = "profile"
    ANALYTICS_POLICY = "analytics_policy"
    SET_CAPTURE = "set_capture"
    ONBOARDING_COMPLETE = "onboarding_complete"


#: The ops that change nothing a stopped child could leave half-written (a
#: review's preview file is written atomically), as the app's own `mutating()`
#: says. Any other op, one added later included, is a change: never killed at
#: its deadline.
DESKTOP_READS = frozenset(
    {
        DesktopOp.JOBS,
        DesktopOp.JOB,
        DesktopOp.DISCOVER_SESSIONS,
        DesktopOp.PREVIEW_BACKFILL,
        DesktopOp.DELIVERY,
        DesktopOp.PROFILE,
        DesktopOp.ANALYTICS_POLICY,
    }
)
#: `action.run`'s and `desktop.request`'s `origin`: a person asked, in the app.
ORIGIN_DESKTOP = "desktop"

#: `session.list`'s `sessions[].receipts` (plan D29; `receipts.py`): what the
#: server has taken of the session's transcript and what waits, and `scope`,
#: whose (the journal's account: RECEIPT_SCOPE_FIELDS). A reader overlays a
#: receipt only where its scope is the account it shows.
RECEIPT_FIELDS = (
    "last_line",
    "received_bytes",
    "received_at",
    "pending_bytes",
    "pending_lines",
    "pending_batch",
    "finalized",
    "retrying",
    "scope",
)
#: `scope`: the journal's backend (its base URL's host, `:port` when named)
#: and customer id.
RECEIPT_SCOPE_FIELDS = ("backend", "customer")
#: `session.list`'s `harnesses[]`: per agent and scope.
HARNESS_RECEIPT_FIELDS = (
    "harness",
    "scope",
    "sessions",
    "alive",
    "received_at",
    "pending_bytes",
    "pending_lines",
    "pending_batches",
)
class Cap:
    """`service.hello`'s `caps`: what this service guarantees beyond its
    methods, for a client that must not rely on it where an older service
    (same method, older code) does not. A service from before `caps` sends none."""

    #: `action.run` refuses a caller in a sandbox, whatever client kind it
    #: names (`refused: sandboxed`). The CLI hands a wizard action only to a
    #: service that says so: an older one would run it outside the sandbox.
    ACTION_SANDBOX = "action.sandbox"
    #: The service decides when the desktop app checks for its update and
    #: never launches the app to do it: `update.check_now` with `app` answers
    #: `app: {due, reason}`, an open app subscribed to `app.check` is sent
    #: one when a cycle wants it, and `update.app_result` records what the
    #: app's own updater did. An older service launched a hidden copy of the
    #: app instead (`--background-update`) and knows none of these.
    UPDATE_APP = "update.app"


CAPS = frozenset({Cap.ACTION_SANDBOX, Cap.UPDATE_APP})


class NotesMode:
    """`notes.sync`'s `mode`: the two `probe notes sync`s the hooks run."""

    #: `probe notes sync --push-only`: a turn's edits out, no pull (Stop).
    PUSH = "push"
    #: `probe notes sync`: fetch, merge, push, and re-render the instruction
    #: files' block (a session's start and end).
    RECONCILE = "reconcile"


NOTES_MODES = frozenset({NotesMode.PUSH, NotesMode.RECONCILE})


class JobOutcome:
    """`notes.sync`'s `outcome`."""

    #: A run starts once the debounce has passed (requests meanwhile join it).
    SCHEDULED = "scheduled"
    #: One runs now: this request joins the one run right after it.
    QUEUED = "queued"


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
    """`update.check_now`'s `outcome`; `manifest.refresh` answers with the
    first three (`started`, `running`, `recent`)."""

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
    #: This system's CLI is not updated by the service (Windows, R3: its files
    #: cannot be replaced or moved while it runs; the CLI updates itself there).
    UNSUPPORTED = "unsupported"


class UpdateStep:
    """An update cycle's steps, in order (plan D3, X1, R2)."""

    #: `python -m probe.core.run update`: the CLI's uv tool env (CLI, daemon,
    #: SDK) and every coding agent's plugins and pi's package, in one run.
    CLI_AND_PLUGINS = "cli_and_plugins"
    #: The desktop app: the service decides when, and the open app's own
    #: signed updater checks and installs (`app.check`, `update.app_result`).
    #: The service never launches the app.
    APP = "app"
    #: The service's own code: a new CLI version means a restart.
    SELF = "self"


class StepState:
    STARTED = "started"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"
    DEFERRED = "deferred"
    #: The `app` step is in the open app's hands: its own updater checks, and
    #: the app reports `update.app_result`. No report within
    #: `APP_RESULT_TIMEOUT_S` reads as failed.
    DELEGATED = "delegated"
    #: The app reported `ready`: a newer app is downloaded and verified and
    #: installs when the app quits. No timeout; one final report (`updated`
    #: or `failed`) closes it, or the app's next launch check does.
    READY = "ready"


#: A delegated app check with no `update.app_result` this long after it was
#: handed over reads as failed, and the next ask is due again. The app's own
#: bound (prbe-ai/desktop `background_update.rs`): a 10-minute check and
#: install, a minute more for an install under way, and a margin.
APP_RESULT_TIMEOUT_S = 12 * 60


class AppCheckReason:
    """`update.check_now`'s `app.reason` and `update.status`'s `app.reason`:
    why the app's update check is due now, or not."""

    #: Due: no app check was ever handed over on this machine.
    NEVER_CHECKED = "never_checked"
    #: Due: the last one was handed over at least CHECK_EVERY_S (6 h) ago.
    INTERVAL_PASSED = "interval_passed"
    #: Due: the last one failed, never reported within APP_RESULT_TIMEOUT_S,
    #: or lost the app connection it was sent to before a report.
    LAST_FAILED = "last_failed"
    #: Due: the app asked with `force` (a person's "Check for updates"),
    #: which passes over the interval and the automatic-updates setting but
    #: never over a check already in the app's hands (`in_progress`).
    FORCED = "forced"
    #: Not due: one was handed over less than CHECK_EVERY_S ago and did not fail.
    CHECKED_RECENTLY = "checked_recently"
    #: Not due: one is in the app's hands now (no result yet, not timed out),
    #: or a downloaded update waits for the app to quit (`ready`).
    IN_PROGRESS = "in_progress"
    #: Not due: automatic updates are off (`autoupdate.json`).
    AUTO_UPDATE_OFF = "auto_update_off"


class AppResult:
    """`update.app_result`'s `result`: what the app's own updater did."""

    UPDATED = "updated"  # a newer app was installed (it runs after a restart)
    CURRENT = "current"  # the installed app is the newest
    FAILED = "failed"  # the check or the install failed (`error` says why)
    #: A newer app (`version_after`) is downloaded and verified and installs
    #: when the app quits. Not the check's end: the same check then takes one
    #: final `updated` or `failed` (same `cycle_id`), and no second check is
    #: handed over while it waits.
    READY = "ready"


APP_RESULTS = frozenset({AppResult.UPDATED, AppResult.CURRENT, AppResult.FAILED, AppResult.READY})


class AppReportIgnored:
    """`update.app_result`'s `reason` when it answers `recorded: false`: the
    report changed nothing (the app need not send it again)."""

    #: Its `cycle_id` names an earlier check than the one in the app's hands
    #: now: it never settles (or ends) the current one.
    STALE = "stale"
    #: The check it answers already has the app's report (after `ready`:
    #: anything but one final `updated` or `failed`).
    DUPLICATE = "duplicate"


class AppStepOutcome:
    """A cycle's `app` step `outcome` (`update.step`, `update.done`'s `steps.app`)
    while it is delegated or skipped. Once the app reports (or its delegation
    times out) the step is `done` or `failed` and its outcome is the AppResult
    word: `updated`, `current` or `failed`."""

    #: Delegated: `app.check` was sent to the open app.
    APP_CHECK_SENT = "app_check_sent"
    #: Delegated: the app already has this check (its launch `update.check_now`
    #: handed it over, or an earlier `app.check` that has not reported yet).
    ALREADY_DELEGATED = "already_delegated"
    #: Skipped: no Probe app is connected. Nothing is launched; the app's
    #: next launch is due.
    APP_CLOSED = "app_closed"
    #: Skipped: no Probe app is connected and none is installed.
    NOT_INSTALLED = "not_installed"
    #: Skipped: a Probe app is connected but did not subscribe to `app.check`
    #: (an older build, which checks for its own update at its launch).
    APP_NOT_LISTENING = "app_not_listening"
    #: Skipped: the app checked less than CHECK_EVERY_S ago.
    CHECKED_RECENTLY = "checked_recently"
    #: Skipped: automatic updates were turned off while the cycle ran.
    AUTO_UPDATE_OFF = "auto_update_off"
    #: Skipped: a downloaded update (`ready`) waits for the app to quit.
    READY_WAITING = "ready_waiting"


class StopReason:
    """`service.stopping`'s `reason`."""

    IDLE = "idle"
    STOP = "stop"
    SIGNAL = "signal"
    #: An update cycle installed a new CLI: the next `ensure` starts its code.
    UPDATE = "update"
    #: A session asked to be recorded by a newer tap than the one loaded (plan A2).
    TAP_UPDATE = "tap_update"


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
#: The credentials a process may hold in its environment (`probe.sdk.config`'s
#: env table), which outrank the CLI config's stored ones and which a request's
#: `context` never carries: a caller holding one does the work itself, since a
#: child of the service would act with another credential (or none).
#: `hooks/_service_call.py` keeps a copy (tests/test_service_cli_surfaces.py).
CREDENTIAL_ENV = frozenset(
    {"PROBE_TOKEN", "PROBE_MCP_TOKEN", "PROBE_SERVICE_TOKEN", "PROBE_INGEST_TOKEN", "PROBE_HMAC_SECRET"}
)
#: `base_url` reaches the child as this variable (`probe.sdk.config.resolve`).
BASE_URL_ENV = "PROBE_BASE_URL"
MAX_CONTEXT_VALUE = 4096

#: `hooks.run`'s `lane`s and the hook event each one serves: `hooks/dispatch.py`
#: LANE_EVENT, the argument each frozen hooks.json entry passes its dispatcher, and
#: the lanes only `harness_io.py --event` runs (Kimi Code's TurnStarted); the
#: two are held equal (tests/test_service_hooks_run.py).
HOOK_LANES = {
    "session-start": "SessionStart",
    "prompt": "UserPromptSubmit",
    "pre-compact": "PreCompact",
    "pre-tool": "PreToolUse",
    "post-tool": "PostToolUse",
    "session-end": "SessionEnd",
    "stop": "Stop",
    "turn-started": "TurnStarted",
}
#: `hooks.run`'s `timeout_ms` when the caller names none: plan D28's wait, the
#: one a dispatcher gives a warm host before it runs the event itself.
HOOK_WAIT_MS = 300
MAX_HOOK_WAIT_MS = 60_000
#: `hooks.run`'s `env` (the session's environment, as a harness hands its
#: hooks theirs): at most this many variables, each value at most this long,
#: as a recorder's (`recorders.MAX_ENV`).
MAX_HOOK_ENV = 200
MAX_HOOK_ENV_VALUE = 8192
MAX_HOOK_PATH = 4096
#: `hooks.queue`: one queued event, as JSON, is at most this big (its payload
#: and environment included: `hooks/dispatch.py` MAX_QUEUED_BYTES), and its id
#: is the dispatcher's: 32 lowercase hex digits.
MAX_HOOK_QUEUE_ITEM = 256 * 1024
MAX_HOOK_QUEUE_ENV = 1024
HOOK_EVENT_ID_LEN = 32
#: `hooks.queue`'s `env` keeps only what a record-keeping route reads (the rest
#: is dropped before it is stored: the queue is a file on disk, and an agent's
#: environment carries every credential its session was given): these names,
#: the harness's, Probe's, the XDG folders' and the locale's by prefix, and
#: never a name that looks like a secret (HOOK_QUEUE_ENV_SECRET) but Probe's own
#: API key, which the CLI already keeps on disk (0600) and `probe notes sync`
#: may need. `hooks/dispatch.py` `queue_env` keeps the same (a test holds them).
HOOK_QUEUE_ENV_NAMES = frozenset(
    {
        "HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TERM",
        "PLUGIN_ROOT", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR", "PIPX_HOME",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
    }
)
HOOK_QUEUE_ENV_PREFIXES = ("PROBE_", "CLAUDE", "CODEX_", "KIMI_", "PI_", "XDG_", "LC_")
HOOK_QUEUE_ENV_SECRET = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "COOKIE", "AUTH")
HOOK_QUEUE_ENV_CREDENTIAL = "PROBE_TOKEN"


def proxy_userinfo(name: str, value: str) -> bool:
    """Is this a proxy variable whose URL carries a user (and a password):
    `http://user:secret@proxy:8080`, `user@proxy`? A credential, never stored."""
    if not name.upper().endswith("_PROXY"):
        return False
    return "@" in value.split("://", 1)[-1].split("/", 1)[0]


def hook_queue_env(env: dict) -> dict:
    """The part of a queued event's environment that is kept (HOOK_QUEUE_ENV_*,
    never a proxy URL with credentials in it)."""
    kept = {}
    for name, value in env.items():
        if proxy_userinfo(name, value):
            continue
        if name != HOOK_QUEUE_ENV_CREDENTIAL:
            upper = name.upper()
            if any(word in upper for word in HOOK_QUEUE_ENV_SECRET):
                continue
            if name not in HOOK_QUEUE_ENV_NAMES and not name.startswith(HOOK_QUEUE_ENV_PREFIXES):
                continue
        kept[name] = value
    return kept

#: `outbox.drain` / `box.watch`'s `env`: the caller's environment as far as
#: the SDK's drainer and box watcher read it (its PROBE_* settings and knobs,
#: where config and state live, the interpreter's own variables, the network's
#: proxy and certificates), so the child runs as the detached one the caller
#: would have forked. Never a credential (`secret_env`): the drainer delivers
#: with the stored login, as the outbox's ops already pin it, and a caller
#: holding a credential in its environment keeps forking its own drainer.
RUNTIME_ENV_NAMES = frozenset(
    {
        "PATH",
        "LANG",
        "TZ",
        "TMPDIR",
        "VIRTUAL_ENV",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
    }
)
RUNTIME_ENV_PREFIXES = ("PROBE_", "XDG_", "PYTHON", "LC_", "CONDA_", "SSL_", "LD_", "DYLD_")
#: A PROBE_* name with one of these in it is a credential (PROBE_TOKEN,
#: PROBE_INGEST_TOKEN, PROBE_HMAC_SECRET, ...): never sent, never accepted.
SECRET_ENV_WORDS = ("TOKEN", "SECRET", "PASSWORD", "KEY")
#: `telemetry.send`'s `entries`: at most this many (the SDK sender's own queue).
MAX_TELEMETRY_ENTRIES = 256


def secret_env(name: str) -> bool:
    """A Probe credential's variable (`PROBE_TOKEN` and the like)."""
    return name.startswith("PROBE_") and any(word in name for word in SECRET_ENV_WORDS)


def runtime_env_name(name: str) -> bool:
    """A variable `outbox.drain` / `box.watch` take in `env`."""
    return (name in RUNTIME_ENV_NAMES or name.startswith(RUNTIME_ENV_PREFIXES)) and not secret_env(name)


#: `session.register`'s optional `report_env` (crash-report plan B2): whether
#: the registrant's own environment allows the Probe service to send the Probe
#: team an automatic diagnostic report about its session. A bool; absent from
#: an older registrant, which then counts as "no".
REPORT_ENV = "report_env"
_REPORTS_OFF = frozenset({"off", "0", "false", "no", "disabled"})


def report_consent(env) -> bool:
    """`report_env` for a registrant whose environment is `env` (a tap, pi
    through the tap): `PROBE_TELEMETRY` or `PROBE_DIAGNOSTICS` off, or a
    `PROBE_BASE_URL` that is not Probe's hosted service (https, `prbe.ai` or a
    subdomain), says no. The rule of `probe._shared.report_policy.allowed(env)`
    with no CLI config, restated for the tap, which cannot import `probe`
    (tests/test_service_watch.py holds the two equal). The service takes the
    bool as said (it holds none of the registrant's variables), and the report
    child, handed `{}` for such a session, checks only the CLI config's
    backend: this is the one place the session's own opt-outs are read."""
    for name in ("PROBE_TELEMETRY", "PROBE_DIAGNOSTICS"):
        if (env.get(name) or "").strip().lower() in _REPORTS_OFF:
            return False
    base_url = env.get(BASE_URL_ENV) or ""
    if not base_url:
        return True
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(base_url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    return parts.scheme == "https" and (host == "prbe.ai" or host.endswith(".prbe.ai"))


class BackgroundState:
    """`outbox.drain` / `box.watch`'s `state` when `accepted`."""

    STARTED = "started"  # a child starts now
    RUNNING = "running"  # this service's child for it runs already


class BackgroundRefusal:
    """`{accepted: false, reason}`: the caller forks its own process, as before."""

    SANDBOXED = "sandboxed"  # the caller is in a sandbox: its work would run outside it
    BUSY = "busy"  # this service runs as many as it takes
    FAILING = "failing"  # a child for it exited at its start lately
    STOPPING = "stopping"  # this service is on its way out
    UNUSABLE = "unusable"  # the folder or interpreter named is not this user's to run
    UNKNOWN_ENDPOINT = "unknown_endpoint"  # PROBE_BASE_URL not an endpoint the config names (D10)


BACKGROUND_REFUSALS = frozenset(
    {
        BackgroundRefusal.SANDBOXED,
        BackgroundRefusal.BUSY,
        BackgroundRefusal.FAILING,
        BackgroundRefusal.STOPPING,
        BackgroundRefusal.UNUSABLE,
        BackgroundRefusal.UNKNOWN_ENDPOINT,
    }
)


class Client:
    APP = "app"
    TAP = "tap"
    PI = "pi"  # harness-literal-ok: an API v1 client kind (the contract), not a harness lookup
    CLI = "cli"
    #: A training process's SDK handing off its background work (plan Q5).
    SDK = "sdk"


CLIENTS = frozenset({Client.APP, Client.TAP, Client.PI, Client.CLI, Client.SDK})


class Mode:
    DAEMON = "daemon"
    INLINE = "inline"
    OFF = "off"


MODES = frozenset({Mode.DAEMON, Mode.INLINE, Mode.OFF})


class State:
    DISCOVERING = "discovering"
    RUNNING = "running"
    GRACE = "grace"


class WorkerState:
    """`service.status` `workers[].state`: a recorded session's daemon worker,
    as the service's own supervisor sees it (`probe.service.workers`)."""

    RUNNING = "running"
    #: Being started, on a thread of its own (the interpreter's stat, the exec).
    STARTING = "starting"
    #: The switch does not want a worker (`off`, `inline`, read-only outside a
    #: daemon-profile session).
    IDLE = "idle"
    #: Another live worker holds the session's lock: asked to hand over, and
    #: none is started until it lets go.
    WAITING_FOR_LOCK = "waiting_for_lock"
    #: Not before `retry_in`: after a crash (doubling), a switch exit, missing
    #: AI libraries, or the respawn cap.
    BACKOFF = "backoff"
    #: The updater holds new worker starts (`barrier.hold_workers`, the barrier).
    HELD_FOR_UPDATE = "held_for_update"
    #: The worker said respawning cannot help (no key, a newer store, no CLI):
    #: none again until the switch moves (`gave_up_in`).
    GAVE_UP = "gave_up"
    #: Its session ended: asked to stop (SIGTERM), finishing its queue.
    DRAINING = "draining"


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
    #: A watched session's reads mailbox changed (plan Q2): `{sid, parts}`,
    #: each part a MailboxPart. Only "look again": the files say what changed.
    MAILBOX_CHANGED = "mailbox.changed"
    #: A held question of a watched session was filed, rewritten, answered or
    #: removed on the approvals board (plan Q2): `{sid, ids}`.
    APPROVALS_CHANGED = "approvals.changed"
    #: An update cycle hands the open desktop app its update check:
    #: `{cycle_id}`. The app runs its own updater (installing on quit, or
    #: offering its "Restart to update" banner, never restarting under the
    #: person) and reports `update.app_result` with that `cycle_id`. Sent to
    #: ONE connection: the newest one whose `service.hello` said `client: app`
    #: and that subscribed to it (`Cap.UPDATE_APP`). That connection owns the
    #: check: closed before a report, the check is failed (due again).
    APP_CHECK = "app.check"


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
        Topic.MAILBOX_CHANGED,
        Topic.APPROVALS_CHANGED,
        Topic.APP_CHECK,
    }
)

#: The topics whose events are about one session: `events.subscribe`'s
#: `sessions` (and `state_dir`) name which ones the service watches for the
#: connection. A subscription to one of them that names no session watches
#: every live registered session.
SESSION_TOPICS = frozenset({Topic.MAILBOX_CHANGED, Topic.APPROVALS_CHANGED})
#: `events.subscribe`'s `sessions` names at most this many.
MAX_SUBSCRIBE_SESSIONS = 64


class MailboxPart:
    """What changed in a session's reads mailbox (`<state>/reads/`), as a
    `mailbox.changed` event names it."""

    MESSAGES = "messages"  # messages/<sid>/: a message arrived, or was claimed out
    CLAIMED = "claimed"  # claimed/<sid>/: a deliverer took one
    ASKS = "asks"  # asks/<sid>/: an ask filed, or taken by the worker
    REQUESTS = "requests"  # requests/<sid>/: a request to the writer, likewise
    TURN = "turn"  # turns/<sid>: a new prompt started a turn
    WAITER = "waiter"  # turns/<sid>.waiter: a newer Stop waiter took over
    STATUS = "status"  # status/<sid>.json: the reader's health


MAILBOX_PARTS = frozenset(
    {
        MailboxPart.MESSAGES,
        MailboxPart.CLAIMED,
        MailboxPart.ASKS,
        MailboxPart.REQUESTS,
        MailboxPart.TURN,
        MailboxPart.WAITER,
        MailboxPart.STATUS,
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
    #: `action.run` from inside a coding agent's session the Probe daemon
    #: records (its caller's process ancestry or environment says so, plan D9).
    DAEMON_SESSION = "daemon_session"
    #: Any method that runs or stores work (the service's `Policy.WORK`) on a
    #: connection whose opener has exited (plan D9).
    CALLER_GONE = "caller_gone"
    #: Any method that runs or stores work, from a caller the service cannot
    #: rule out being in a sandbox: what it asks for would run (or stop)
    #: outside it, with the context it chose.
    SANDBOXED = "sandboxed"
    #: `desktop.request`: no child could start (no CLI interpreter). Nothing ran.
    CLI_UNAVAILABLE = "cli_unavailable"
    #: `desktop.request`: as many requests as it takes at once are running or
    #: waiting, or none got its turn within its deadline. Nothing ran.
    TOO_MANY = "too_many"
    #: `desktop.request`: no answer within the request's deadline. A read was
    #: stopped (`data.killed: true`); a change goes on, and may still finish.
    TIMED_OUT = "timed_out"
    #: `desktop.request`: the child ended without a readable v1 envelope
    #: (`data.exit_code`), or its answer is over one line (`data.too_large`).
    NO_ANSWER = "no_answer"


class BusyHolder:
    """`busy`'s `data.holder.kind`: what holds the one mutating slot."""

    ACTION = "action"
    UPDATE = "update"
    #: A `desktop.request` change whose child still runs (`data.holder.changes`:
    #: how many), also one its caller stopped waiting for.
    DESKTOP = "desktop"


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


def need_wait(params: dict, key: str = "wait", *, maximum: float = MAX_WAIT_S) -> float:
    """An optional number of seconds from 0 to `maximum` (default 0)."""
    value = params.get(key, 0)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= maximum:
        raise invalid_params(f"{key} must be a number of seconds from 0 to {maximum:g}")
    return float(value)


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
            or not base_url.startswith("https://")
            or any(ch.isspace() for ch in base_url)
        ):
            raise invalid_params("base_url must be an https URL")
        env[BASE_URL_ENV] = base_url
    elif BASE_URL_ENV in env and not env[BASE_URL_ENV].startswith("https://"):
        # The same endpoint, named through `context`: the stored token follows
        # it (plan D10), so the same rule.
        raise invalid_params(f"context.{BASE_URL_ENV} must be an https URL")
    return env


#: `session.register`'s `state_dir` is at most this long.
MAX_STATE_DIR = 1024


def need_state_dir(params: dict) -> str | None:
    """`session.register`'s optional `state_dir` (plan D13): the session's own
    state folder, an absolute path. Absent or null: an older registrant."""
    value = params.get("state_dir")
    if value is None:
        return None
    if not isinstance(value, str) or not os.path.isabs(value) or len(value) > MAX_STATE_DIR or "\0" in value:
        raise invalid_params(f"state_dir must be an absolute path of at most {MAX_STATE_DIR} characters")
    return value


def _need_path(params: dict, key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.startswith("/") or len(value) > MAX_HOOK_PATH or "\0" in value:
        raise invalid_params(f"{key} must be an absolute path of at most {MAX_HOOK_PATH} characters")
    return value


def need_path(params: dict, key: str) -> str:
    """An absolute path of at most MAX_HOOK_PATH characters."""
    return _need_path(params, key)


def need_env(params: dict, key: str = "env") -> dict:
    """A process environment the caller hands over (`hooks.run`'s session
    environment, `import.start`'s CLI environment): an object of at most
    MAX_HOOK_ENV strings, each at most MAX_HOOK_ENV_VALUE long; `{}` when left out."""
    env = params.get(key, {})
    if (
        not isinstance(env, dict)
        or len(env) > MAX_HOOK_ENV
        or not all(
            isinstance(k, str)
            and k
            and len(k) <= 256
            and "=" not in k
            and "\0" not in k
            and isinstance(v, str)
            and len(v) <= MAX_HOOK_ENV_VALUE
            and "\0" not in v
            for k, v in env.items()
        )
    ):
        raise invalid_params(
            f"{key} must be an object of at most {MAX_HOOK_ENV} variables, each a string of at most "
            f"{MAX_HOOK_ENV_VALUE} characters"
        )
    return dict(env)


#: A coding agent's session id as Probe keys it (`probe.sdk.session_marker.valid_session_id`).
SID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
#: A harness registry id's characters (`probe.harness`'s `id`s: lower case, digits, `_`).
AGENT_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")


def need_sid(params: dict, key: str = "sid") -> str:
    """A session id the status line and its marker files accept: 8 to 200 of
    `SID_CHARS` (it names a file, so never a path)."""
    value = params.get(key)
    if not isinstance(value, str) or not 8 <= len(value) <= 200 or not set(value) <= SID_CHARS:
        raise invalid_params(f"{key} must be a session id (8 to 200 letters, digits, '.', '_', ':' or '-')")
    return value


def is_agent_id(value: object) -> bool:
    """Is `value` shaped like a harness registry id (lower-case, digits, `_`)?"""
    return (
        isinstance(value, str)
        and 0 < len(value) <= 64
        and value[0].isalpha()
        and set(value) <= AGENT_CHARS
    )


def need_hook_run(params: dict) -> dict:
    """`hooks.run`'s params, checked: `{root, lane, event, payload (bytes),
    env, cwd, timeout_s}`.

        plugin_root  the plugin folder whose routes run (absolute)
        lane         a HOOK_LANES key: the hooks.json entry's argument
        payload      the event as the harness's hook reads it on stdin: a JSON
                     object; its `hook_event_name`, when given, must be the lane's
        env          the session's environment (at most MAX_HOOK_ENV strings)
        cwd          the session's folder (absolute)
        timeout_ms   how long the caller waits (default HOOK_WAIT_MS)
    """
    root = _need_path(params, "plugin_root")
    lane = params.get("lane")
    if lane not in HOOK_LANES:
        raise invalid_params(f"lane must be one of {sorted(HOOK_LANES)}")
    event = HOOK_LANES[lane]
    payload = params.get("payload")
    if not isinstance(payload, dict):
        raise invalid_params("payload must be a JSON object")
    named = payload.get("hook_event_name")
    if named is not None and named != event:
        raise invalid_params(f"payload.hook_event_name must be {event!r} on lane {lane!r}")
    payload = {**payload, "hook_event_name": event}
    env = need_env(params)
    cwd = _need_path(params, "cwd")
    timeout = params.get("timeout_ms", HOOK_WAIT_MS)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= MAX_HOOK_WAIT_MS:
        raise invalid_params(f"timeout_ms must be an integer from 1 to {MAX_HOOK_WAIT_MS}")
    return {
        "root": root,
        "lane": lane,
        "event": event,
        # ASCII, so a lone surrogate a JSON client may send stays an escape.
        "payload": json.dumps(payload, separators=(",", ":")).encode("ascii"),
        "env": dict(env),
        "cwd": cwd,
        "timeout_s": timeout / 1000.0,
    }


def need_hook_queue(params: dict) -> dict:
    """`hooks.queue`'s params (and a queue file's item, the same object),
    checked: `{id, root, lane, payload (bytes), env, cwd, at}`.

        id           the event's id, 32 lowercase hex digits (the dispatcher's
                     own): an id is run once, however often it is sent
        plugin_root  the plugin folder whose record-keeping routes run (absolute)
        lane         a HOOK_LANES key
        payload      the event as the harness wrote it on the hook's stdin: a
                     string (the dispatcher's bytes, `surrogateescape`)
        env          the event's environment: an object of strings, of which only
                     HOOK_QUEUE_ENV_* is kept (no credential but Probe's own)
        cwd          the event's working folder (absolute)
        at           when it happened (epoch seconds; optional): the queue
                     runs events oldest first

    The whole item is at most MAX_HOOK_QUEUE_ITEM bytes as JSON."""
    event_id = params.get("id")
    if (
        not isinstance(event_id, str)
        or len(event_id) != HOOK_EVENT_ID_LEN
        or any(c not in "0123456789abcdef" for c in event_id)
    ):
        raise invalid_params(f"id must be {HOOK_EVENT_ID_LEN} lowercase hex digits")
    root = _need_path(params, "plugin_root")
    lane = params.get("lane")
    if lane not in HOOK_LANES:
        raise invalid_params(f"lane must be one of {sorted(HOOK_LANES)}")
    payload = params.get("payload")
    if not isinstance(payload, str):
        raise invalid_params("payload must be a string: the event's JSON as the harness wrote it")
    env = params.get("env")
    if (
        not isinstance(env, dict)
        or len(env) > MAX_HOOK_QUEUE_ENV
        or not all(
            isinstance(k, str) and k and "=" not in k and "\0" not in k and isinstance(v, str) and "\0" not in v
            for k, v in env.items()
        )
    ):
        raise invalid_params(f"env must be an object of at most {MAX_HOOK_QUEUE_ENV} string variables")
    cwd = _need_path(params, "cwd")
    at = params.get("at")
    if at is None:
        at = time.time()
    elif not isinstance(at, (int, float)) or isinstance(at, bool) or at != at or at in (float("inf"), float("-inf")):
        raise invalid_params("at must be a number (epoch seconds)")
    item = {"id": event_id, "plugin_root": root, "lane": lane, "payload": payload, "env": hook_queue_env(env),
            "cwd": cwd, "at": float(at)}
    if len(json.dumps(item).encode("utf-8")) > MAX_HOOK_QUEUE_ITEM:
        raise invalid_params(f"a queued event must be at most {MAX_HOOK_QUEUE_ITEM} bytes as JSON")
    return item


def need_sessions(params: dict) -> list[str] | None:
    """`events.subscribe`'s optional `sessions`: the session ids whose mailbox
    and held questions to watch (plan Q2). None when it names none."""
    value = params.get("sessions")
    if value is None:
        return None
    if (
        not isinstance(value, list)
        or not value
        or len(value) > MAX_SUBSCRIBE_SESSIONS
        or not all(isinstance(sid, str) and sid and len(sid) <= 128 and "\0" not in sid for sid in value)
    ):
        raise invalid_params(
            f"sessions must be a non-empty list of at most {MAX_SUBSCRIBE_SESSIONS} session ids "
            "(each at most 128 characters)"
        )
    return list(dict.fromkeys(value))


#: `mailbox.*`'s `deadline` when the caller names none, and the furthest one
#: taken: the service never acts past it, and a caller that heard nothing by
#: then (and a moment after) does the step itself.
MAILBOX_WAIT_S = 1.0
MAX_MAILBOX_WAIT_S = 10.0
#: `mailbox.claim`'s `names`, at most.
MAX_MAILBOX_CLAIM = 64
#: A message file's name (`probe.daemon.mailbox._file_name`), and the ids,
#: tokens and deliverer names a `mailbox.*` call may carry.
MESSAGE_FILE = re.compile(r"[0-9]{1,24}-[A-Za-z0-9_]{1,64}\.json")
MAILBOX_ID = re.compile(r"[A-Za-z0-9_]{1,64}")
_DELIVERER = re.compile(r"[A-Za-z0-9 _.:/@#()-]{1,64}")


def _need_mailbox(params: dict, *, sid: bool = True) -> dict:
    """What every `mailbox.*` call carries: the session (`sid`), the state
    folder its files live under (`state_dir`, the caller's own
    `<XDG_STATE_HOME>/probe`; null: the default), and when the service must
    be done: `deadline` (epoch seconds) and `budget` (seconds from its arrival),
    the sooner of the two, each optional (MAILBOX_WAIT_S when neither is
    given), at most MAX_MAILBOX_WAIT_S. The absolute one holds against a
    request that arrives late, the relative one against a clock stepped back.
    `deadline_mono` is that moment on this process's monotonic clock."""
    out: dict = {"state_dir": need_state_dir(params)}
    if sid:
        out["sid"] = need_str(params, "sid", max_len=128)
    left = MAX_MAILBOX_WAIT_S
    given = False
    for key, now in (("deadline", time.time()), ("budget", 0.0)):
        value = params.get(key)
        if value is None:
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool) or value != value:
            raise invalid_params(f"{key} must be a number of seconds")
        left, given = min(left, float(value) - now), True
    if not given:
        left = MAILBOX_WAIT_S
    out["deadline_mono"] = time.monotonic() + max(0.0, left) if left > 0 else time.monotonic() - 1.0
    return out


def need_mailbox_post(params: dict) -> dict:
    """`mailbox.post`: `{sid, state_dir, deadline}` and exactly one of
    `message` (a `probe.daemon.mailbox.Message` as a dict: a reader's message,
    an answer, a writer's reply, a run alert) or `question` (a held question,
    `probe.daemon.approvals.Request.to_json()`). Their fields are checked where
    they are written."""
    out = _need_mailbox(params)
    message, question = params.get("message"), params.get("question")
    if (message is None) == (question is None) or not isinstance(message if question is None else question, dict):
        raise invalid_params("give exactly one of message or question, as an object")
    out["message"], out["question"] = message, question
    return out


def need_mailbox_claim(params: dict) -> dict:
    """`mailbox.claim`: `{sid, state_dir, deadline}`, `names` (the message
    files to take from `messages/<sid>/`, oldest first), `by` (the deliverer,
    as the claim log records it) and `token` (the request's own, logged beside
    each claim so a caller that lost the answer finds what was claimed for it)."""
    out = _need_mailbox(params)
    names = params.get("names")
    if (
        not isinstance(names, list)
        or not names
        or len(names) > MAX_MAILBOX_CLAIM
        or not all(isinstance(n, str) and MESSAGE_FILE.fullmatch(n) for n in names)
    ):
        raise invalid_params(f"names must be a non-empty list of at most {MAX_MAILBOX_CLAIM} message file names")
    by, token = params.get("by"), params.get("token")
    if not isinstance(by, str) or not _DELIVERER.fullmatch(by):
        raise invalid_params("by must name the deliverer (at most 64 plain characters)")
    if not isinstance(token, str) or not MAILBOX_ID.fullmatch(token):
        raise invalid_params("token must be at most 64 letters, digits or underscores")
    out.update(names=list(dict.fromkeys(names)), by=by, token=token)
    return out


def need_mailbox_answer(params: dict) -> dict:
    """`mailbox.answer`: `{state_dir, deadline}`, `id` (the held question's),
    `choice` (the researcher's pick, as the question tool reported it: the yes
    label is a yes, anything else a no) and `channel` (where it was given)."""
    out = _need_mailbox(params, sid=False)
    rid, choice, channel = params.get("id"), params.get("choice"), params.get("channel")
    if not isinstance(rid, str) or not MAILBOX_ID.fullmatch(rid):
        raise invalid_params("id must be a held question's id")
    if not isinstance(choice, str) or len(choice) > 4096:
        raise invalid_params("choice must be a string of at most 4096 characters")
    if not isinstance(channel, str) or not _DELIVERER.fullmatch(channel):
        raise invalid_params("channel must name where the answer was given (at most 64 plain characters)")
    out.update(id=rid, choice=choice, channel=channel)
    return out


#: `update.app_result`'s `error` is kept to this many characters.
MAX_APP_ERROR = 500


def _optional_version(value: object, key: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 64:
        raise invalid_params(f"{key} must be a version string of at most 64 characters, or null")
    return value


def need_app_check(params: dict, trigger: str) -> dict | None:
    """`update.check_now`'s optional `app` (the desktop app's launch check):
    `{version, force}`, or None when the caller sent none (an older app, the
    CLI, a hook): then the answer has no `app` either. Only with trigger `app`."""
    app = params.get("app")
    if app is None:
        return None
    if not isinstance(app, dict):
        raise invalid_params("app must be an object {version, force}")
    if trigger != CheckTrigger.APP:
        raise invalid_params("app is the desktop app's: only with trigger app")
    force = app.get("force", False)
    if not isinstance(force, bool):
        raise invalid_params("app.force must be a boolean")
    return {"version": _optional_version(app.get("version"), "app.version"), "force": force}


def need_app_result(params: dict) -> dict:
    """`update.app_result`: `result` (AppResult), and optional `version_before`,
    `version_after`, `error` (kept to MAX_APP_ERROR characters) and
    `cycle_id`: the `app.check`'s, or the launch check's `app.cycle_id`, which
    names the check it answers. None (an older build sends none): the check in
    the app's hands now."""
    result = params.get("result")
    if result not in APP_RESULTS:
        raise invalid_params(f"result must be one of {sorted(APP_RESULTS)}")
    error = params.get("error")
    if error is not None and not isinstance(error, str):
        raise invalid_params("error must be a string or null")
    cycle_id = params.get("cycle_id")
    if cycle_id is not None and (not isinstance(cycle_id, str) or not cycle_id or len(cycle_id) > 64):
        raise invalid_params("cycle_id must be a string of at most 64 characters, or null")
    return {
        "result": result,
        "version_before": _optional_version(params.get("version_before"), "version_before"),
        "version_after": _optional_version(params.get("version_after"), "version_after"),
        "error": error[:MAX_APP_ERROR] if error else None,
        "cycle_id": cycle_id,
    }


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


def need_background(params: dict, *, directory: bool) -> dict:
    """`outbox.drain`'s params (`directory`) or `box.watch`'s, checked:
    `{dir, python, cwd, env}`.

        dir     the outbox journal folder to drain (absolute; `outbox.drain` only)
        python  the interpreter the child runs on: the caller's own (absolute)
        cwd     the caller's working folder, the child's too (absolute; optional)
        env     the caller's environment, by `runtime_env_name` (at most
                MAX_HOOK_ENV strings); a credential's name is refused.
                `box.watch` needs its registry folder, PROBE_BOX_DIR, in it.
    """
    found = {
        "dir": _need_path(params, "dir") if directory else None,
        "python": _need_path(params, "python"),
        "cwd": _need_path(params, "cwd") if params.get("cwd") is not None else None,
    }
    env = params.get("env", {})
    if not isinstance(env, dict) or len(env) > MAX_HOOK_ENV:
        raise invalid_params(f"env must be an object of at most {MAX_HOOK_ENV} variables")
    for name, value in env.items():
        if not isinstance(name, str) or "\0" in name or "=" in name or not runtime_env_name(name):
            raise invalid_params(f"env may not set {name!r}: never a credential, only what the drainer reads")
        if not isinstance(value, str) or len(value) > MAX_HOOK_ENV_VALUE or "\0" in value:
            raise invalid_params(f"env.{name} must be a string of at most {MAX_HOOK_ENV_VALUE} characters")
    if not directory and not str(env.get("PROBE_BOX_DIR") or "").startswith("/"):
        raise invalid_params("env.PROBE_BOX_DIR must name the box registry folder (an absolute path)")
    found["env"] = dict(env)
    return found


def need_telemetry(params: dict) -> tuple[list, str | None]:
    """`telemetry.send`'s `(entries, id)`. `entries`: PostHog batch entries
    the caller built (its identity resolved, its opt-out and hosted gate
    passed), each an object naming its `event`; at most
    MAX_TELEMETRY_ENTRIES. `id` (optional): the batch's, the same on a resend,
    so the service takes it once and acknowledges it again."""
    batch_id = params.get("id")
    if batch_id is not None and (
        not isinstance(batch_id, str)
        or not 1 <= len(batch_id) <= 64
        or not batch_id.isascii()
        or not batch_id.replace("-", "").isalnum()
    ):
        raise invalid_params("id must be a string of at most 64 letters, digits and dashes")
    entries = params.get("entries")
    if (
        not isinstance(entries, list)
        or not entries
        or len(entries) > MAX_TELEMETRY_ENTRIES
        or not all(isinstance(e, dict) and isinstance(e.get("event"), str) and e["event"] for e in entries)
    ):
        raise invalid_params(
            f"entries must be a list of 1 to {MAX_TELEMETRY_ENTRIES} objects, each naming its event"
        )
    return entries, batch_id
