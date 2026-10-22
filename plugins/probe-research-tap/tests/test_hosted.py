"""The tap's recorder hosted by the Probe service (plan A2, D2, D3, D39).

The loop itself runs every `test_durable_runtime` test under both hosts; these
pin what only the hosted one has: its environment per thread, `stop` (finish)
against `pause` (leave for the next service), the live claim (one recorder
per session), the environment the service is given, and the fallback to
`tap watch` whenever the service cannot record.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest

from tap import config as cfg
from tap import hosted, record_client, threadenv
from tap import main as tapmain
from tap import start as tap_start
from tap.session_journal import Journal
from tests.test_durable_runtime import _hosted, setup  # noqa: F401 -- the fixture

# -- one environment per thread ---------------------------------------------------


def test_a_session_thread_sees_only_its_own_environment(monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ)
    monkeypatch.setenv("PROBE_TEST_PROCESS_ONLY", "process")
    environ = threadenv.install()
    seen = {}

    def session(name):
        with environ.session({"PROBE_TEST_NAME": name, "HOME": f"/h/{name}"}):
            os.environ["PROBE_TEST_WRITTEN"] = name
            time.sleep(0.05)  # both threads inside their sessions at once
            seen[name] = (
                os.environ.get("PROBE_TEST_NAME"),
                os.getenv("HOME"),
                os.environ.get("PROBE_TEST_WRITTEN"),
                "PROBE_TEST_PROCESS_ONLY" in os.environ,
                dict(os.environ.copy()),
            )

    threads = [threading.Thread(target=session, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen["a"][:4] == ("a", "/h/a", "a", False)
    assert seen["b"][:4] == ("b", "/h/b", "b", False)
    assert seen["a"][4] == {"PROBE_TEST_NAME": "a", "HOME": "/h/a", "PROBE_TEST_WRITTEN": "a"}
    # The process's own environment is untouched, and other threads still see it.
    assert os.environ.get("PROBE_TEST_PROCESS_ONLY") == "process"
    assert "PROBE_TEST_WRITTEN" not in os.environ
    assert threadenv.install() is environ  # once per process


# -- stop finishes, pause leaves ----------------------------------------------------


def _ticks(monkeypatch, n, then):
    count = 0

    def sleep(_seconds):
        nonlocal count
        count += 1
        if count >= n:
            then()

    monkeypatch.setattr(tapmain.time, "sleep", sleep)


@pytest.mark.parametrize("setup", ["hosted"], indirect=True)
def test_stop_finishes_the_session_with_its_finalize(setup, monkeypatch):
    config, storage, wire = setup
    _ticks(monkeypatch, 3, lambda: config.stop.set())
    assert tapmain._run_durable_loop(config, storage) == 0
    batches = [json.loads(body) for body in wire.accepted.values()]
    assert batches and batches[-1]["finalize"] is True


@pytest.mark.parametrize("setup", ["hosted"], indirect=True)
def test_pause_leaves_without_finishing_and_lets_go_of_the_session(setup, monkeypatch):
    """A service restart: no FINALIZE (the session goes on), the daemon worker
    is left running (its next supervisor asks it to hand over), and the live
    claim is released so the next service's recorder can take the session."""
    config, storage, wire = setup
    stopped = []
    monkeypatch.setattr(tapmain.Supervisor, "stop", lambda self: stopped.append(True))
    _ticks(monkeypatch, 3, lambda: config.pause.set())
    assert tapmain._run_durable_loop(config, storage) == 0
    batches = [json.loads(body) for body in wire.accepted.values()]
    assert batches, "what was there was delivered"
    assert not any(b.get("finalize") for b in batches)
    assert stopped == []
    journal = Journal(wire.base_url, "synthetic", "claude_code")
    try:
        assert journal.get(config.session_id)["finalized"] is not True
        assert journal.claim_live(config.session_id), "the paused recorder still holds the session"
    finally:
        journal.close()


@pytest.mark.parametrize("setup", ["hosted"], indirect=True)
def test_a_session_another_recorder_holds_is_left_to_it(setup, monkeypatch):
    """D39: the session's own `tap watch` (any other live recorder) still holds
    the journal's claim: the hosted loop returns at once and records nothing."""
    config, storage, wire = setup
    holder = Journal(wire.base_url, "synthetic", "claude_code")
    try:
        assert holder.claim_live(config.session_id)
        _ticks(monkeypatch, 50, lambda: config.stop.set())
        assert tapmain._run_durable_loop(config, storage) == 0
        assert wire.accepted == {}
    finally:
        holder.close()


