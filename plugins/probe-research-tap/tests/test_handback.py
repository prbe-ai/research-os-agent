"""A session `tap watch` records is handed to the Probe service once the service can record it.

The watcher's real loop (`tap.main._run_durable_loop`) and, for the service,
the real hosted loop on a thread of this process (as `tap.hosted.record` runs
it, retried as the service's guard retries a recorder that found the claim
taken): the journal's live claim decides who records, as it does between two
processes. Only the service's three calls are stood in for (`ServiceCalls`:
`session.record`, `service.status`'s claim, `session.finish`).
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tap import companion_supervisor, handback
from tap import config as cfg
from tap import main as tapmain
from tap import start as tap_start
from tap.service import client
from tap.session_journal import Journal
from tap.storage import Storage
from tests.test_durable_runtime import Wire

_REAL_SLEEP = time.sleep
HOOKS = Path(__file__).resolve().parent.parent / "hooks"
SERVICE_PID = 4242


def _wait(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        _REAL_SLEEP(0.01)
    return bool(predicate())


class Session:
    """One session's transcript, its watcher's config, the server's receipts."""

    def __init__(self, tmp_path: Path, wire: Wire) -> None:
        self.wire = wire
        self.sid = str(uuid4())
        self.path = tmp_path / f"{self.sid}.jsonl"
        self.path.write_text("")
        self.written = 0
        self.tmp = tmp_path
        self.write(3)

    def write(self, count: int) -> None:
        with self.path.open("a") as handle:
            for _ in range(count):
                handle.write(json.dumps(dict(type="user", sessionId=self.sid,
                                             message=dict(role="user", content=f"turn {self.written}"))) + "\n")
                self.written += 1

    def config(self, **hosted) -> SimpleNamespace:
        return SimpleNamespace(
            session_id=self.sid, transcript_path=self.path, cwd=self.tmp, plugin_root=None, token="synthetic",
            active_interval_s=1, idle_interval_s=1, shutdown_sentinel=self.tmp / "shutdown", **hosted,
        )

    def batches(self) -> list[dict]:
        found = [json.loads(body) for (sid, _), body in sorted(self.wire.accepted.items()) if sid == self.sid]
        return sorted(found, key=lambda b: b["batch_seq"])

    def lines(self) -> list[int]:
        return [e["line_no"] for b in self.batches() for e in b.get("events", [])]

    def delivered(self, count: int) -> bool:
        return len(self.lines()) >= count


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_TRANSCRIPT_STATE_DIR", str(tmp_path / "v2"))
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("PROBE_BASE_URL", Wire.base_url)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cfg, "_watcher_dir", lambda: tmp_path)  # the `.hosted` marker, the pid file
    monkeypatch.setattr(tapmain, "_shutdown_requested", False)
    monkeypatch.setattr(tapmain.killswitch, "is_ingestion_enabled", lambda **kw: (True, None))
    monkeypatch.setattr(tapmain.reconcile, "find_gaps", lambda *args, **kw: [])
    monkeypatch.setattr(tapmain, "_transcript_has_active_reader", lambda _path: None)
    # A cadence second is 5 ms here; the hand-back's own clock is real.
    monkeypatch.setattr(tapmain.time, "sleep", lambda _s: _REAL_SLEEP(0.005))
    wire = PostLog()
    monkeypatch.setattr(tapmain, "Wire", lambda *args: wire)
    return Session(tmp_path, wire)


class PostLog(Wire):
    """The server, plus every POST it was sent (`accepted` keeps one per batch)."""

    def __init__(self):
        super().__init__()
        self.posts: list[tuple[str, int]] = []
        self.lock = threading.Lock()

    def post(self, body):
        data = json.loads(body)
        with self.lock:
            self.posts.append((data["session_id"], data["batch_seq"]))
        return super().post(body)


def _each_batch_posted_once(session: Session) -> None:
    sent = Counter(key for key in session.wire.posts if key[0] == session.sid)
    assert sent and set(sent.values()) == {1}, f"a batch sent twice: {sent}"
    assert sorted(sent) == sorted(k for k in session.wire.accepted if k[0] == session.sid)


