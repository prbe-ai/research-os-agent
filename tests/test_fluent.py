"""The module-level layer: probe.init() / probe.log() / probe.finish().

The W&B ergonomic, minus the process-wide global that makes it dangerous. These
tests are mostly about the binding rules, because that is the part that differs.
"""

from __future__ import annotations

import threading

import pytest

import probe
from probe.sdk import fluent
from tests.conftest import make_client


@pytest.fixture(autouse=True)
def _clean_binding():
    """The layer keeps process state on purpose, so tests must not inherit it."""
    fluent._current.set(None)
    fluent._process_default = None
    fluent._exit_status = "completed"
    yield
    fluent._current.set(None)
    fluent._process_default = None


@pytest.fixture
def wired(app, tmp_path, monkeypatch):
    """Make a bare `probe.init()` build a client wired to the fake backend, so the
    owned-client path (the one init actually takes in real use) is under test."""
    built = []

    def factory(*_a, **_kw):
        client = make_client(app, tmp_spool=tmp_path / "spool")
        built.append(client)
        return client

    monkeypatch.setattr(fluent, "Client", factory)
    app.seed_experiment("e1")
    return built


# -- the basic round trip ------------------------------------------------------
def test_init_log_finish(app, wired):
    run = probe.init(experiment="e1", name="r1")
    probe.log({"loss": 0.4}, step=1)
    probe.finish()

    assert app.runs[run.id]["status"] == "completed"
    assert app.metrics_inserted == 1


def test_active_run_is_none_until_init(app, wired):
    assert probe.active_run() is None
    run = probe.init(experiment="e1", name="r1")
    assert probe.active_run() is run
    probe.finish()
    assert probe.active_run() is None


def test_logging_with_no_active_run_says_so(app, wired):
    """The failure mode this replaces is W&B's: log() before init() silently
    starts a run, or drops the call. Neither is recoverable from the outside."""
    with pytest.raises(probe.errors.RosError, match="no active run"):
        probe.log({"loss": 0.4})


def test_init_returns_a_real_run_handle(app, wired):
    """It is not a proxy — the explicit API stays the implementation, so
    everything on Run is reachable without going through this layer."""
    run = probe.init(experiment="e1", name="r1")
    assert isinstance(run, probe.Run)
    run.link(wandb_run_id="abc")
    probe.finish()
    assert app.runs[run.id]["foreign_keys"] == {"wandb_run_id": "abc"}


def test_init_is_usable_as_a_context_manager(app, wired):
    with probe.init(experiment="e1", name="r1") as run:
        probe.log({"loss": 0.4})
    assert app.runs[run.id]["status"] == "completed"


# -- binding rules -------------------------------------------------------------
def test_a_worker_thread_reaches_the_run(app, wired):
    """The reason there is a process default at all. Threads start with an EMPTY
    context, so a contextvar alone would leave a DataLoader worker — or any
    library logging from a thread — silently unable to find the run."""
    run = probe.init(experiment="e1", name="r1")
    seen = {}

    def worker():
        seen["run"] = probe.active_run()
        probe.log({"loss": 0.1}, step=1)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()
    probe.finish()

    assert seen["run"] is run


def test_a_scoped_init_shadows_rather_than_hijacks(app, wired):
    """W&B's global is last-writer-wins process-wide, so a second run started
    anywhere silently steals every subsequent log(). Here the context is
    consulted first, so the thread's own init cannot redirect the main thread."""
    outer = probe.init(experiment="e1", name="outer")
    inner_seen = {}

    def worker():
        inner = probe.init(experiment="e1", name="inner")
        inner_seen["run"] = probe.active_run()
        assert inner_seen["run"] is inner

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    # the main thread still logs into its OWN run, not the thread's
    assert probe.active_run() is outer
    assert inner_seen["run"] is not outer


# -- client ownership ----------------------------------------------------------
def test_finish_closes_the_client_init_built(app, wired):
    probe.init(experiment="e1", name="r1")
    (built,) = wired
    probe.finish()
    assert built.transport._client.is_closed