# -- hosted.record ---------------------------------------------------------------------


def _session(tmp_path, sid="s-1", **env):
    return {
        "sid": sid, "cwd": str(tmp_path), "transcript": str(tmp_path / f"{sid}.jsonl"), "transcript_dir": None,
        "env": {"PROBE_RESEARCH_TAP_PLUGIN_DIR": str(tmp_path / "plugin"), **env},
    }


@pytest.mark.parametrize(
    ("behaviour", "outcome"),
    [("stop", hosted.ENDED), ("pause", hosted.PAUSED), ("return", hosted.STOPPED), ("raise", hosted.FAILED),
     ("exit", hosted.FAILED)],
)
def test_record_says_how_the_loop_ended(tmp_path, monkeypatch, behaviour, outcome):
    monkeypatch.setattr(os, "environ", os.environ)
    controls = hosted.Controls()
    seen = {}

    def fake_main(argv, *, hosted=None):
        assert hosted is controls
        seen["argv"] = argv
        seen["plugin_dir"] = os.environ.get("PROBE_RESEARCH_TAP_PLUGIN_DIR")
        tapmain.log.info("a line from the loop")
        if behaviour == "stop":
            controls.stop.set()
        elif behaviour == "pause":
            controls.pause.set()
        elif behaviour == "raise":
            raise RuntimeError("boom")
        elif behaviour == "exit":
            raise SystemExit(2)
        return 0

    monkeypatch.setattr(tapmain, "main", fake_main)
    result = []
    thread = threading.Thread(target=lambda: result.append(hosted.record(_session(tmp_path), controls)))
    thread.start()
    thread.join(10)
    assert result[0][0] == outcome
    assert seen["argv"][:4] == ["--session-id", "s-1", "--cwd", str(tmp_path)]
    assert seen["plugin_dir"] == str(tmp_path / "plugin")
    # Its records went to the session's own log, as `tap watch` writes them.
    assert "a line from the loop" in (tmp_path / "plugin" / "logs" / "s-1.log").read_text()


