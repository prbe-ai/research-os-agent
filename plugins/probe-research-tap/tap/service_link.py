"""Tells the Probe service this session is alive, from inside `tap watch` (plan T5).

Every daemon AND inline session runs `tap watch`, so it is the one carrier of
"this session is alive" for the machine-wide service (`probe.service`), which
has no switch: only a session whose mode is off starts nothing here.

    thread start ──mode off──> nothing: no ensure, no connection (plan A2)
                 └─otherwise─> ensure() once, connect, hello, session.register
    every HEARTBEAT_S ───────> session.register again (same generation)
    service gone (EOF) ──────> reconnect (ensure again) and register at once
    owner changed ───────────> a higher generation (a resumed session's new agent)
    owner not running ───────> (`alive: false`) nothing more, no ensure, until
                               a new owner is recorded
    service ended OUR gen ───> a higher generation at once (a lapsed heartbeat);
                               only a NEWER generation's refusal steps back
    switched to off ─────────> one register with mode off: the session stops
                               holding the service, and no ensure while off
    tap watch exits ─────────> session.end (none when it handed the session
                               to the service: `tap/handback.py`)

On its OWN thread with its own 2 s socket deadlines (plan X6), never between
capture operations: a capture tick that runs for minutes must not let the
service's discovery window lapse. It never raises into capture and never
waits on the service.

The owner is the session's agent process, as `tap/owner.py` recorded it at
SessionStart ({pid, start}); pi's extension names its own process instead,
through `PROBE_SERVICE_OWNER_PID` in the env it starts the tap with (pi runs no
`tap owner`). Neither: a heartbeat-only owner, alive while this thread beats.
"""

from __future__ import annotations

import logging
import os
import select
import threading
import time
from collections.abc import Callable

from tap import __version__
from tap import companion_lease as lease
from tap import config as cfg
from tap import owner as session_owner
from tap.service import client, procid
from tap.service.protocol import Client, ErrorCode, Method, Mode, RefusedReason

log = logging.getLogger("tap.service")

#: Re-register this often (API v1: every <= 30 s; a heartbeat-only owner's TTL is 120 s).
HEARTBEAT_S = 20.0
#: After a failed connect or a lost connection, try again this soon.
RETRY_S = 2.0
#: A session whose switch nobody has written yet (`probe session initialize`
#: runs in another plugin's SessionStart, in no fixed order with this one) is
#: given this long before it counts as the default, `on`.
UNDECIDED_WAIT_S = 20.0
#: How long `session.end` may take on the way out (SessionEnd waits on tap watch).
END_TIMEOUT_S = 1.0
OWNER_PID_ENV = "PROBE_SERVICE_OWNER_PID"
#: `<sid>.state` values (`probe.sdk.session_marker`; the tap cannot import it).
_STATE_OFF = "off"
_STATE_DAEMON = "daemon"


def session_mode(session_id: str) -> str | None:
    """The session's Probe mode as API v1 names it, or None while undecided.

    `daemon`: the daemon records (or, in a daemon-profile session, reads only).
    `off`: no Probe at all. Anything else (`full` = on, `read-only` in the agent
    profile) has the agent using Probe itself: `inline`.
    """
    state = lease.session_state(session_id)
    if state is None:
        return None
    if state == _STATE_OFF:
        return Mode.OFF
    if state == _STATE_DAEMON or lease.reads_only(session_id, state):
        return Mode.DAEMON
    return Mode.INLINE


def _env_owner() -> dict | None:
    raw = os.environ.get(OWNER_PID_ENV, "").strip()
    if not raw.isdigit():
        return None
    return client.owner_of(int(raw))


def _now_ms() -> int:
    return int(time.time() * 1000)


