"""Killer-side forensics: the TERM-arrival anchor and its status surface.

The tap's third failure mode is a daemon found dead with its pid file unlinked
and NO shutdown sentinel — the signature of the probe CLI's _stop_daemon(),
with no caller ever caught. macOS cannot expose the signal sender to the dying
process, so attribution is correlation: the killer journals every SIGTERM it
sends into <plugin_dir>/logs/stop-daemon.jsonl, and the daemon logs the
arrival timestamp to its session log. `python -m tap status` prints the newest
journal entry so a "transcripts missing" report carries its own cause — and a
TERM with no matching journal entry exonerates the stop path, which is itself
the answer.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_plugin_dir(monkeypatch, tmp_path: Path):
    """Fresh plugin dir per test so nothing touches real state (same pattern as
    test_lifecycle.py)."""
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path))
    monkeypatch.delenv("PROBE_TAP_SOURCE", raising=False)
    yield tmp_path


# ---------------------------------------------------------------------------
# the daemon's TERM-arrival anchor
# ---------------------------------------------------------------------------


def _with_installed_handlers(fn):
    """Install the daemon's handlers, run fn, restore the previous handlers and
    the module's shutdown flag — the test process must not keep them."""
    from tap import main as tap_main

    prev_term = signal.getsignal(signal.SIGTERM)
    prev_int = signal.getsignal(signal.SIGINT)
    tap_main._shutdown_requested = False
    try:
        tap_main._install_signal_handlers()
        return fn(tap_main)
    finally:
        signal.signal(signal.SIGTERM, prev_term)
        signal.signal(signal.SIGINT, prev_int)
        tap_main._shutdown_requested = False


def test_sigterm_arrival_is_logged_with_a_timestamp(caplog) -> None:
    """The correlation anchor: TERM arrival lands in the session log with an
    epoch timestamp and the daemon's own pid, before any shutdown work."""

    def scenario(tap_main):
        with caplog.at_level(logging.INFO, logger="probe-research-tap"):
            os.kill(os.getpid(), signal.SIGTERM)
            # CPython delivers at the next bytecode boundary; give it one.
            time.sleep(0.05)
        assert tap_main._shutdown_requested is True

    _with_installed_handlers(scenario)
    messages = [record.getMessage() for record in caplog.records]
    anchors = [m for m in messages if "signal SIGTERM received" in m]
    assert anchors, f"no TERM-arrival anchor logged; saw {messages!r}"
    assert "unix=" in anchors[0]
    assert f"pid={os.getpid()}" in anchors[0]


def test_sigint_gets_the_same_anchor(caplog) -> None:
    def scenario(tap_main):
        with caplog.at_level(logging.INFO, logger="probe-research-tap"):
            os.kill(os.getpid(), signal.SIGINT)
            time.sleep(0.05)
        assert tap_main._shutdown_requested is True

    _with_installed_handlers(scenario)
    assert any("signal SIGINT received" in r.getMessage() for r in caplog.records)


def test_a_broken_logger_never_breaks_shutdown(caplog, monkeypatch) -> None:
    """The anchor is best-effort: shutdown must still be requested when the
    logging call itself raises."""
    from tap import main as tap_main

    def _boom(*_args, **_kwargs):
        raise RuntimeError("handler closed")

    monkeypatch.setattr(tap_main.log, "info", _boom)

    def scenario(tap_main_mod):
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(0.05)
        assert tap_main_mod._shutdown_requested is True

    _with_installed_handlers(scenario)


# ---------------------------------------------------------------------------
# `python -m tap status` surfaces the newest stop-journal entry
# ---------------------------------------------------------------------------


def _configure_status_env(monkeypatch) -> None:
    monkeypatch.setenv("PROBE_INGEST_TOKEN", "tok_forensics")
    monkeypatch.setenv("PROBE_BASE_URL", "https://api.invalid")


def test_status_surfaces_the_last_stop_event(monkeypatch, tmp_path: Path, capsys) -> None:
    from tap import status

    _configure_status_env(monkeypatch)
    journal = tmp_path / "logs" / "stop-daemon.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps({"ts": int(time.time()) - 7200, "pid": 1, "argv": ["old"], "signalled": []}),
        json.dumps(
            {
                "ts": int(time.time()) - 120,
                "pid": 4242,
                "argv": ["probe", "wizard"],
                "signalled": [{"pid": 77, "pid_file": "/tmp/probe-research-tap-watcher-x.pid"}],
            }
        ),
        "not json at all",  # a torn write must not hide the good entry above
    ]
    journal.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert status.run() == 0
    out = capsys.readouterr().out
    assert "last stop:" in out
    assert "`probe wizard` (pid 4242)" in out
    assert "2 minutes ago" in out
    assert "old" not in out, "must surface the newest parseable entry"


def test_status_stays_quiet_when_nothing_was_ever_stopped(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    from tap import status

    _configure_status_env(monkeypatch)
    assert status.run() == 0
    assert "last stop" not in capsys.readouterr().out