class Service:
    """The Probe service, as a hand-back sees it. `present`: it answers.
    `answer`: what `session.record` says once it does. Accepting starts a
    recorder of the session: the real hosted loop on a thread, retried while
    it finds the claim taken (the service's guard)."""

    def __init__(self, session: Session, *, answer: dict | None = None, host: bool = True) -> None:
        self.session = session
        self.present = False
        self.answer = answer or {"recording": True, "started": True, "pid": SERVICE_PID}
        self.host = host
        self.records: list[dict] = []
        self.finished: list[str] = []
        self.claimed: str | None = None
        self.controls = SimpleNamespace(stop=threading.Event(), pause=threading.Event())
        self.thread: threading.Thread | None = None
        self.codes: list[int] = []
        #: Called on each `holds` before it answers (a test's hook into the wait).
        self.on_holds = lambda: None
        #: `listed`'s answer: its recorder's row is there (True), gone (False),
        #: or unknown (None: no answer).
        self.row = lambda: False
        self.looked: list[tuple[float, bool | None]] = []

    # -- the three calls -------------------------------------------------------------
    def record(self, params: dict) -> dict:
        self.records.append(params)
        if not self.present:
            return {}
        if self.answer.get("recording") and self.host and self.thread is None:
            self.thread = threading.Thread(target=self._guard, name="service-recorder", daemon=True)
            self.thread.start()
        return dict(self.answer)

    def holds(self, sid: str) -> int | None:
        self.on_holds()
        return SERVICE_PID if self.present and self.claimed is not None and sid == self.session.sid else None

    def finish(self, sid: str, wait: float = 0.0) -> None:  # `wait`: what an older hand-back sent
        self.finished.append(sid)
        self.controls.stop.set()

    def listed(self, sid: str) -> bool | None:
        answer = self.row()
        self.looked.append((time.monotonic(), answer))
        return answer

    # -- its recorder ----------------------------------------------------------------
    def _publish(self, folder: str | None) -> None:
        self.claimed = folder

    def _guard(self) -> None:
        config = self.session.config(stop=self.controls.stop, pause=self.controls.pause, workers=None,
                                     claimed=self._publish)
        storage = Storage(self.session.tmp / "service-legacy.db")
        try:
            while True:
                self.codes.append(tapmain._run_durable_loop(config, storage))
                # `tap.hosted.record`: its stop, a pause, or the session's end.
                if self.controls.stop.is_set() or self.controls.pause.is_set() or config.shutdown_sentinel.exists():
                    return
                _REAL_SLEEP(0.05)  # the claim was taken: again shortly
        finally:
            storage.close()

    def end(self) -> None:
        self.controls.stop.set()
        if self.thread is not None:
            self.thread.join(20)
            assert not self.thread.is_alive(), "the service's recorder did not finish"


class Watcher:
    """`tap watch`'s loop on a thread, with its hand-back."""

    def __init__(self, session: Session, service: Service, **offer) -> None:
        self.session = session
        self.offer = handback.HandBack(
            session.sid, cwd=session.tmp, transcript=session.path, service=service,
            **{"ask_every_s": 0.05, "wait_s": 10.0, **offer},
        )
        self.code: int | None = None
        self.thread = threading.Thread(target=self._run, name="tap-watch", daemon=True)

    def _run(self) -> None:
        storage = Storage(self.session.tmp / "watch-legacy.db")
        try:
            self.code = tapmain._run_durable_loop(self.session.config(), storage, offer=self.offer)
        finally:
            storage.close()

    def start(self) -> Watcher:
        self.thread.start()
        return self

    def join(self, timeout: float = 20.0) -> int:
        self.thread.join(timeout)
        assert not self.thread.is_alive(), "tap watch did not return"
        return self.code


def _claim_is_free(session: Session) -> bool:
    journal = Journal(session.wire.base_url, "synthetic", "claude_code")
    try:
        if not journal.claim_live(session.sid):
            return False
        journal.release_live(session.sid)
        return True
    finally:
        journal.close()


# -- the hand-over ------------------------------------------------------------------


