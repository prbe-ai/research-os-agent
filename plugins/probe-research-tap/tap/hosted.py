"""One session's recorder, run as a thread of the Probe service (plan A2, D2, D3).

The service imports this package from the newest installed tap plugin and,
for each session it records, runs `record()` on a thread of its own: the same
`tap watch` loop (`tap.main.main`, hosted), with the session's environment
(`threadenv`), its own log file (`<log dir>/<sid>.log`, as `tap watch`
writes: the records of this thread, from the tap's loggers), its own stop (the session ended: finish it, FINALIZE included) and
pause (the service restarts: leave now; the next service resumes from this
session's saved offsets and outbox). One recorder per session still: the
journal's live claim (`session_journal.claim_live`) refuses a second, so a
session whose own `tap watch` still runs is never recorded twice (D39), and
the thread ends at once (`record` returns CLAIMED).

Never raises: the service's per-session guard restarts a thread that ended
for any reason but the session's end.

Stdlib only.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from dataclasses import dataclass, field

from tap import threadenv

#: Every logger the tap writes to (its modules use both spellings).
LOGGERS = ("tap", "probe-research-tap")

#: `record`'s outcomes.
ENDED = "ended"  # the session ended and its recorder finished it
PAUSED = "paused"  # told to leave without finishing (a service restart)
STOPPED = "stopped"  # the loop returned for another reason (no token, killswitch, claimed elsewhere)
FAILED = "failed"  # an error the service's guard restarts after


@dataclass
class Controls:
    stop: threading.Event = field(default_factory=threading.Event)
    pause: threading.Event = field(default_factory=threading.Event)
    #: The service's handle on this session's daemon worker
    #: (`probe.service.workers.Link`), set by a service that supervises
    #: workers itself: the tap's Supervisor hands its worker over to it.
    #: None (an older service): the tap's Supervisor spawns it, as before.
    workers: object = None
    #: The journal folder (`<transcript state>/<namespace>`: one backend,
    #: customer and agent) this session's recorder holds the live claim in,
    #: while it holds it; None otherwise. The service reads the session's
    #: receipts from there (`probe.service.receipts`), never by probing a lock.
    claimed: str | None = None
    #: When (monotonic, this process's clock) the recorder's loop last made
    #: progress (`beat`); None before its first pass. A Probe service reads it:
    #: a beat older than 5 min while the agent runs is a stalled recorder.
    beaten: float | None = None
    #: This recording's number among its session's recordings in this process
    #: (`record` numbers it on its first call; a retried call keeps it).
    generation: int | None = None

    def claim(self, folder: str | None) -> None:
        """Published by the recorder's loop (`WatchConfig.claimed`)."""
        self.claimed = folder

    def beat(self) -> None:
        """Called by the recorder's loop on every pass and every second it waits
        (`WatchConfig.beat`). One float written: nothing to lock."""
        self.beaten = time.monotonic()


class _ThisThread(logging.Filter):
    """This session's records: its recorder thread's, and its service link's."""

    def __init__(self, ident: int, sid: str) -> None:
        super().__init__()
        self.ident = ident
        self.link = f"probe-service-link-{sid[:12]}"

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread == self.ident or record.threadName == self.link


def ending(session: dict) -> bool:
    """Did the session's SessionEnd come (its shutdown sentinel is there)?
    Asked by the service before it asks the session's worker to hand over."""
    environ = threadenv.install()
    with environ.session(session.get("env") or {}):
        from tap import config as cfg

        return cfg.shutdown_sentinel(str(session["sid"])).exists()


#: `{sid: n}`: how many recordings of each session this process started. A
#: finishing recording and the fresh one a resume started run side by side in
#: one service (D39), and both name this process in the session's marker.
_STARTS: dict[str, int] = {}
_STARTS_LOCK = threading.Lock()


