"""parameter validators: one need_* per request field, mailbox ones included."""

from __future__ import annotations

import json
import os
import re
import time

from .env import (
    BASE_URL_ENV,
    CONTEXT_ENV,
    HOOK_EVENT_ID_LEN,
    HOOK_LANES,
    HOOK_WAIT_MS,
    MAX_CONTEXT_VALUE,
    MAX_HOOK_ENV,
    MAX_HOOK_ENV_VALUE,
    MAX_HOOK_PATH,
    MAX_HOOK_QUEUE_ENV,
    MAX_HOOK_QUEUE_ITEM,
    MAX_HOOK_WAIT_MS,
    MAX_TELEMETRY_ENTRIES,
    hook_queue_env,
    runtime_env_name,
)
from .vocabularies import MAX_SUBSCRIBE_SESSIONS
from .wire import MAX_WAIT_S, invalid_params

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
#: A harness registry id's characters (`probe.foundation.harness_registry`'s `id`s: lower case, digits, `_`).
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
#: A message file's name (`probe.shared.mailbox.mailbox._file_name`), and the ids,
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
    `message` (a `probe.shared.mailbox.mailbox.Message` as a dict: a reader's message,
    an answer, a writer's reply, a run alert) or `question` (a held question,
    `probe.shared.mailbox.approvals.Request.to_json()`). Their fields are checked where
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
