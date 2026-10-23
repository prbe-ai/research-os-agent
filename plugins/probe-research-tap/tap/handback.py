"""Hands a session `tap watch` records to the Probe service, once the service can record it.

A session gets a `tap watch` of its own when the service could not take it at
SessionStart: the service was restarting (an update had just landed), or `tap
record` ran past the hook's budget on a slow machine. Before this it stayed on
`tap watch` for its whole life (live run 39933153434: two such sessions were
recorded by the service in 0 of 11 and 0 of 7 samples). Now the watcher, every
ASK_EVERY_S while it holds the session's live claim (`Journal.claim_live`) and
between two passes, asks the service to record it: `session.record`, the call
SessionStart makes, on a connection only (it never starts a service):

    no service, or refused ──> record on; ask again in ASK_EVERY_S
    refused: sandboxed ──────> record on; never ask again (the service never
                               records a confined caller's session)
    a service before 0.294 ──> not asked: it cannot say its recorder holds
                               the claim (asked again in RETRY_AFTER_S)
    restart (a newer tap) ───> record on; ask the next service in RESTARTING_S
    accepted ────────────────> let go of the claim and wait, up to WAIT_S, for
                               the service to say its recorder holds it
                               (`service.status` `recorders[].recording`):
        it does ─────────────> the `.hosted` marker names the service; exit
                               EXIT_HANDED_OVER, which the wrapper reads as done
                               (no respawn). The service's recorder goes on from
                               the journal where this one stopped: no line lost,
                               none sent twice
        not in time ─────────> take the claim back and record on (TAKEN_BACK);
                               the service's recorder, which never got the claim,
                               is told to finish (`session.finish`: without the
                               claim it finishes nothing), and the session
                               registers again only once `service.status`
                               confirms that recorder is gone: within
                               STAND_DOWN_S, else at a later interval (capture
                               goes on meanwhile); ask again in RETRY_AFTER_S
        the session ends ────> whoever holds the claim ends it: the service's
                               recorder if it took it, else this watcher (its
                               last pass and FINALIZE); the daemon worker
                               finishes its queue, never handed over

One recorder per session throughout: the claim is a kernel lock, held by this
watcher or by the service's recorder, never both, and the watcher never exits
before the service said its recorder holds it.

Stdlib only; never raises into capture.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from tap import __version__, record_client, service_link
from tap import config as cfg
from tap.service.protocol import Client, Method, RefusedReason

log = logging.getLogger("tap.handback")

#: How often a recording `tap watch` asks the service to take its session.
ASK_EVERY_S = 60.0
#: Seconds, in place of ASK_EVERY_S: tests shorten it (as the service's
#: `PROBE_SERVICE_RECORDER_RETRY_S` shortens its recorders' retries).
ASK_ENV = "PROBE_TAP_HANDBACK_S"
#: After the service accepted: how long the watcher waits for its recorder to
#: hold the claim. Covers that recorder's first receipts read and, when it
#: tried the claim before the watcher let go, its retry 15 s later
#: (`probe.service.recorders.STOPPED_RETRIES`).
WAIT_S = 60.0
#: The service accepted and did not take the session in time: asked again
#: this much later, not every minute.
RETRY_AFTER_S = 600.0
#: The service restarts onto this tap (`restart`): the next one, this soon.
RESTARTING_S = 10.0
#: While waiting: how often the service is asked whether it holds the claim.
POLL_S = 0.5
#: Taking the claim back: how long another holder's brief hold (an orphan pin)
#: is waited out before the session counts as someone else's.
TAKE_BACK_S = 2.0
CALL_TIMEOUT_S = 2.0
#: Taken back: how long the watcher waits, before it records on, for the
#: service's recorder it told to finish to be gone (its row no longer in
#: `service.status`): this watcher registers again only then. A recorder in its
#: retry wait makes one more attempt when told to finish, and that attempt
#: registers the session under a newer generation and ends it: registered
#: before it is gone, this watcher's registration would be superseded, and
#: step back for good. Not gone (or no answer) by then: capture goes on, and
#: the row is looked at again every ask interval until it is confirmed gone.
STAND_DOWN_S = 10.0
#: The first service (CLI 0.294.0) whose `service.status` says whether its
#: recorder of a session holds the claim (`recorders[].recording`): an older
#: one could never confirm a hand-over, so it is never offered one.
MIN_SERVICE = (0, 294, 0)
#: `record`'s `reason` for such a service (the tap's own word, never the service's).
TOO_OLD = "service_too_old"
#: `tap watch`'s exit once the service records its session: the wrapper
#: (`hooks/session-start.sh`, `tap start`) ends instead of starting another.
EXIT_HANDED_OVER = 12


class Outcome:
    """`HandBack.attempt`'s answer."""

    #: This watcher still records the session (nothing asked, or refused).
    KEPT = "kept"
    #: Accepted, not taken in time: this watcher took the claim back.
    TAKEN_BACK = "taken_back"
    #: The service's recorder holds the claim: this watcher leaves.
    HANDED_OVER = "handed_over"
    #: Neither holds it (another recorder took it while it was free): leave it
    #: to that one, as on any claim found taken.
    LOST = "lost"