def _numbered(sid: str, controls: Controls) -> int:
    """This recording's number for `sid` in this process: given on its first
    `record` call and kept on `controls`, so the service's retries of the same
    recording (its guard calls `record` again after a failure, its last pass
    included) keep it, and only a new recording counts as newer."""
    with _STARTS_LOCK:
        mine = getattr(controls, "generation", None)
        if not isinstance(mine, int):
            mine = _STARTS[sid] = _STARTS.get(sid, 0) + 1
            with contextlib.suppress(AttributeError):  # a service's own controls without the field
                controls.generation = mine
        return mine


def _drop_marker(path, sid: str | None = None, mine: int | None = None) -> None:
    """The session ended here: its `.hosted` marker goes (when it names this
    process), so a resume of it records again instead of reading as recorded.
    Not when a newer recording of the session started in this process since
    (`mine`, from `_numbered`): that one wrote the same pid into the marker for
    the resumed session, and dropping it would read that session as recorded
    by nobody. The check, the read and the unlink hold `_STARTS_LOCK`: a new
    recording numbered between them (`_numbered` takes the same lock) waits,
    and writes its marker after this one is gone."""
    import os

    with _STARTS_LOCK:
        if sid is not None and mine is not None and _STARTS.get(sid, 0) != mine:
            return
        try:
            if path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                path.unlink()
        except (OSError, ValueError):
            pass


def _argv(session: dict) -> list[str]:
    argv = ["--session-id", str(session["sid"]), "--cwd", str(session["cwd"])]
    if session.get("transcript"):
        argv += ["--transcript", str(session["transcript"])]
    else:
        argv += ["--transcript-dir", str(session["transcript_dir"])]
    return argv


def record(session: dict, controls: Controls) -> tuple[str, str]:
    """Run one session's recorder on this thread until it ends, pauses or
    fails: `(outcome, detail)`. `session`: `{sid, cwd, transcript |
    transcript_dir, env}` (`env`: the environment its `tap watch` would have)."""
    environ = threadenv.install()
    handler: logging.Handler | None = None
    loggers = [logging.getLogger(name) for name in LOGGERS]
    mine = _numbered(str(session["sid"]), controls)
    try:
        with environ.session(session.get("env") or {}):
            from tap import config as cfg
            from tap import main as tap_main

            try:
                log_dir = cfg.log_dir()
                log_dir.mkdir(parents=True, exist_ok=True)
                handler = logging.FileHandler(log_dir / f"{session['sid']}.log")
                handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
                handler.addFilter(_ThisThread(threading.get_ident(), str(session["sid"])))
                for logger in loggers:
                    logger.addHandler(handler)
                    if logger.level == logging.NOTSET or logger.level > logging.INFO:
                        logger.setLevel(logging.INFO)
                    logger.propagate = False
            except OSError:
                handler = None
            logging.getLogger("tap.hosted").info("recording in the Probe service (thread %s); the session's end: %s",
                                                 threading.get_ident(), cfg.shutdown_sentinel(str(session["sid"])))
            # This process records it now (a resumed session's marker named the
            # last service): the hooks' fast path and `probe status` read it.
            try:
                import os

                from tap import record_client

                record_client._write_hosted(str(session["sid"]), os.getpid())
            except Exception:  # noqa: BLE001 -- the next prompt's check rewrites it
                pass
            try:
                code = tap_main.main(_argv(session), hosted=controls)
            except SystemExit as exc:  # argparse, or a sys.exit deep inside
                return FAILED, f"exited {exc.code!r}"
            if controls.pause.is_set():
                return PAUSED, ""
            if controls.stop.is_set() or cfg.shutdown_sentinel(str(session["sid"])).exists():
                _drop_marker(cfg.hosted_file(str(session["sid"])), str(session["sid"]), mine)
                return ENDED, ""
            return STOPPED, f"returned {code!r}"
    except BaseException as exc:  # noqa: BLE001 -- the guard decides; never take the service down
        return FAILED, f"{type(exc).__name__}: {str(exc)[:300]}"
    finally:
        if handler is not None:
            for logger in loggers:
                logger.removeHandler(handler)
            handler.close()