class ServiceLink:
    def __init__(
        self,
        session_id: str,
        *,
        harness: str,
        mode_fn: Callable[[str], str | None] = session_mode,
        owner_fn: Callable[[], dict | None] | None = None,
        ensure_fn: Callable[[], str] = client.ensure,
        heartbeat_s: float = HEARTBEAT_S,
        retry_s: float = RETRY_S,
        undecided_wait_s: float = UNDECIDED_WAIT_S,
        env: dict | None = None,
    ) -> None:
        self.session_id = session_id
        #: Hosted by the Probe service (plan A2): the session's environment,
        #: which this link's own thread reads under (`tap.threadenv`).
        self.env = env
        self.harness = harness
        self.mode_fn = mode_fn
        env_owner = _env_owner()
        self.owner_fn = owner_fn or (lambda: self._recorded_owner() or env_owner)
        self.ensure_fn = ensure_fn
        self.heartbeat_s = heartbeat_s
        self.retry_s = retry_s
        self.undecided_wait_s = undecided_wait_s
        self.generation = _now_ms()
        self.registered_mode: str | None = None
        self.superseded = False
        #: `session.end` on the way out (`stop(end=False)`: not).
        self.end_on_exit = True
        self._owner: dict | None = None
        self._owner_seen = False
        #: The service answered `alive: false` for this owner: it is not running.
        self._owner_dead = False
        self._conn: client.Connection | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._in_env, name=f"probe-service-link-{session_id[:12]}", daemon=True)

    def _in_env(self) -> None:
        if self.env is None:
            self._run()
            return
        from tap import threadenv

        with threadenv.install().session(self.env):
            self._run()

    def _recorded_owner(self) -> dict | None:
        record = session_owner._read(self.session_id)
        return {"pid": record.pid, "start_time": record.start} if record else None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> ServiceLink:
        self._thread.start()
        return self

    def stop(self, timeout: float = END_TIMEOUT_S + 1.0, *, end: bool = True) -> None:
        """`end=False`: the session goes on under another registrant (this
        `tap watch` handed it to the Probe service, `tap/handback.py`, or a
        newer link of this one replaces it): no `session.end` on the way out."""
        self.end_on_exit = end
        self._stop.set()
        self._thread.join(timeout)

    # -- the loop ------------------------------------------------------------

    def _run(self) -> None:
        started = time.monotonic()
        failures = 0
        try:
            while not self._stop.is_set() and not self.superseded:
                mode = self.mode_fn(self.session_id)
                if mode is None:
                    if time.monotonic() - started < self.undecided_wait_s:
                        self._stop.wait(self.retry_s)
                        continue
                    mode = Mode.INLINE  # the default state, `on`
                owner = self.owner_fn()
                if not self._owner_seen:
                    self._owner, self._owner_seen = owner, True
                elif owner != self._owner:
                    # A resumed session's new agent process: a higher generation
                    # replaces the old owner, whose exit then ends nothing.
                    self._owner = owner
                    self._owner_dead = False
                    self.generation = max(self.generation + 1, _now_ms())
                if (
                    self._conn is None
                    and self.registered_mode is not None
                    and self._owner is not None
                    and procid.alive(self._owner["pid"], self._owner["start_time"]) is False
                ):
                    # The service went away after this session's agent did (its
                    # exit let the service idle out): ask nothing, start nothing.
                    self._owner_dead = True
                if self._owner_dead:
                    # The agent exited and this tap has not noticed yet: the
                    # session holds nothing, so register nothing and never
                    # start a service for it, until a new owner is recorded.
                    self._drop()
                    self._stop.wait(self.retry_s)
                    continue
                if mode == Mode.OFF and self.registered_mode in (None, Mode.OFF) and self._conn is None:
                    # Off never starts or holds the service (plan A2).
                    self._stop.wait(self.retry_s)
                    continue
                if self._conn is None:
                    self._conn = self._connect(start_service=mode != Mode.OFF)
                    if self._conn is None:
                        # 2, 4, 8, 16 s, then every heartbeat: a service that
                        # cannot start is not asked every two seconds forever.
                        failures += 1
                        self._stop.wait(min(self.heartbeat_s, self.retry_s * 2 ** min(failures - 1, 4)))
                        continue
                    failures = 0
                if not self._register(mode):
                    continue
                if not self._wait(self.heartbeat_s):
                    self._drop()  # the service went away: reconnect at once
        except Exception:  # noqa: BLE001 -- capture must never die for the service
            log.exception("probe service link failed; this session is not registered")
        finally:
            self._finish()

    def _connect(self, *, start_service: bool) -> client.Connection | None:
        conn = client.connect(client.TIMEOUT_S)
        if conn is None:
            if start_service:
                # ensure() reaps, de-duplicates and backs off by itself; the
                # next pass connects to what it started.
                outcome = self.ensure_fn()
                log.info("probe service not running; ensure: %s", outcome)
                if outcome in (client.Ensured.RUNNING, client.Ensured.SPAWNED, client.Ensured.STARTING):
                    conn = self._await_socket()
            if conn is None:
                return None
        try:
            conn.hello(Client.TAP, __version__)
        except (OSError, ValueError, client.ServiceError) as exc:
            log.warning("probe service hello failed: %s", exc)
            conn.close()
            return None
        return conn

    def _await_socket(self) -> client.Connection | None:
        """A just-started service binds within ~0.1 s: look for it briefly."""
        deadline = time.monotonic() + client.TIMEOUT_S
        while not self._stop.is_set() and time.monotonic() < deadline:
            conn = client.connect(client.TIMEOUT_S)
            if conn is not None:
                return conn
            self._stop.wait(0.05)
        return None

    def _register(self, mode: str) -> bool:
        assert self._conn is not None
        params = {
            "sid": self.session_id,
            "harness": self.harness,
            "mode": mode,
            "owner": self._owner,
            "generation": self.generation,
            # Where this session's worker, runs and setups keep their locks: the
            # service's quiet check looks there too (plan D13).
            "state_dir": str(lease.sessions_dir().parent.resolve()),
        }
        try:
            answer = self._conn.call(Method.SESSION_REGISTER, params)
        except client.ServiceError as exc:
            data = exc.data if isinstance(exc.data, dict) else {}
            if exc.code == ErrorCode.REFUSED and data.get("reason") == RefusedReason.STALE_GENERATION:
                standing = data.get("generation")
                if standing == self.generation:
                    # The service ended THIS generation itself (a heartbeat-only
                    # owner past its TTL after a sleep or a stall, or an owner
                    # exit) and refuses it for a while. No newer registrant
                    # exists, so come back under a new generation now.
                    self.generation = max(self.generation + 1, _now_ms())
                    log.info("probe service: this registration had ended; registering as generation %s", self.generation)
                    return False
                # A newer tap registered this session (a resume replaced this
                # daemon): it is theirs now, ending included.
                log.info("probe service: session registered by a newer generation; stepping back")
                self.superseded = True
                return False
            log.warning("probe service refused the registration: %s", exc)
            self._stop.wait(self.retry_s)
            return False
        except (OSError, ValueError):
            self._drop()
            return False
        if mode != self.registered_mode:
            log.info("probe service: session registered (%s, generation %s)", mode, self.generation)
        self.registered_mode = mode
        if mode != Mode.OFF and self._owner is not None and isinstance(answer, dict) and answer.get("alive") is False:
            # Not stored: the service found this owner not running (API v1).
            log.info("probe service: this session's agent process is gone; not registering it again")
            self._owner_dead = True
        return True

    def _wait(self, seconds: float) -> bool:
        """Sleep up to `seconds`; False as soon as the service closes the connection."""
        deadline = time.monotonic() + seconds
        while not self._stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            conn = self._conn
            if conn is None:
                return False
            try:
                # The stream, not `conn.wait_readable`: what this keeps in the
                # buffer below must not read as more to come. Windows: the
                # pipe's own wait (`select` takes sockets only there).
                if hasattr(conn.sock, "readable"):
                    readable = conn.sock.readable(min(remaining, 0.5))
                else:
                    readable, _, _ = select.select([conn.fileno()], [], [], min(remaining, 0.5))
                if readable:
                    data = conn.sock.recv(65536)
                    if not data:
                        return False
                    conn._buffer += data  # an event; nothing here subscribes, kept for the next read
            except (OSError, ValueError):
                return False
        return True

    def _drop(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _finish(self) -> None:
        """`session.end` on the way out, if this thread registered it (a hint:
        the owner's exit is the truth). Bounded by END_TIMEOUT_S."""
        try:
            if self.registered_mode is not None and not self.superseded and self.end_on_exit:
                conn = self._conn
                if conn is None:
                    conn = client.connect(END_TIMEOUT_S)
                    if conn is not None:
                        conn.hello(Client.TAP, __version__)
                if conn is not None:
                    conn.sock.settimeout(END_TIMEOUT_S)
                    conn.call(Method.SESSION_END, {"sid": self.session_id, "generation": self.generation})
                    self._conn = conn
        except (OSError, ValueError, client.ServiceError):
            pass
        finally:
            self._drop()


def start(session_id: str, *, env: dict | None = None) -> ServiceLink | None:
    """The link for this `tap watch`, or None when it cannot start.

    `env`: recorded by the Probe service (`tap.hosted`), the session's
    environment, for the link's own thread. The link registers and heartbeats
    the session there as a `tap watch` would, over the service's own socket.

    On Windows too since R3: the service listens on this user's named pipe
    there (`tap/service/winpipe.py`)."""
    try:
        return ServiceLink(session_id, harness=cfg.capture_source(), env=env).start()
    except Exception:  # noqa: BLE001
        log.exception("probe service link not started")
        return None
