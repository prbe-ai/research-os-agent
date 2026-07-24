"""Run liveness, SDK side: the auto-heartbeat thread.

The server reaps only runs that have beat at least once, so the thread's one job
is to make an SDK-owned run reapable for exactly as long as this process owns
it: first beat immediately at create, stop on the terminal PATCH. The negative
space matters as much: detached creates (CLI `run start`, the miles exporter,
raw `Run(...)` attach handles) must never beat, because beating once and going
silent gets a legitimately-running run reaped as crashed.
"""

from __future__ import annotations

import gc
import time

from probe.sdk.run import Run


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_create_run_beats_immediately_and_finish_stops_it(client, app, monkeypatch):
    monkeypatch.setenv("PROBE_HEARTBEAT_SECONDS", "60")
    run = client.run(experiment="e", hypothesis="h", name="r")
    # First beat lands at start, not one interval later: a run that crashes
    # five seconds in must already be reapable.
    assert _wait_for(lambda: app.run_heartbeats.get(run.id, 0) >= 1)
    assert app.runs[run.id]["last_heartbeat_at"]
    thread = run._hb_thread
    assert thread is not None and thread.is_alive()
    run.finish()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert run._hb_thread is None


def test_beats_keep_coming_on_the_interval(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r", heartbeat=False)
    run.start_heartbeat(0.01)
    assert _wait_for(lambda: app.run_heartbeats.get(run.id, 0) >= 3)
    run.stop_heartbeat()


def test_start_heartbeat_is_idempotent(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r", heartbeat=False)
    run.start_heartbeat(0.01)
    first = run._hb_thread
    run.start_heartbeat(0.01)
    assert run._hb_thread is first
    run.stop_heartbeat()


def test_detached_create_never_beats(client, app, monkeypatch):
    monkeypatch.setenv("PROBE_HEARTBEAT_SECONDS", "60")
    run = client.run(experiment="e", hypothesis="h", name="r", heartbeat=False)
    time.sleep(0.05)
    assert run._hb_thread is None
    assert app.run_heartbeats == {}


def test_attach_handle_is_inert(client, app, monkeypatch):
    """The miles attach path constructs Run(client, get_run(...)) directly; a
    handle that merely OBSERVES a run must never assert its liveness."""
    monkeypatch.setenv("PROBE_HEARTBEAT_SECONDS", "60")
    created = client.run(experiment="e", hypothesis="h", name="r", heartbeat=False)
    attached = Run(client, client.get_run(created.id))
    assert attached._hb_thread is None
    assert app.run_heartbeats == {}


def test_env_kill_switch_disables_the_default(client, app, monkeypatch):
    monkeypatch.setenv("PROBE_HEARTBEAT_SECONDS", "0")
    run = client.run(experiment="e", hypothesis="h", name="r")
    assert run._hb_thread is None


def test_terminal_set_status_stops_nonterminal_does_not(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r", heartbeat=False)
    run.start_heartbeat(0.01)
    thread = run._hb_thread
    run.set_status("running")
    assert thread.is_alive()
    run.set_status("canceled")
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_beat_failures_do_not_kill_the_loop(client, app):
    """Liveness reporting must never take down the work it reports on, and a
    transient failure must not end beating for good — a run that beat once and
    then went silent is exactly what the reaper crashes."""
    run = client.run(experiment="e", hypothesis="h", name="r", heartbeat=False)
    run.start_heartbeat(0.01)
    assert _wait_for(lambda: app.run_heartbeats.get(run.id, 0) >= 1)
    row = app.runs.pop(run.id)  # every beat now 404s
    baseline = app.run_heartbeats[run.id]
    time.sleep(0.05)  # a few failing cycles
    assert run._hb_thread.is_alive()
    app.runs[run.id] = row  # server "recovers"; beating resumes
    assert _wait_for(lambda: app.run_heartbeats[run.id] > baseline)
    run.stop_heartbeat()


def test_dropping_the_handle_stops_the_beat(client, app):
    """A run nobody holds a handle to can never be finished; the honest outcome
    is beats stopping so the reaper flips it — not a leaked thread per run for
    the life of a sweep process that hit an exception path."""
    run = client.run(experiment="e", hypothesis="h", name="r", heartbeat=False)
    run.start_heartbeat(0.01)
    thread = run._hb_thread
    rid = run.id  # captured up front so no closure below pins the handle
    assert _wait_for(lambda: app.run_heartbeats.get(rid, 0) >= 1)
    del run
    gc.collect()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_client_close_stops_the_beat(client, app):
    """Beats ride the client's transport: close() must take them down rather
    than leave threads spinning against a closed httpx client."""
    run = client.run(experiment="e", hypothesis="h", name="r", heartbeat=False)
    run.start_heartbeat(0.01)
    thread = run._hb_thread
    assert _wait_for(lambda: app.run_heartbeats.get(run.id, 0) >= 1)
    client.close()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_late_beat_racing_completion_is_a_noop(client, app):
    run = client.run(experiment="e", hypothesis="h", name="r", heartbeat=False)
    run.finish()
    client.heartbeat_run(run.id)  # 200, not an error — mirrors the backend
    assert app.run_heartbeats.get(run.id, 0) == 0
    assert "last_heartbeat_at" not in app.runs[run.id]


def test_cli_run_start_is_detached_and_never_beats(app, tmp_path, monkeypatch, capsys):
    """`probe run start` prints an id and exits; the run is closed later by
    `probe run end`. If the CLI forgot to opt out, the beat below would land."""
    from probe import cli
    from tests.conftest import make_client

    monkeypatch.setenv("PROBE_HEARTBEAT_SECONDS", "60")
    monkeypatch.setattr(
        cli, "Client", lambda **_kw: make_client(app, tmp_spool=tmp_path / "spool")
    )
    rc = cli.main(["run", "start", "--experiment", "e", "--hypothesis", "h", "--name", "r1"])
    assert rc == 0
    rid = capsys.readouterr().out.strip()
    time.sleep(0.05)
    assert app.run_heartbeats == {}
    assert "last_heartbeat_at" not in app.runs[rid]
