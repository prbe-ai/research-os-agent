"""`tap start` — gates, pid-file states, and the Probe service as the only recorder (service-only task 15)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tap import config as cfg
from tap import start as tap_start

#: One fixed id for every test here, so the fixture can scrub the /tmp files
#: that carry it. `cfg.pid_file` and `cfg.shutdown_sentinel` live in the shared
#: /tmp by design (session-end.sh and a detached wrapper both have to find
#: them), so tmp_path isolation does not reach them.
SID = "01a06383-6f4e-751a-b94c-bcefef19938c"


@pytest.fixture
def plugin_dir(tmp_path, monkeypatch):
    """A private plugin state dir, so no test touches a real install."""
    d = tmp_path / "state"
    d.mkdir()
    monkeypatch.setenv("PROBE_TAP_SOURCE", "pi")
    monkeypatch.setenv("PROBE_PI_TAP_PLUGIN_DIR", str(d))
    # `load_token()`'s last resort is the probe CLI's own config.json. Without
    # this the "unpaired" tests read the developer's real credentials and pass
    # or fail on whether they happen to be logged in.
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(d / "absent-probe-config.json"))
    monkeypatch.delenv("PROBE_PI_TAP_TOKEN", raising=False)
    for p in (cfg.pid_file(SID), cfg.shutdown_sentinel(SID), cfg.version_file(SID)):
        p.unlink(missing_ok=True)
    yield d
    for p in (cfg.pid_file(SID), cfg.shutdown_sentinel(SID), cfg.version_file(SID)):
        p.unlink(missing_ok=True)


@pytest.fixture
def paired(plugin_dir):
    (plugin_dir / ".token").write_text("probe_ing_test", encoding="utf-8")
    return plugin_dir


def _args(tmp_path, **over):
    transcript = over.pop("transcript", None)
    if transcript is None:
        transcript = tmp_path / "session.jsonl"
        transcript.write_text('{"type":"session"}\n', encoding="utf-8")
    return [
        "--session-id", over.pop("session_id", SID),
        "--cwd", str(over.pop("cwd", tmp_path)),
        "--transcript", str(transcript),
    ]


class _Service:
    """The Probe service as `tap start` asks it (`record`): what it was asked, and its answer."""

    def __init__(self, takes: bool = True) -> None:
        self.takes = takes
        self.asked: list = []

    def __call__(self, args) -> bool:
        self.asked.append(args)
        return self.takes


def test_unpaired_refuses_with_its_own_exit_code(plugin_dir, tmp_path):
    service = _Service()
    assert tap_start.main(_args(tmp_path), record=service) == tap_start.EXIT_NOT_PAIRED
    assert service.asked == []


def test_killswitch_refuses(paired, tmp_path):
    (paired / ".disabled").write_text("", encoding="utf-8")
    service = _Service()
    assert tap_start.main(_args(tmp_path), record=service) == tap_start.EXIT_KILLSWITCH
    assert service.asked == []


def test_disabled_path_refuses(paired, tmp_path):
    (paired / ".disabled_paths").write_text(str(tmp_path) + "\n", encoding="utf-8")
    service = _Service()
    assert tap_start.main(_args(tmp_path), record=service) == tap_start.EXIT_DISABLED_PATH
    assert service.asked == []


def test_missing_transcript_refuses(paired, tmp_path):
    service = _Service()
    code = tap_start.main(_args(tmp_path, transcript=tmp_path / "gone.jsonl"), record=service)
    assert code == tap_start.EXIT_NO_TRANSCRIPT
    assert service.asked == []


def test_old_interpreter_refuses(paired, tmp_path):
    service = _Service()
    code = tap_start.main(_args(tmp_path), record=service, version_info=(3, 10, 14))
    assert code == tap_start.EXIT_INTERPRETER_TOO_OLD
    assert service.asked == []


def test_past_the_gates_the_service_is_asked_once_with_the_transcript(paired, tmp_path):
    service = _Service()
    assert tap_start.main(_args(tmp_path), record=service) == tap_start.EXIT_OK
    [asked] = service.asked
    assert asked.session_id == SID and str(asked.transcript).endswith("session.jsonl")
    assert not cfg.version_file(SID).exists() and not cfg.pid_file(SID).exists(), "nothing records here"


def test_a_service_that_does_not_take_it_is_not_recorded_and_nothing_starts_here(paired, tmp_path):
    """Service-only task 15: no `tap watch` of its own; the next prompt's heal asks again."""
    assert tap_start.main(_args(tmp_path), record=_Service(takes=False)) == tap_start.EXIT_NOT_RECORDED
    assert not cfg.pid_file(SID).exists()


def test_a_live_old_wrapper_is_already_running(paired, tmp_path, monkeypatch):
    """A `tap watch` an older tap started still records the session: left to it."""
    cfg.pid_file(SID).write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(tap_start, "_looks_like_the_uploader", lambda pid: True)
    service = _Service()
    assert tap_start.main(_args(tmp_path), record=service) == tap_start.EXIT_OK
    assert service.asked == []