class ServiceCalls:
    """The three calls a hand-back makes, each on its own connection. None of
    them starts a service; none raises."""

    def __init__(self, timeout: float = CALL_TIMEOUT_S) -> None:
        self.timeout = timeout

    def _call(self, method: str, params: dict, *, min_service: tuple | None = None) -> object:
        from tap.service import client

        conn = client.connect(self.timeout)
        if conn is None:
            raise ConnectionError("no Probe service answers")
        try:
            conn.sock.settimeout(self.timeout)
            hello = conn.hello(Client.TAP, __version__)
            if min_service is not None and _version(hello.get("service_version")) < min_service:
                return {"recording": False, "reason": TOO_OLD}
            return conn.call(method, params)
        finally:
            conn.close()

    def record(self, params: dict) -> dict:
        """`session.record`'s answer; `{}` when none came. A refusal for the
        caller's confinement reads `reason: sandboxed`, as an answer
        (`CONFINED_ANSWERS`) or as an error. A service before MIN_SERVICE is
        not asked (`reason: service_too_old`)."""
        from tap.service import client

        try:
            answer = self._call(Method.SESSION_RECORD, params, min_service=MIN_SERVICE)
        except client.ServiceError as exc:
            data = exc.data if isinstance(exc.data, dict) else {}
            return {"recording": False, "reason": data.get("reason")}
        except (OSError, ValueError):
            return {}
        return answer if isinstance(answer, dict) else {}

    def _status(self) -> dict | None:
        try:
            status = self._call(Method.STATUS, {})
        except Exception:  # noqa: BLE001 -- no answer
            return None
        return status if isinstance(status, dict) else None

    def holds(self, sid: str) -> int | None:
        """The service's pid when its recorder of `sid` holds the session's
        live claim now (`service.status` `recorders[].recording`, which the
        tap's loop publishes as it takes and lets go of the claim); else None,
        a service too old to say included."""
        status = self._status()
        if status is None:
            return None
        pid = status.get("pid")
        for entry in status.get("recorders") or ():
            if (
                isinstance(entry, dict) and entry.get("sid") == sid and entry.get("alive") is True
                and entry.get("recording") is True and isinstance(pid, int) and not isinstance(pid, bool)
            ):
                return pid
        return None

    def listed(self, sid: str) -> bool | None:
        """Does the service still run a recorder of `sid` (its row in
        `service.status`, alive or in its retry wait)? None when that is
        unknown (no answer, or an answer without `recorders`): never read as
        gone."""
        status = self._status()
        if status is None or not isinstance(status.get("recorders"), list):
            return None
        return any(isinstance(entry, dict) and entry.get("sid") == sid for entry in status["recorders"])

    def finish(self, sid: str) -> None:
        """`session.finish`, not waited on: the service's recorder of `sid`
        ends. Asked only while this watcher holds the claim, so that recorder
        cannot take it for a last pass: it finishes nothing. Whether it is gone
        is `listed`'s to say."""
        with contextlib.suppress(Exception):
            self._call(Method.SESSION_FINISH, {"sid": sid, "wait": 0})