def test_a_watcher_hands_over_when_the_service_appears_and_every_line_goes_once_in_order(session):
    service = Service(session)
    watcher = Watcher(session, service).start()
    assert _wait(lambda: session.delivered(3)), "the watcher records the session"
    assert _wait(lambda: len(service.records) >= 3), "it keeps asking while no service answers"
    session.write(2)
    assert _wait(lambda: session.delivered(5)), "and records on meanwhile"

    service.present = True  # the service is back (its update landed)
    assert watcher.join() == handback.EXIT_HANDED_OVER
    assert service.claimed is not None, "the service's recorder holds the claim"
    assert cfg.hosted_file(session.sid).read_text() == str(SERVICE_PID), "the marker names the service"
    assert not any(b.get("finalize") for b in session.batches()), "a hand-over is not the session's end"

    session.write(3)  # the live tail, after the hand-over
    assert _wait(lambda: session.delivered(8)), "the service's recorder goes on from the journal"
    service.end()
    batches = session.batches()
    assert session.lines() == list(range(8)), "every line once, in order, across the hand-over"
    assert [b["batch_seq"] for b in batches] == list(range(len(batches))), "one stream, no gap, no repeat"
    _each_batch_posted_once(session)
    assert batches[-1]["finalize"] is True, "the service's recorder ends the session"
    assert service.finished == [], "a recorder that took over is never told to finish"


def test_the_handed_over_watchers_daemon_worker_is_asked_to_hand_over_not_stopped(session, monkeypatch):
    calls = []

    def note(name):
        return lambda self: calls.append((threading.current_thread().name, name))

    monkeypatch.setattr(tapmain.Supervisor, "stop", note("stop"))
    monkeypatch.setattr(tapmain.Supervisor, "hand_over", note("hand_over"))
    service = Service(session)
    service.present = True
    assert Watcher(session, service).start().join() == handback.EXIT_HANDED_OVER
    assert [name for thread, name in calls if thread == "tap-watch"] == ["hand_over"]
    service.end()


def test_a_session_ending_as_the_hand_over_is_confirmed_lets_the_worker_finish(session, monkeypatch):
    """Codex on 59ac0c431: SessionEnd during the confirmation wait, then the
    service's recorder confirmed: the watcher still asked its worker to hand
    over, to a successor that never comes (the session is ending). It stops
    it instead: the worker finishes its queue."""
    calls = []

    def note(name):
        return lambda self: calls.append((threading.current_thread().name, name))

    monkeypatch.setattr(tapmain.Supervisor, "stop", note("stop"))
    monkeypatch.setattr(tapmain.Supervisor, "hand_over", note("hand_over"))
    service = Service(session)
    service.present = True

    def session_end_once_held():
        if service.claimed is not None:
            (session.tmp / "shutdown").touch()  # SessionEnd's sentinel: both recorders see it

    service.on_holds = session_end_once_held
    assert Watcher(session, service).start().join() == handback.EXIT_HANDED_OVER
    assert [name for thread, name in calls if thread == "tap-watch"] == ["stop"]
    service.thread.join(20)
    assert not service.thread.is_alive()
    assert session.lines() == list(range(3))
    assert session.batches()[-1]["finalize"] is True, "the service's recorder, holding it, ended it"
    _each_batch_posted_once(session)