def test_two_sessions_log_to_their_own_files(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ)
    barrier = threading.Barrier(2)

    def fake_main(argv, *, hosted=None):
        sid = argv[1]
        barrier.wait(5)
        for _ in range(20):
            tapmain.log.info("line of %s", sid)
        hosted.stop.set()
        return 0

    monkeypatch.setattr(tapmain, "main", fake_main)
    threads = [
        threading.Thread(target=hosted.record, args=(_session(tmp_path, sid), hosted.Controls())) for sid in ("a", "b")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    for sid, other in (("a", "b"), ("b", "a")):
        text = (tmp_path / "plugin" / "logs" / f"{sid}.log").read_text()
        assert text.count(f"line of {sid}") == 20 and f"line of {other}" not in text


# -- what the service is given -------------------------------------------------------------


def test_the_service_is_given_only_what_a_recorder_reads():
    env = record_client.tap_env({
        "HOME": "/h", "PATH": "/bin", "PROBE_INGEST_TOKEN": "t", "CLAUDE_CONFIG_DIR": "/c", "PLUGIN_ROOT": "/r",
        "HTTPS_PROXY": "http://p", "XDG_STATE_HOME": "/s", "CODEX_HOME": "/x",
        "ANTHROPIC_API_KEY": "secret", "OPENAI_API_KEY": "secret", "AWS_SECRET_ACCESS_KEY": "secret",
        "GITHUB_TOKEN": "secret", "SSH_AUTH_SOCK": "/agent",
        # An agent's own credentials share the prefixes, and never go (they would be kept on disk).
        "CLAUDE_CODE_OAUTH_TOKEN": "secret", "CODEX_API_KEY": "secret", "KIMI_API_KEY": "secret",
        "PI_API_KEY": "secret", "CLAUDE_CODE_SESSION_ID": "s",
        # Probe's own do: the tap's token overrides, the daemon's key.
        "PRBE_CODEX_TAP_TOKEN": "t", "PROBE_DAEMON_KEY": "k",
    })
    assert set(env) == {
        "HOME", "PATH", "PROBE_INGEST_TOKEN", "CLAUDE_CONFIG_DIR", "PLUGIN_ROOT", "HTTPS_PROXY", "XDG_STATE_HOME",
        "CODEX_HOME", "CLAUDE_CODE_SESSION_ID", "PRBE_CODEX_TAP_TOKEN", "PROBE_DAEMON_KEY",
    }


def _args(tmp_path, sid="s-1"):
    return argparse.Namespace(session_id=sid, cwd=str(tmp_path), transcript=str(tmp_path / "t.jsonl"),
                              transcript_dir=None, plugin_root=str(tmp_path))


def test_record_asks_again_after_a_restart_onto_this_tap_and_names_the_service(tmp_path, monkeypatch):
    monkeypatch.setenv(record_client.HOSTED_ENV, "1")
    monkeypatch.setattr(record_client.time, "sleep", lambda _s: None)
    answers = [{"recording": False, "restart": True}, {"recording": True, "started": True, "pid": os.getpid()}]
    calls = []

    def call(method, params, *, deadline, start=False):
        calls.append((method, params, start))
        return answers.pop(0)

    monkeypatch.setattr(record_client, "_call", call)
    monkeypatch.setattr(cfg, "hosted_file", lambda sid: tmp_path / f"{sid}.hosted")
    assert record_client.record(_args(tmp_path)) == 0
    assert [c[0] for c in calls] == ["session.record", "session.record"]
    params = calls[0][1]
    assert params["sid"] == "s-1" and params["tap_version"] and params["tap_root"] == str(tmp_path)
    assert params["transcript"] == str(tmp_path / "t.jsonl") and isinstance(params["env"], dict)
    marker = tmp_path / "s-1.hosted"
    assert marker.read_text() == str(os.getpid())
    assert stat.S_IMODE(marker.stat().st_mode) == 0o600
    assert record_client.hosted_pid("s-1") is None, "a pytest process is no Probe service"


@pytest.mark.parametrize("answer", [None, {"recording": False, "reason": "no_owner"}, {"recording": False, "restart": True}])
def test_anything_but_recording_means_tap_watch(tmp_path, monkeypatch, answer):
    monkeypatch.setenv(record_client.HOSTED_ENV, "1")
    monkeypatch.setattr(record_client, "TOTAL_S", 0.2)
    monkeypatch.setattr(record_client, "_call", lambda *a, **k: answer)
    monkeypatch.setattr(cfg, "hosted_file", lambda sid: tmp_path / f"{sid}.hosted")
    assert record_client.record(_args(tmp_path)) == 1
    assert not (tmp_path / "s-1.hosted").exists()


def test_switched_off_never_asks(tmp_path, monkeypatch):
    monkeypatch.setenv(record_client.HOSTED_ENV, "0")
    monkeypatch.setattr(record_client, "_call", lambda *a, **k: pytest.fail("asked the service"))
    assert record_client.record(_args(tmp_path)) == 1


def test_tap_start_lets_the_service_record_before_spawning(tmp_path, monkeypatch):
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("")
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "plugin"))
    monkeypatch.setenv("PROBE_INGEST_TOKEN", "t")
    monkeypatch.setattr(cfg, "pid_file", lambda sid: tmp_path / f"{sid}.pid")
    monkeypatch.setattr(cfg, "shutdown_sentinel", lambda sid: tmp_path / f"{sid}.shutdown")
    monkeypatch.setattr(tap_start, "_hosted", lambda sid: None)
    spawned, asked = [], []
    argv = ["--session-id", "s-1", "--cwd", str(tmp_path), "--transcript", str(transcript)]
    assert tap_start.main(argv, spawn=spawned.append, record=lambda a: asked.append(a) or True) == 0
    assert asked and asked[0].session_id == "s-1" and spawned == []
    assert tap_start.main(argv, spawn=spawned.append, record=lambda a: False) == 0
    assert len(spawned) == 1, "the service could not: the wrapper, as before"