def test_a_caller_supplied_client_is_left_open(app, client):
    """Closing it would kill a transport — and any other run's heartbeat — that
    the caller is still using. Ownership follows who built it."""
    app.seed_experiment("e1")
    probe.init(client=client, experiment="e1", name="r1")
    probe.finish()
    assert not client.transport._client.is_closed


def test_finish_twice_is_a_no_op(app, wired):
    probe.init(experiment="e1", name="r1")
    probe.finish()
    assert probe.finish() is None


def test_a_failed_init_does_not_leak_its_client(app, wired):
    """init() builds a client before it knows the run will open. A transport left
    behind here would keep a connection pool — and later, heartbeat threads —
    alive with nothing able to close them."""
    with pytest.raises(probe.errors.RosError):
        probe.init(experiment="does-not-exist-and-has-no-hypothesis", name="r1")
    (built,) = wired
    assert built.transport._client.is_closed
    assert probe.active_run() is None


# -- exit handling -------------------------------------------------------------
def test_atexit_completes_a_run_the_script_forgot_to_close(app, wired):
    """A clean script that never calls finish() would otherwise sit `running`
    until the server reaper marks it crashed — the wrong answer for one that
    worked."""
    run = probe.init(experiment="e1", name="r1")
    fluent._finish_at_exit()
    assert app.runs[run.id]["status"] == "completed"


def test_atexit_does_not_claim_success_after_an_unhandled_exception(app, wired):
    """atexit still runs after a traceback prints, so guessing `completed` for
    every exit would record a lie."""
    run = probe.init(experiment="e1", name="r1")
    try:
        raise ValueError("boom")
    except ValueError as exc:
        fluent._excepthook(type(exc), exc, exc.__traceback__)
    fluent._finish_at_exit()
    assert app.runs[run.id]["status"] == "failed"


def test_an_interrupt_is_canceled_not_failed(app, wired):
    """`canceled` is a real status here, and "I stopped it" is different
    information from "it broke"."""
    run = probe.init(experiment="e1", name="r1")
    exc = KeyboardInterrupt()
    fluent._excepthook(KeyboardInterrupt, exc, None)
    fluent._finish_at_exit()
    assert app.runs[run.id]["status"] == "canceled"


def test_atexit_is_silent_when_nothing_is_active(app, wired):
    fluent._finish_at_exit()  # must not raise


# -- regressions caught in pre-landing review ---------------------------------
def test_finish_from_another_thread_clears_the_binding_everywhere(app, wired):
    """finish() can only clear the contextvar of the thread that CALLS it. A
    worker closing the run must not leave the main thread logging into a
    completed one, so the binding itself carries the closed flag."""
    probe.init(experiment="e1", name="r1")
    thread = threading.Thread(target=probe.finish)
    thread.start()
    thread.join()

    assert probe.active_run() is None
    with pytest.raises(probe.errors.RosError, match="no active run"):
        probe.log({"loss": 0.4})


def test_the_excepthook_is_captured_once_and_never_chains_to_itself(app, wired):
    """Unsynchronised, two concurrent init()s could both pass the installed check
    and the second would capture _excepthook as its own predecessor — every later
    uncaught exception then recurses until the stack blows."""
    probe.init(experiment="e1", name="r1")
    first = fluent._previous_excepthook
    probe.finish()
    probe.init(experiment="e1", name="r2")
    assert fluent._previous_excepthook is first
    assert fluent._previous_excepthook is not fluent._excepthook


def test_a_new_run_does_not_inherit_the_previous_exit_status(app, wired):
    """_exit_status is process-wide, so without a reset a run that follows a
    failed one would be closed as failed at exit."""
    probe.init(experiment="e1", name="r1")
    fluent._excepthook(ValueError, ValueError("boom"), None)
    probe.finish()

    run = probe.init(experiment="e1", name="r2")
    fluent._finish_at_exit()
    assert app.runs[run.id]["status"] == "completed"