def _version(value: object) -> tuple:
    """`0.294.0` -> (0, 294, 0); its leading numbers only (`0.295.0.dev1`
    -> (0, 295, 0)); () for anything else, which is older than every version."""
    numbers = []
    for part in str(value or "").split("."):
        if not part.isdigit():
            break
        numbers.append(int(part))
    return tuple(numbers)


def ask_every() -> float:
    try:
        return max(0.0, float(os.environ.get(ASK_ENV, ASK_EVERY_S)))
    except ValueError:
        return ASK_EVERY_S


def recorded_by_service(sid: str, service: ServiceCalls | None = None) -> bool:
    """Does the Probe service record `sid` now, holding its claim? A `tap watch`
    started for such a session (an older wrapper respawning one after a
    hand-off, a racing start) leaves at once: no registration of its own to
    supersede the service's. Free unless the `.hosted` marker names a live
    service."""
    try:
        if not record_client.enabled() or record_client.hosted_pid(sid) is None:
            return False
        return (service or ServiceCalls()).holds(sid) is not None
    except Exception:  # noqa: BLE001 -- unknown: record it here
        return False


class HandBack:
    """One `tap watch`'s offers of its session to the Probe service."""

    def __init__(
        self,
        session_id: str,
        *,
        cwd: Path,
        transcript: Path,
        plugin_root: Path | None = None,
        link: object = None,
        service: ServiceCalls | None = None,
        ask_every_s: float | None = None,
        wait_s: float = WAIT_S,
        retry_after_s: float = RETRY_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
        nap: Callable[[float], None] = time.sleep,
    ) -> None:
        self.session_id = session_id
        self.args = argparse.Namespace(
            session_id=session_id, cwd=cwd, transcript=transcript, transcript_dir=None, plugin_root=plugin_root
        )
        #: This session's service link (`service_link.ServiceLink`, or None):
        #: replaced by a newer one when the session is taken back.
        self.link = link
        self.service = service or ServiceCalls()
        self.ask_every_s = ask_every() if ask_every_s is None else ask_every_s
        self.wait_s = wait_s
        self.retry_after_s = retry_after_s
        self.clock = clock
        self.nap = nap
        #: The first ask comes after the first pass that holds the claim.
        self.next_at = clock()
        #: Refused for the caller's confinement: never asked again.
        self.confined = False
        #: Taken back, and the service's recorder not yet confirmed gone: this
        #: session registers again once it is (`_relink_once_gone`).
        self.relink_pending = False
        self.relink_at = 0.0

    def due(self) -> bool:
        now = self.clock()
        return (not self.confined and now >= self.next_at) or (self.relink_pending and now >= self.relink_at)

    def attempt(
        self,
        journal,
        *,
        stopping: Callable[[], bool] = lambda: False,
        tick: Callable[[], None] = lambda: None,
    ) -> str:
        """Offer the session to the service, between two passes (and look
        again whether a recorder it told to finish is gone). Never raises."""
        try:
            if self.relink_pending and self.clock() >= self.relink_at:
                self._relink_once_gone(stopping)
            if self.confined or self.clock() < self.next_at:
                return Outcome.KEPT
            return self._attempt(journal, stopping, tick)
        except Exception:  # noqa: BLE001 -- capture goes on here
            log.exception("handing the session to the Probe service failed; this recorder keeps it")
            self.next_at = self.clock() + self.ask_every_s
            if journal is not None and not journal.holds_live(self.session_id):
                return self._take_back(journal, stopping=stopping)
            return Outcome.KEPT

    def _attempt(self, journal, stopping: Callable[[], bool], tick: Callable[[], None]) -> str:
        sid = self.session_id
        self.next_at = self.clock() + self.ask_every_s
        if journal is None or not journal.holds_live(sid):
            return Outcome.KEPT  # not recording it: nothing to hand over
        answer = self.service.record(record_client._params(self.args))
        if answer.get("recording") is not True:
            if answer.get("reason") == RefusedReason.SANDBOXED:
                self.confined = True
                log.info("the Probe service will not record this session (a caller in a sandbox); "
                         "this tap watch records it for its whole life")
            elif answer.get("restart"):
                self.next_at = self.clock() + RESTARTING_S
            elif answer.get("reason") == TOO_OLD:
                self.next_at = self.clock() + self.retry_after_s  # until it updates
            return Outcome.KEPT
        log.info("the Probe service (pid %s) will record this session; handing it over", answer.get("pid"))
        journal.release_live(sid)
        deadline = self.clock() + self.wait_s
        while True:
            pid = self.service.holds(sid)
            if pid is not None:
                return self._handed_over(pid)
            if stopping() or self.clock() >= deadline:
                break
            tick()
            self.nap(POLL_S)
        return self._take_back(journal, stopping=stopping)

    def _take_back(self, journal, *, stopping: Callable[[], bool] = lambda: False) -> str:
        sid = self.session_id
        deadline = self.clock() + TAKE_BACK_S
        while True:
            if journal.claim_live(sid):
                # Its recorder never got the claim, and cannot while this one
                # holds it: told to finish (not waited on), it ends without a pass.
                self.service.finish(sid)
                self.next_at = self.clock() + self.retry_after_s
                # Registered again once that recorder is confirmed gone; the
                # session's end (this watcher's last pass, now) never waits.
                self.relink_pending, self.relink_at = True, self.clock()
                self._relink_once_gone(stopping, wait_s=STAND_DOWN_S)
                log.warning("the Probe service's recorder did not take this session over (waited up to "
                            "%.0f s); this tap watch records it on, and asks again in %.0f s",
                            self.wait_s, self.retry_after_s)
                return Outcome.TAKEN_BACK
            pid = self.service.holds(sid)
            if pid is not None:
                return self._handed_over(pid)  # it took it as the wait ended
            if self.clock() >= deadline:
                log.warning("another recorder holds this session's claim (not the Probe service's); "
                            "leaving the session to it")
                return Outcome.LOST
            self.nap(0.2)

    def _handed_over(self, pid: int) -> str:
        sid = self.session_id
        record_client._write_hosted(sid, pid)
        # No wrapper runs this session now: what the next resume's check reads.
        with contextlib.suppress(OSError):
            cfg.version_file(sid).unlink()
        log.info("the Probe service (pid %s) records this session now; this tap watch leaves it to it", pid)
        return Outcome.HANDED_OVER

    def _relink_once_gone(self, stopping: Callable[[], bool], *, wait_s: float = 0.0) -> None:
        """Register again, but only once `service.status` confirms the
        recorder told to finish is gone: only then is this watcher's the newest
        registration. Waits `wait_s` at most, in short status calls, and not
        at all once the session ends (its last pass comes first). Not
        confirmed: looked at again at the next ask interval."""
        sid = self.session_id
        deadline = self.clock() + wait_s
        while not stopping():
            if self.service.listed(sid) is False:
                self.relink_pending = False
                self._relink()
                return
            if self.clock() >= deadline:
                break
            self.nap(POLL_S)
        self.relink_at = self.clock() + self.ask_every_s

    def _relink(self) -> None:
        """Taken back: this session registers again under a newer generation.
        The offer's own registration and the service recorder's link outrank
        this watcher's, and the latter's end leaves a tombstone above it."""
        old, self.link = self.link, None
        if old is not None:
            with contextlib.suppress(Exception):
                old.stop(end=False)
        self.link = service_link.start(self.session_id)
