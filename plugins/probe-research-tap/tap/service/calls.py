"""one typed wrapper per service method (outbox.drain, box.watch, session.register, run.hold, hooks.queue, ...)."""

from __future__ import annotations

import time
from collections.abc import Callable

from . import paths, procid
from .env import REPORT_ENV
from .service_api import DESKTOP_TIMEOUT_S, HANDOFF_BUDGET_S, TIMEOUT_S, call, call_within
from .start import ensure
from .vocabularies import Client
from .wire import Method

#: `check_now` waits this long for a service it had to start (never in a hook:
#: the hook and the CLI run it as a detached process).
CHECK_NOW_WAIT_S = 5.0
#: `check-now --wait-for-pid` waits at most this long for the command that
#: asked (`probe.shared.updates.autoupdate.WAIT_FOR_PID_TIMEOUT`).
WAIT_FOR_PID_S = 1800.0


def drain_outbox(
    directory: str,
    python: str,
    env: dict,
    *,
    cwd: str | None = None,
    budget: float = HANDOFF_BUDGET_S,
    deadline: float | None = None,
    on_sent: Callable[[], None] | None = None,
    client_version: str = "unknown",
) -> dict:
    """`outbox.drain`: `{accepted: true, state}` or `{accepted: false, reason}`."""
    params = {"dir": directory, "python": python, "env": env}
    if cwd:
        params["cwd"] = cwd
    return _dict(call_within(Method.OUTBOX_DRAIN, params, budget=budget, deadline=deadline, on_sent=on_sent,
                             client_version=client_version))


def watch_box(
    python: str,
    env: dict,
    *,
    cwd: str | None = None,
    budget: float = HANDOFF_BUDGET_S,
    deadline: float | None = None,
    on_sent: Callable[[], None] | None = None,
    client_version: str = "unknown",
) -> dict:
    """`box.watch`: `{accepted: true, state}` or `{accepted: false, reason}`.
    `env` names the box registry folder (PROBE_BOX_DIR)."""
    params = {"python": python, "env": env}
    if cwd:
        params["cwd"] = cwd
    return _dict(call_within(Method.BOX_WATCH, params, budget=budget, deadline=deadline, on_sent=on_sent,
                             client_version=client_version))


def send_telemetry(
    entries: list,
    *,
    batch_id: str | None = None,
    budget: float = HANDOFF_BUDGET_S,
    deadline: float | None = None,
    on_sent: Callable[[], None] | None = None,
    client_version: str = "unknown",
) -> dict:
    """`telemetry.send`: `{accepted: true}` or `{accepted: false, reason}`.
    `batch_id`: the same id on a resend, so the service takes the batch once."""
    params: dict = {"entries": entries}
    if batch_id:
        params["id"] = batch_id
    return _dict(call_within(Method.TELEMETRY_SEND, params, budget=budget, deadline=deadline, on_sent=on_sent,
                             client_version=client_version))


def owner_of(pid: int) -> dict | None:
    """`{pid, start_time}` for a running pid, or None (a heartbeat-only owner)."""
    start = procid.start_time(pid)
    return {"pid": pid, "start_time": start} if start else None


def register(
    sid: str,
    harness: str,
    mode: str,
    owner: dict | None,
    generation: int,
    *,
    client: str = Client.TAP,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
    report_env: bool | None = None,
) -> dict:
    params = {"sid": sid, "harness": harness, "mode": mode, "owner": owner, "generation": generation}
    if report_env is not None:
        params[REPORT_ENV] = report_env
    answer = call(Method.SESSION_REGISTER, params, client=client, client_version=client_version, timeout=timeout)
    return answer if isinstance(answer, dict) else {}