def test_a_dead_or_foreign_pid_is_stale_and_the_service_is_asked(paired, tmp_path, monkeypatch):
    for pid, ours in (("999999999", True), (str(os.getpid()), False)):
        cfg.pid_file(SID).write_text(pid, encoding="utf-8")
        monkeypatch.setattr(tap_start, "_looks_like_the_uploader", lambda _pid, ours=ours: ours)
        service = _Service()
        assert tap_start.main(_args(tmp_path), record=service) == tap_start.EXIT_OK
        assert len(service.asked) == 1 and not cfg.pid_file(SID).exists()


def test_a_stale_shutdown_sentinel_is_cleared_before_asking(paired, tmp_path):
    sentinel = cfg.shutdown_sentinel(SID)
    sentinel.write_text("", encoding="utf-8")
    tap_start.main(_args(tmp_path), record=_Service())
    assert not sentinel.exists()


def test_heal_marker_is_under_the_plugin_state_dir(plugin_dir):
    """The bash hook stats this exact path — see hooks/ensure-daemon.sh."""
    assert cfg.heal_marker(SID) == Path(plugin_dir) / "heal" / SID


def test_a_live_uploader_is_recognised_in_a_narrow_terminal(monkeypatch):
    """`ps` cuts at `$COLUMNS` unless told `-ww`, and the wrapper's first `tap`
    sits ~550 characters in, so a narrow caller read a live daemon as stale
    and spawned a second one. Same check as probe's `capture_state`."""
    import subprocess
    import sys

    # argv[0] fixed so only the args can carry `tap` (see test_capture_state.py).
    proc = subprocess.Popen(
        ["sleeper", "-c", "import time; time.sleep(30)", "x" * 600, "tap"], executable=sys.executable
    )
    try:
        monkeypatch.setenv("COLUMNS", "80")
        cut = subprocess.run(
            ["/bin/ps", "-p", str(proc.pid), "-o", "command="], capture_output=True, text=True
        ).stdout
        assert "tap" not in cut, "precondition: without -ww the line must be cut before `tap`"
        assert tap_start._looks_like_the_uploader(proc.pid) is True
    finally:
        proc.kill()
        proc.wait()


# -- a session the Probe service records ------------------------------------------------


def test_a_resume_while_the_hosted_session_finishes_records_it_again(paired, tmp_path, monkeypatch):
    """Audit #10, pi quit-then-resume: pi's quit touches the shutdown sentinel,
    and the service's recorder makes its last pass and FINALIZEs. Until its
    thread ends the service still answers `recording`, and a `tap start` for
    the resumed session in that window read "already recorded": it returned
    without clearing the sentinel or asking the service, the recorder ended,
    and nothing recorded the resumed session."""
    sentinel = cfg.shutdown_sentinel(SID)
    sentinel.touch()
    # The marker names a live service that still answers recording=True.
    from tap import record_client

    monkeypatch.setattr(record_client, "hosted_pid", lambda sid: 4242)
    monkeypatch.setattr(record_client, "recording", lambda args: 0)
    asked = []
    code = tap_start.main(_args(tmp_path), record=lambda a: asked.append(a) or True)
    assert code == tap_start.EXIT_OK
    assert len(asked) == 1, "the resumed session was never asked to be recorded"
    assert not sentinel.exists(), "the sentinel would end the resumed session's recorder"


def test_a_hosted_session_that_is_not_finishing_is_already_running(paired, tmp_path, monkeypatch):
    from tap import record_client

    monkeypatch.setattr(record_client, "hosted_pid", lambda sid: 4242)
    monkeypatch.setattr(record_client, "recording", lambda args: 0)
    asked = []
    code = tap_start.main(_args(tmp_path), record=lambda a: asked.append(a) or True)
    assert (code, asked) == (tap_start.EXIT_OK, [])
    assert tap_start.daemon_state(SID) == ("running", 4242)


def test_a_finishing_hosted_session_reads_as_not_recorded(paired, monkeypatch):
    from tap import record_client

    monkeypatch.setattr(record_client, "hosted_pid", lambda sid: 4242)
    monkeypatch.setattr(record_client, "recording", lambda args: 0)
    cfg.shutdown_sentinel(SID).touch()
    assert tap_start.daemon_state(SID) == ("none", None)


def test_pi_0_6_2s_resume_with_no_watcher_files_clears_the_sentinel_and_asks(paired, tmp_path):
    """What pi 0.6.2 relies on after its quit removed every watcher file: `tap
    start` reads "none", clears the sentinel and asks the service."""
    cfg.hosted_file(SID).unlink(missing_ok=True)
    sentinel = cfg.shutdown_sentinel(SID)
    sentinel.touch()
    assert tap_start.daemon_state(SID) == ("none", None)
    asked = []
    code = tap_start.main(_args(tmp_path), record=lambda a: asked.append(a) or True)
    assert (code, len(asked), sentinel.exists()) == (tap_start.EXIT_OK, 1, False)


def test_tap_finish_takes_a_session_and_a_wait(monkeypatch):
    """pi 0.6.2 runs `tap finish --session-id <id> --wait <s>`."""
    from tap import record_client

    calls = []
    monkeypatch.setattr(record_client, "_call", lambda method, params, **kw: calls.append((method, params)) or
                        {"recording": True, "finished": True})
    assert record_client.main("finish", ["--session-id", SID, "--wait", "2.5"]) == 0
    assert calls[0][1] == {"sid": SID, "wait": 2.5}