def test_a_running_worker_gets_a_fresh_handover_ask_never_one_dated_ahead(tmp_path, monkeypatch):
    """It sees its parent gone and hands over (an ask younger than 10 s,
    `probe.daemon.worker.HANDOVER_FRESH_S`) to the service recorder's worker,
    whose supervisor keeps the ask fresh while it waits. Never dated ahead:
    a session ending after the hand-over must let the worker finish its queue,
    not hand it to nobody for minutes."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    supervisor = companion_supervisor.Supervisor(session_id="s-1", transcript=tmp_path / "t", cwd=tmp_path)
    child = subprocess.Popen(["sleep", "30"])
    try:
        supervisor.child = child
        supervisor.hand_over()
        marker = companion_supervisor.daemon_dir() / "s-1.handover"
        assert time.time() - 5 < marker.stat().st_mtime <= time.time() + 1, "fresh, not dated ahead"
        assert child.poll() is None, "the worker is left running"
        assert supervisor.child is None
    finally:
        child.kill()
        child.wait()


# -- refusals and a service that does not take over ---------------------------------------


def test_a_refusal_for_confinement_is_never_asked_again(session):
    service = Service(session, answer={"recording": False, "reason": "sandboxed"})
    service.present = True
    watcher = Watcher(session, service, ask_every_s=0.0).start()
    assert _wait(lambda: watcher.offer.confined)
    session.write(4)
    assert _wait(lambda: session.delivered(7)), "the watcher records it on"
    _REAL_SLEEP(0.3)  # dozens of passes, each past its ask time
    tapmain._shutdown_requested = True
    assert watcher.join() == 0
    assert len(service.records) == 1, "asked once, never again"
    assert session.lines() == list(range(7))
    assert session.batches()[-1]["finalize"] is True, "the watcher ends the session itself"


def test_a_confinement_refusal_sent_as_an_error_reads_the_same(monkeypatch):
    calls = handback.ServiceCalls()

    def refuse(method, params, **_kw):
        raise client.ServiceError(-32012, "refused: a caller in a sandbox", {"reason": "sandboxed"})

    monkeypatch.setattr(calls, "_call", refuse)
    assert calls.record({}) == {"recording": False, "reason": "sandboxed"}


@pytest.mark.parametrize(
    "version, offered", [("0.293.1", False), ("0.294.0", True), ("0.295.0.dev3", True), (None, False)]
)
def test_only_a_service_that_can_confirm_a_hand_over_is_offered_one(monkeypatch, version, offered):
    """Before CLI 0.294.0 `service.status` has no `recorders[].recording`: such
    a service could take the session and never say so."""
    sent = []

    class Conn:
        sock = SimpleNamespace(settimeout=lambda _t: None)

        def hello(self, _kind, _version):
            return {"service_version": version} if version else {}

        def call(self, method, params):
            sent.append(method)
            return {"recording": True, "started": True, "pid": 7}

        def close(self):
            pass

    monkeypatch.setattr(client, "connect", lambda _timeout: Conn())
    answer = handback.ServiceCalls().record({"sid": "s-1"})
    assert sent == (["session.record"] if offered else [])
    assert answer == ({"recording": True, "started": True, "pid": 7} if offered
                      else {"recording": False, "reason": handback.TOO_OLD})


def test_other_refusals_are_asked_again_at_the_cadence(session):
    service = Service(session, answer={"recording": False, "reason": "no_owner"})
    service.present = True
    watcher = Watcher(session, service).start()
    assert _wait(lambda: len(service.records) >= 3), "no_owner, no_tap, a restart: asked again"
    tapmain._shutdown_requested = True
    assert watcher.join() == 0
    assert not watcher.offer.confined


def test_a_service_that_does_not_take_over_in_time_leaves_the_watcher_recording(session):
    service = Service(session, host=False)  # accepts, and its recorder never gets going
    frees = []
    service.on_holds = lambda: frees.append(_claim_is_free(session)) if not frees else None
    watcher = Watcher(session, service, wait_s=0.3, retry_after_s=3600.0).start()
    assert _wait(lambda: session.delivered(3))
    asked = len(service.records)
    service.present = True
    assert _wait(lambda: service.finished == [session.sid]), "its recorder is told to finish"
    assert frees == [True], "the watcher let go of the claim while it waited"
    session.write(3)
    assert _wait(lambda: session.delivered(6)), "and took it back, and records on"
    _REAL_SLEEP(0.2)
    assert len(service.records) - asked <= 1, "accepted once, not asked again before RETRY_AFTER_S"
    tapmain._shutdown_requested = True
    assert watcher.join() == 0
    assert session.lines() == list(range(6))
    assert session.batches()[-1]["finalize"] is True
    assert not cfg.hosted_file(session.sid).exists()


def _relinks(monkeypatch) -> list[float]:
    relinked: list[float] = []
    monkeypatch.setattr(handback.service_link, "start",
                        lambda sid, **_kw: relinked.append(time.monotonic()) or None)
    return relinked


def test_taken_back_it_registers_again_only_once_the_services_recorder_is_gone(session, monkeypatch):
    """Codex on 59ac0c431: `session.finish(wait=0)` and an immediate new
    registration; the service's recorder, in its retry wait, made one more
    attempt that registered a newer generation and ended it, and the
    watcher's new registration was refused for good. Now the watcher registers
    again only once `service.status` no longer lists that recorder."""
    relinked = _relinks(monkeypatch)
    service = Service(session, host=False)
    gone_at = []

    def row():
        # Gone 0.3 s after it was told to finish (its last attempt, ending).
        if not service.finished:
            return True
        if not gone_at:
            gone_at.append(time.monotonic() + 0.3)
        return time.monotonic() < gone_at[0]

    service.row = row
    watcher = Watcher(session, service, wait_s=0.2, retry_after_s=3600.0).start()
    assert _wait(lambda: session.delivered(3))
    service.present = True
    assert _wait(lambda: bool(relinked)), "registered again"
    assert relinked[0] >= gone_at[0], "not before the service's recorder was gone"
    session.write(2)
    assert _wait(lambda: session.delivered(5)), "and records on"
    tapmain._shutdown_requested = True
    assert watcher.join() == 0
    assert relinked == relinked[:1], "once"
    assert session.lines() == list(range(5))
    _each_batch_posted_once(session)


def test_unknown_or_still_listed_never_reregisters_and_capture_goes_on_meanwhile(session, monkeypatch):
    """Codex on 33d27365e: an unanswered `service.status` read as "gone", and
    the 30 s deadline re-registered with the row still there. Now only a
    confirmed absence re-registers; until then the watcher records on (after
    STAND_DOWN_S) and looks again at each ask interval."""
    monkeypatch.setattr(handback, "STAND_DOWN_S", 0.3)
    relinked = _relinks(monkeypatch)
    service = Service(session, host=False)
    answers = iter([None, None, True, True, None, True])  # unknown, still there, ...
    service.row = lambda: next(answers, False) if service.finished else True  # ... then gone
    watcher = Watcher(session, service, wait_s=0.2, retry_after_s=3600.0, ask_every_s=0.2).start()
    assert _wait(lambda: session.delivered(3))
    service.present = True
    assert _wait(lambda: bool(service.finished))
    session.write(2)
    assert _wait(lambda: session.delivered(5)), "capture goes on while the old recorder is not confirmed gone"
    assert _wait(lambda: bool(relinked)), "registered again once it is"
    gone = [t for t, answer in service.looked if answer is False]
    assert gone and relinked[0] >= gone[0], "never on an unknown or a listed row"
    assert [a for _t, a in service.looked if a is not False][:6] == [None, None, True, True, None, True]
    tapmain._shutdown_requested = True
    assert watcher.join() == 0
    assert session.lines() == list(range(5))
    _each_batch_posted_once(session)


def test_a_session_ending_while_it_stands_down_gets_its_last_pass_at_once(session, monkeypatch):
    """Codex on 33d27365e: the stand-down after a take-back (a 10 s finish,
    then 30 s of status reads) never looked at the session's end: its last
    pass and FINALIZE waited behind it. Now it returns to the last pass at once."""
    relinked = _relinks(monkeypatch)
    service = Service(session, host=False)

    def row():
        if service.finished:
            tapmain._shutdown_requested = True  # SessionEnd, as the stand-down begins
        return True  # the old recorder never goes

    service.row = row
    watcher = Watcher(session, service, wait_s=0.2, retry_after_s=3600.0).start()
    assert _wait(lambda: session.delivered(3))
    service.present = True
    assert _wait(lambda: bool(service.finished))
    ended = time.monotonic()
    assert watcher.join() == 0
    assert time.monotonic() - ended < 3.0, "the last pass came at once"
    assert relinked == [], "no registration for an ending session"
    assert session.batches()[-1]["finalize"] is True
    assert session.lines() == list(range(3))
    _each_batch_posted_once(session)


def test_the_session_ending_while_it_waits_is_ended_by_the_watcher(session):
    service = Service(session, host=False)
    service.present = True
    service.on_holds = lambda: setattr(tapmain, "_shutdown_requested", True)  # SessionEnd, mid-wait
    started = time.monotonic()
    watcher = Watcher(session, service, wait_s=30.0).start()
    assert watcher.join() == 0
    assert time.monotonic() - started < 10, "the wait ends with the session"
    assert service.finished == [session.sid]
    assert session.lines() == list(range(3))
    assert session.batches()[-1]["finalize"] is True, "the watcher held the claim: it finalized"


def test_the_service_taking_the_claim_as_the_wait_ends_is_a_hand_over(session):
    """The service's recorder got the claim after the last look and before the
    watcher could take it back: the watcher leaves it to that recorder."""
    service = Service(session, host=False)
    service.present = True
    other = Journal(session.wire.base_url, "synthetic", "claude_code")  # the service recorder's
    looks = []

    def late_take():
        looks.append(True)
        if len(looks) == 1:
            assert other.claim_live(session.sid)
            return  # taken, not published yet: this look says no
        service.claimed = str(other.directory)

    service.on_holds = late_take
    try:
        assert Watcher(session, service, wait_s=0.0).start().join() == handback.EXIT_HANDED_OVER
        assert service.finished == [], "never told to finish: it holds the session"
        assert len(looks) == 2
    finally:
        other.close()


def test_a_watcher_started_for_a_session_the_service_records_leaves_at_once(monkeypatch):
    monkeypatch.setenv("PROBE_TAP_HOSTED", "1")
    monkeypatch.setattr(handback.record_client, "hosted_pid", lambda sid: SERVICE_PID)
    holder = SimpleNamespace(holds=lambda sid: SERVICE_PID)
    assert handback.recorded_by_service("s-1", holder) is True
    monkeypatch.setattr(handback.record_client, "hosted_pid", lambda sid: None)
    assert handback.recorded_by_service("s-1", holder) is False, "no marker: free, and nothing asked"
    monkeypatch.setenv("PROBE_TAP_HOSTED", "0")
    monkeypatch.setattr(handback.record_client, "hosted_pid", lambda sid: SERVICE_PID)
    assert handback.recorded_by_service("s-1", holder) is False


def test_the_service_status_names_the_claim_only_while_its_recorder_holds_it(monkeypatch):
    """`service.status` `recorders[]` as `probe.service.recorders.RecorderHost.public` serves it."""
    calls = handback.ServiceCalls()
    row = {"sid": "s-1", "alive": True, "recording": True, "started_at": 0.0, "last": "", "failures": 0,
           "harness": "claude_code"}
    answers = {
        "held": {"pid": 77, "recorders": [row]},
        "waiting": {"pid": 77, "recorders": [{**row, "recording": False}]},
        "older tap": {"pid": 77, "recorders": [{**row, "recording": None}]},
        "older service": {"pid": 77, "recorders": [{k: v for k, v in row.items() if k != "recording"}]},
        "other session": {"pid": 77, "recorders": [{**row, "sid": "s-2"}]},
    }
    seen = {}
    for name, status in answers.items():
        monkeypatch.setattr(calls, "_call", lambda method, params, status=status: status)
        seen[name] = calls.holds("s-1")
    assert seen == {"held": 77, "waiting": None, "older tap": None, "older service": None, "other session": None}


# -- the wrappers end on a hand-over ----------------------------------------------------------


def _fake_python(tmp_path: Path, code: int) -> tuple[Path, Path]:
    ran = tmp_path / "ran"
    fake = tmp_path / "fake-python"
    fake.write_text(f'#!/bin/sh\necho "$*" >>"{ran}"\nexit {code}\n')
    fake.chmod(0o755)
    return fake, ran


def _session_start_wrapper() -> str:
    """The wrapper script `hooks/session-start.sh` spawns, as its own bash reads it."""
    done = subprocess.run(
        ["bash", "-c", 'eval "$(sed -n "/^WRAPPER_SCRIPT=/,/^\'\\$/p" "$1")"; printf %s "$WRAPPER_SCRIPT"', "x",
         str(HOOKS / "session-start.sh")],
        capture_output=True, text=True, check=True, timeout=10,
    )
    assert "-m tap watch" in done.stdout, done.stdout
    return done.stdout


@pytest.mark.skipif(os.name != "posix", reason="the wrappers are POSIX shell")
@pytest.mark.parametrize("wrapper", ["session-start.sh", "tap start"])
def test_the_wrapper_ends_when_tap_watch_handed_the_session_over(tmp_path, wrapper):
    fake, ran = _fake_python(tmp_path, handback.EXIT_HANDED_OVER)
    sid = f"pytest-handback-{os.getpid()}-{uuid4().hex[:8]}"
    pid_file = tmp_path / f"{sid}.pid"
    log = tmp_path / "log"
    if wrapper == "tap start":
        argv = ["/bin/sh", "-c", tap_start.WRAPPER, "sh", sid, str(tmp_path), str(fake), str(log), str(pid_file),
                "probe-research-tap", "--transcript", str(tmp_path / "t.jsonl")]
    else:
        argv = ["/bin/bash", "-c", _session_start_wrapper(), "wrapper", sid, str(tmp_path), str(fake), str(tmp_path),
                str(log), str(pid_file), "probe-research-tap", "--transcript", str(tmp_path / "t.jsonl")]
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=4, check=False)
    finally:
        Path(f"/tmp/probe-research-tap-watcher-{sid}.shutdown").unlink(missing_ok=True)
    assert done.returncode == 0, done.stderr
    assert len(ran.read_text().splitlines()) == 1, "no respawn after a hand-over"
    assert "tap watch" in ran.read_text()
    assert not pid_file.exists(), "its pid file goes with it"