def end(
    sid: str,
    generation: int,
    *,
    client: str = Client.TAP,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    params = {"sid": sid, "generation": generation}
    answer = call(Method.SESSION_END, params, client=client, client_version=client_version, timeout=timeout)
    return answer if isinstance(answer, dict) else {}


def _dict(answer: object) -> dict:
    return answer if isinstance(answer, dict) else {}


def finish_recorders(
    harness: str,
    *,
    wait: float = 0.0,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`recorders.finish`: `{finished: [sid], unfinished: [sid]}`. The socket
    waits `wait` longer than `timeout`, the service's own wait."""
    params = {"harness": harness, "wait": wait}
    return _dict(call(Method.RECORDERS_FINISH, params, client=client, client_version=client_version,
                      timeout=timeout + wait))


def hold_run(
    run: str,
    owner: dict,
    *,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`run.hold`: `{held: true}` or `{held: false, reason}`."""
    params = {"run": run, "owner": owner}
    return _dict(call(Method.RUN_HOLD, params, client=client, client_version=client_version, timeout=timeout))


def release_run(
    run: str,
    owner: dict | None = None,
    *,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`run.release`: `{released: n}`."""
    params: dict = {"run": run}
    if owner is not None:
        params["owner"] = owner
    return _dict(call(Method.RUN_RELEASE, params, client=client, client_version=client_version, timeout=timeout))


def refresh_manifest(
    context: dict | None = None,
    *,
    wait: float = 0.0,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`manifest.refresh`: `{outcome, done, result}`. `context`: the caller's
    environment, by `protocol.CONTEXT_ENV` name."""
    params: dict = {"wait": wait}
    if context:
        params["context"] = context
    return _dict(call(Method.MANIFEST_REFRESH, params, client=client, client_version=client_version,
                      timeout=timeout + wait))


def queue_hook(
    event: dict,
    *,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`hooks.queue` (plan D36): hand the service one hook event's
    record-keeping routes, to run once by `event["id"]` (`{id, plugin_root,
    lane, payload, env, cwd, at}`, `protocol.need_hook_queue`). The answer:
    `{"queued": true, "duplicate": bool}`, or `{"refused": why}` (then run them
    yourself). OSError when no service runs. The plugin's dispatcher speaks it
    on its own socket (`hooks/dispatch.py` `Miss`): it cannot import this."""
    answer = call(Method.HOOKS_QUEUE, event, client=client, client_version=client_version, timeout=timeout)
    return answer if isinstance(answer, dict) else {}


def desktop_request(
    request: dict,
    context: dict | None = None,
    *,
    origin: str | None = None,
    client: str = Client.APP,
    client_version: str = "unknown",
    timeout: float = DESKTOP_TIMEOUT_S,
) -> dict:
    """`desktop.request`: one management request (`probe desktop request
    --json`'s stdin object) -> its v1 envelope, `{v, ok, data | error}`.
    `origin="desktop"`: a person asked. ServiceError for a refusal (its
    `data.reason`), OSError when no service answers: the caller then runs the
    CLI, as before (`agent/src/probe/service/DESIGN.md`)."""
    params: dict = {"request": request}
    if context:
        params["context"] = context
    if origin is not None:
        params["origin"] = origin
    return _dict(call(Method.DESKTOP_REQUEST, params, client=client, client_version=client_version, timeout=timeout))


def locations() -> dict:
    """Where this machine's service keeps its files, for a client that speaks
    API v1 itself and must not work out the machine key again (the desktop
    app, pi's tool guard): `socket` (connect here first), `legacy_socket`
    (then here: a service from before the machine key), `service_dir`,
    `root_dir`, `runtime` (the CLI's interpreter, `runtime.json`) and
    `machine_key`. All absolute; nothing is created."""
    return {
        "socket": str(paths.socket_path()),
        "legacy_socket": str(paths.legacy_socket_path()),
        "service_dir": str(paths.service_dir()),
        "root_dir": str(paths.root_dir()),
        "runtime": str(paths.runtime_path()),
        "machine_key": paths.machine_key(),
    }
def refresh_statusline(
    sid: str,
    plugin_root: str,
    *,
    agent: str | None = None,
    python: str | None = None,
    context: dict | None = None,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`statusline.refresh`: `{scheduled: true, due_in}` or `{scheduled: false, reason}`."""
    params: dict = {"sid": sid, "plugin_root": plugin_root}
    if agent:
        params["agent"] = agent
    if python:
        params["python"] = python
    if context:
        params["context"] = context
    return _dict(call(Method.STATUSLINE_REFRESH, params, client=client, client_version=client_version,
                      timeout=timeout))


def sync_notes(
    mode: str,
    *,
    rules_for: list | None = None,
    feedback: bool = False,
    context: dict | None = None,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`notes.sync`: `{outcome: scheduled | queued}` or `{outcome: null, reason}`."""
    params: dict = {"mode": mode, "rules_for": list(rules_for or []), "feedback": feedback}
    if context:
        params["context"] = context
    return _dict(call(Method.NOTES_SYNC, params, client=client, client_version=client_version, timeout=timeout))


def start_import(
    job_id: str,
    directory: str,
    attempt: int,
    env: dict,
    *,
    client: str = Client.CLI,
    client_version: str = "unknown",
    timeout: float = TIMEOUT_S,
) -> dict:
    """`import.start`: `{started: true, pid, start_time}` or `{started: false, reason}`."""
    params = {"job_id": job_id, "directory": directory, "attempt": attempt, "env": env}
    return _dict(call(Method.IMPORT_START, params, client=client, client_version=client_version, timeout=timeout))


def wait_for_exit(pid: int, *, timeout: float = WAIT_FOR_PID_S, poll: float = 0.25) -> bool:
    """Until `pid` is gone (True), or `timeout` passed (False).

    The CLI asks for an update from its root callback, before its own command
    runs, and imports lazily all the way through it: an update that started
    now would replace files that command is still reaching for (a
    `ModuleNotFoundError`; `probe.shared.updates.autoupdate.wait_for_pid_exit`). The
    barrier stops new starts only, so the asking process waits it out here."""
    deadline = time.monotonic() + timeout
    while True:
        if not procid.exists(pid):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def check_now(
    trigger: str = "cli",
    *,
    wait: float = CHECK_NOW_WAIT_S,
    client: str = Client.CLI,
    client_version: str = "unknown",
) -> dict:
    """Ask the service for an update check now, starting it when none runs.
    `trigger` says what asks (`protocol.CHECK_TRIGGERS`). OSError when no
    service answers after `wait`."""
    ensure(wait=wait)
    answer = call(
        Method.UPDATE_CHECK_NOW, {"trigger": trigger}, client=client, client_version=client_version
    )
    return answer if isinstance(answer, dict) else {}