def test_a_hosted_session_reads_as_running(tmp_path, monkeypatch):
    monkeypatch.setattr(cfg, "pid_file", lambda sid: tmp_path / f"{sid}.pid")
    monkeypatch.setattr(tap_start, "_hosted", lambda sid: 4242 if sid == "s-1" else None)
    assert tap_start.daemon_state("s-1") == ("running", 4242)
    assert tap_start.daemon_state("s-2") == ("none", None)
    assert isinstance(Path, type)



def test_each_session_thread_gets_its_own_tls_trust(tmp_path, monkeypatch):
    """OpenSSL reads SSL_CERT_FILE from the process's C environment, which a
    hosted session's thread does not change: the tap builds its context from
    the thread's own (critic R2: uploads behind a TLS-intercepting proxy)."""
    import ssl

    from tap import httpclient
    from tap import session_journal

    paths = ssl.get_default_verify_paths()
    found = [c for c in (paths.cafile, paths.openssl_cafile, "/etc/ssl/certs/ca-certificates.crt", "/etc/ssl/cert.pem",
                         "/etc/pki/tls/certs/ca-bundle.crt") if c and Path(c).is_file()]
    if not found:
        pytest.skip("no CA bundle to cut one certificate from")
    text = Path(found[0]).read_text(encoding="utf-8", errors="replace")
    first = text.split("-----END CERTIFICATE-----")[0] + "-----END CERTIFICATE-----\n"
    one = tmp_path / "one.pem"
    one.write_text(first[first.index("-----BEGIN CERTIFICATE-----"):], encoding="utf-8")
    monkeypatch.setattr(os, "environ", os.environ)
    environ = threadenv.install()
    seen = {}

    def session(name, env):
        with environ.session(env):
            seen[name] = (len(httpclient.tls_context().get_ca_certs()),
                          len(session_journal._env_context().get_ca_certs()))

    threads = [threading.Thread(target=session, args=("custom", {"SSL_CERT_FILE": str(one)})),
               threading.Thread(target=session, args=("plain", {}))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen["custom"] == (1, 1)
    assert seen["plain"] != (1, 1)


def test_an_ended_session_leaves_no_marker_naming_this_recorder(tmp_path, monkeypatch):
    """A resume of it records again (pi's `tap start` read a stale marker as
    "running", Codex R2)."""
    monkeypatch.setattr(os, "environ", os.environ)
    marker = tmp_path / "s-1.hosted"
    marker.write_text(str(os.getpid()))
    monkeypatch.setattr(cfg, "hosted_file", lambda sid: marker)

    def fake_main(argv, *, hosted=None):
        hosted.stop.set()
        return 0

    monkeypatch.setattr(tapmain, "main", fake_main)
    out = []
    t = threading.Thread(target=lambda: out.append(hosted.record(_session(tmp_path), hosted.Controls())))
    t.start()
    t.join(10)
    assert out[0][0] == hosted.ENDED and not marker.exists()


def test_each_session_thread_reaches_the_network_through_its_own_proxy(monkeypatch):
    """urllib's default opener caches the first caller's proxies for the process;
    the tap passes a context, so each request builds its opener (and proxy
    handler) from its own thread's environment."""
    import urllib.request

    monkeypatch.setattr(os, "environ", os.environ)
    environ = threadenv.install()
    seen = {}

    def session(name, proxy):
        with environ.session({"HTTPS_PROXY": proxy}):
            handler = next(h for h in urllib.request.build_opener().handlers
                           if isinstance(h, urllib.request.ProxyHandler))
            seen[name] = handler.proxies.get("https")

    threads = [threading.Thread(target=session, args=(n, f"http://{n}.proxy:3128")) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert seen == {"a": "http://a.proxy:3128", "b": "http://b.proxy:3128"}


def test_a_resumed_recorder_names_its_own_service_in_the_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "environ", os.environ)
    marker = tmp_path / "s-1.hosted"
    marker.write_text("12345")  # the last service
    monkeypatch.setattr(cfg, "hosted_file", lambda sid: marker)
    seen = []

    def fake_main(argv, *, hosted=None):
        seen.append(marker.read_text())
        hosted.pause.set()
        return 0

    monkeypatch.setattr(tapmain, "main", fake_main)
    t = threading.Thread(target=hosted.record, args=(_session(tmp_path), hosted.Controls()))
    t.start()
    t.join(10)
    assert seen == [str(os.getpid())]
