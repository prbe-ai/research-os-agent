"""the closed vocabularies requests and events carry: sessions, topics, mailbox, jobs, desktop ops, update steps and app results."""

from __future__ import annotations

from .wire import invalid_params


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
    #: Every workspace on the team (`{id, name, slug, kind}`), fetched WITHOUT this
    #: computer's workspace limit, and that limit: the Settings picker's list.
    WORKSPACES = "workspaces"
    COMPUTER_WORKSPACE = "computer_workspace"
    SET_COMPUTER_WORKSPACE = "set_computer_workspace"
    CREATE_COMPUTER_WORKSPACE = "create_computer_workspace"
    SETUP_OPTIONS = "setup_options"
    SET_SETUP_OPTION = "set_setup_option"


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
        DesktopOp.WORKSPACES,
        DesktopOp.COMPUTER_WORKSPACE,
        DesktopOp.SETUP_OPTIONS,
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
    "offline",
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
    #: `{"workspace": <id|slug|name> | null, "team_items": "show"|"hide"}`: limit what
    #: Probe on this computer reads to one workspace (null: the whole team). Its
    #: `result` row's `actual` is `probe workspace limit --json`'s object.
    WORKSPACE_LIMIT = "workspace_limit"


ACTIONS = frozenset(
    {
        Action.CONFIGURE,
        Action.RECORDER,
        Action.LOGOUT,
        Action.UNINSTALL,
        Action.UPDATE,
        Action.SESSION_DEFAULT,
        Action.WORKSPACE_LIMIT,
    }
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
    #: The lifeboat, from the keeper unit, when no check ran for 6 h: a machine
    #: with no session (and so no service) is still checked (plan fool-proof-updates B2).
    KEEPER = "keeper"


CHECK_TRIGGERS = frozenset(
    {CheckTrigger.APP, CheckTrigger.CLI, CheckTrigger.SESSION, CheckTrigger.SCHEDULE, CheckTrigger.KEEPER}
)


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
#: `app`, `schedule` and `keeper`: all automatic and tied to no agent session.
REPORT_FOR_CHECK = {
    CheckTrigger.APP: ReportTrigger.CLI,
    CheckTrigger.CLI: ReportTrigger.CLI,
    CheckTrigger.SESSION: ReportTrigger.SESSION_START,
    CheckTrigger.SCHEDULE: ReportTrigger.CLI,
    CheckTrigger.KEEPER: ReportTrigger.CLI,
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
    as the service's own supervisor sees it (`probe.service.sessions.workers`)."""

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
