"""The CLI's every-command version check: the gate chain and the state it writes.

Two properties matter more than the individual branches:

  1. The invoking process NEVER makes a network call. An inline fetch would put a
     periodic multi-second stall inside training loops.
  2. The gate order stays cheapest-first, so a `probe log` on an install that
     never opted in touches one small file and stops.

Both are asserted directly rather than inferred from behaviour.
"""

from __future__ import annotations

import importlib
import json
import time

import pytest

from probe import version_policy
from probe.cli import autoupdate

# NOT `from probe.cli import main`: probe/cli/__init__.py binds `main` to its own
# entry-point FUNCTION, which shadows the submodule of the same name.
cli_main = importlib.import_module("probe.cli.main")


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Isolate state, cache and config.

    Sets BOTH PROBE_CONFIG_PATH and XDG_CONFIG_HOME: the surfaces that resolve
    probe config disagree about which wins, and they only agree in production, so
    a fixture setting one can write to the developer's real config.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("PROBE_CONFIG_PATH", str(tmp_path / "config" / "probe" / "config.json"))
    monkeypatch.setenv("PROBE_BASE_URL", "http://127.0.0.1:9")
    return tmp_path


@pytest.fixture
def spawns(monkeypatch):
    """Record spawns instead of performing them."""
    calls: list[list[str]] = []
    monkeypatch.setattr(cli_main, "_spawn_version_refresh", lambda: calls.append(["refresh"]))
    monkeypatch.setattr(cli_main, "_spawn_autoupdate", lambda: calls.append(["apply"]))
    return calls


@pytest.fixture
def no_network(monkeypatch):
    """Any network call from the invoking process is a hard failure."""

    def _forbidden(*_args, **_kwargs):
        raise AssertionError(
            "the invoking CLI process fetched inline — this is what puts a "
            "multi-second stall inside a training loop"
        )

    monkeypatch.setattr(version_policy, "fetch", _forbidden)
    return monkeypatch


def _enable(enabled: bool = True) -> None:
    autoupdate.save(enabled=enabled)


def _cache(latest: str, *, age: int = 0, ok: bool = True) -> None:
    version_policy.write_cache({"cli": {"latest": latest, "min": "0.0.1"}}, ok)
    path = version_policy.cache_path()
    data = json.loads(path.read_text())
    data["fetched_at"] = int(time.time()) - age
    path.write_text(json.dumps(data))


def _as_tty(monkeypatch, value: bool = True) -> None:
    monkeypatch.setattr(cli_main.sys.stdout, "isatty", lambda: value)


def _argv(monkeypatch, command: str) -> None:
    monkeypatch.setattr(cli_main.sys, "argv", ["probe", command])


# ---------------------------------------------------------------------------
# The fast path.
# ---------------------------------------------------------------------------


def test_disabled_does_nothing_at_all(spawns, no_network):
    _enable(False)
    _cache("99.9.9", age=99999)
    cli_main._version_notice()
    assert spawns == [], "an install that never opted in must not spawn anything"


def test_a_fresh_cache_makes_no_network_call_and_no_refresh(spawns, no_network, capsys):
    _enable()
    _cache("0.0.1", age=10)  # fresh AND not newer
    cli_main._version_notice()
    assert spawns == []
    assert capsys.readouterr().err == ""


def test_a_stale_cache_spawns_a_detached_refresh_and_never_fetches(spawns, no_network):
    _enable()
    _cache("0.0.1", age=version_policy.TTL + 60)
    cli_main._version_notice()
    assert ["refresh"] in spawns


def test_a_cold_cache_is_silent_until_the_refresh_lands(spawns, no_network, capsys):
    """update-notifier behaves the same way: the first run has nothing to compare
    against, so the nudge appears on the next one."""
    _enable()
    cli_main._version_notice()
    assert ["refresh"] in spawns
    assert capsys.readouterr().err == "", "nothing to say without a manifest"


def test_a_failed_fetch_uses_backoff_not_ttl(spawns, no_network):
    """A machine that cannot reach the API must not retry every 15 minutes."""
    _enable()
    _cache("99.9.9", age=version_policy.TTL + 60, ok=False)
    cli_main._version_notice()
    assert ["refresh"] not in spawns, "within BACKOFF, a failed fetch must not retry"


# ---------------------------------------------------------------------------
# The apply gate.
# ---------------------------------------------------------------------------


def test_not_a_tty_nudges_but_never_applies(spawns, no_network, monkeypatch, capsys):
    _enable()
    _cache("99.9.9")
    _as_tty(monkeypatch, False)
    _argv(monkeypatch, "ls")
    cli_main._version_notice()
    assert ["apply"] not in spawns
    assert "99.9.9" in capsys.readouterr().err, "a nudge is still useful in CI logs"
    assert autoupdate.load().last_skip.reason == autoupdate.SKIP_NOT_A_TTY


@pytest.mark.parametrize("command", sorted(cli_main._UPDATE_HOT_PATH_COMMANDS))
def test_no_hot_path_command_ever_applies_even_on_a_tty(
    command, spawns, no_network, monkeypatch
):
    """The TTY test is holed by inheritance: a training script launched from a
    terminal hands its TTY to `probe log`, so isatty() is True in exactly the
    case where an upgrade is most destructive."""
    _enable()
    _cache("99.9.9")
    _as_tty(monkeypatch, True)
    _argv(monkeypatch, command)
    cli_main._version_notice()
    assert ["apply"] not in spawns
    assert autoupdate.load().last_skip.reason == autoupdate.SKIP_HOT_PATH_COMMAND


def test_a_live_run_blocks_the_upgrade_from_an_unrelated_command(
    spawns, no_network, monkeypatch
):
    """THE scenario the denylist alone cannot cover: a training run in one shell,
    `probe ls` typed in another. `ls` is not hot-path and its stdout is a
    terminal, so only a cross-process lock closes this."""
    from probe.cli import run_lock

    _enable()
    _cache("99.9.9")
    _as_tty(monkeypatch, True)
    _argv(monkeypatch, "ls")

    lock = run_lock.acquire("training-run")
    try:
        cli_main._version_notice()
        assert ["apply"] not in spawns
        assert autoupdate.load().last_skip.reason == autoupdate.SKIP_RUN_IN_FLIGHT
    finally:
        lock.release()


def test_an_interactive_command_with_no_run_applies(spawns, no_network, monkeypatch):
    _enable()
    _cache("99.9.9")
    _as_tty(monkeypatch, True)
    _argv(monkeypatch, "ls")
    cli_main._version_notice()
    assert ["apply"] in spawns


def test_an_up_to_date_cli_never_applies(spawns, no_network, monkeypatch):
    _enable()
    _cache("0.0.1")
    _as_tty(monkeypatch, True)
    _argv(monkeypatch, "ls")
    cli_main._version_notice()
    assert spawns == []


def test_the_check_never_raises_into_the_command(monkeypatch, spawns):
    """This runs before every command. A broken version check that propagates
    would break `probe log` inside somebody's training loop."""

    def _boom():
        raise RuntimeError("state is on fire")

    monkeypatch.setattr(version_policy, "autoupdate_enabled", _boom)
    cli_main._version_notice()  # must not raise


# ---------------------------------------------------------------------------
# The skip record. What makes "correctly idle" distinguishable from "dead".
# ---------------------------------------------------------------------------


def test_consecutive_skips_for_one_reason_accumulate(isolate):
    autoupdate.record_skip(autoupdate.SKIP_RUN_IN_FLIGHT, available="9.9.9")
    autoupdate.record_skip(autoupdate.SKIP_RUN_IN_FLIGHT, available="9.9.9")
    autoupdate.record_skip(autoupdate.SKIP_RUN_IN_FLIGHT, available="9.9.9")
    skip = autoupdate.load().last_skip
    assert skip.count == 3
    assert "3x" in skip.describe()


def test_a_different_reason_restarts_the_count(isolate):
    autoupdate.record_skip(autoupdate.SKIP_RUN_IN_FLIGHT)
    autoupdate.record_skip(autoupdate.SKIP_RUN_IN_FLIGHT)
    autoupdate.record_skip(autoupdate.SKIP_NOT_A_TTY)
    skip = autoupdate.load().last_skip
    assert skip.count == 1
    assert skip.reason == autoupdate.SKIP_NOT_A_TTY


def test_a_skip_never_overwrites_the_last_real_attempt(isolate):
    """They answer different questions and doctor prints both. Without this, a
    fortnight of correct deferrals erases the record of the last real upgrade."""
    autoupdate.record_attempt(
        autoupdate.Attempt(at=1, ok=True, from_version="1.0.0", to_version="1.1.0")
    )
    autoupdate.record_skip(autoupdate.SKIP_RUN_IN_FLIGHT)
    settings = autoupdate.load()
    assert settings.last_attempt is not None
    assert settings.last_attempt.to_version == "1.1.0"
    assert settings.last_skip is not None


def test_a_real_attempt_clears_a_stale_skip(isolate):
    """A week-old 'skipped, run in flight' printed beside a fresh success would
    read as though we were still blocked."""
    autoupdate.record_skip(autoupdate.SKIP_RUN_IN_FLIGHT)
    autoupdate.record_attempt(autoupdate.Attempt(at=2, ok=True, to_version="1.2.0"))
    assert autoupdate.load().last_skip is None


def test_enabling_autoupdate_preserves_an_existing_skip(isolate):
    """save() is a read-modify-write over the same file. It must not drop a
    sibling key it does not know about."""
    autoupdate.record_skip(autoupdate.SKIP_RUN_IN_FLIGHT)
    autoupdate.save(enabled=True)
    assert autoupdate.load().last_skip is not None


def test_record_skip_never_raises(isolate, monkeypatch):
    monkeypatch.setattr(
        version_policy, "atomic_write_json", lambda *a, **k: (_ for _ in ()).throw(OSError)
    )
    autoupdate.record_skip(autoupdate.SKIP_RUN_IN_FLIGHT)  # must not raise


# ---------------------------------------------------------------------------
# Cross-version compatibility. The plugin and the CLI update independently, so a
# new writer meeting an old reader is the normal case, not the edge case.
# ---------------------------------------------------------------------------


def test_an_unknown_key_written_by_a_newer_client_is_ignored(isolate):
    """Additive-only, unknown keys ignored. A reader that raised here would turn
    a valid state file into 'auto-update is off'."""
    autoupdate.save(enabled=True)
    raw = json.loads(autoupdate.state_path().read_text())
    raw["some_field_from_the_future"] = {"nested": [1, 2, 3]}
    autoupdate.state_path().write_text(json.dumps(raw))
    assert autoupdate.load().enabled is True


def test_a_skip_record_without_a_count_defaults_to_one(isolate):
    """Missing keys default to the reading that preserves old behaviour."""
    autoupdate.save(enabled=True)
    raw = json.loads(autoupdate.state_path().read_text())
    raw["last_skip"] = {"at": 123, "reason": "run in flight"}  # pre-count shape
    autoupdate.state_path().write_text(json.dumps(raw))
    assert autoupdate.load().last_skip.count == 1


def test_a_cache_with_extra_fields_still_reads(isolate):
    """The old plugin reading a cache written by a newer CLI. Raising here means
    refetching every session AND applying the failure backoff to a good cache."""
    version_policy.write_cache({"cli": {"latest": "1.0.0"}}, True)
    path = version_policy.cache_path()
    data = json.loads(path.read_text())
    data["future_field"] = "whatever"
    path.write_text(json.dumps(data))
    manifest, _, ok = version_policy.read_cache()
    assert manifest == {"cli": {"latest": "1.0.0"}}
    assert ok is True


def test_a_corrupt_cache_reads_as_empty_rather_than_raising(isolate):
    version_policy.cache_path().parent.mkdir(parents=True, exist_ok=True)
    version_policy.cache_path().write_text("{ this is not json")
    manifest, fetched_at, ok = version_policy.read_cache()
    assert (manifest, fetched_at, ok) == (None, 0.0, False)


def test_a_failed_write_does_not_leave_a_temp_file_behind(isolate, monkeypatch):
    target = isolate / "state" / "probe" / "autoupdate.json"
    target.parent.mkdir(parents=True, exist_ok=True)

    class _Unserializable:
        pass

    assert version_policy.atomic_write_json(target, {"bad": _Unserializable()}) is False
    leftovers = list(target.parent.glob("*.tmp"))
    assert leftovers == [], f"a temp file outlived its writer: {leftovers}"


def test_concurrent_writers_do_not_share_a_temp_filename(isolate):
    """The old fixed `autoupdate.json.tmp` was shared by every writer, so two
    racing through it could interleave a partial write — and a truncated state
    file reads as 'auto-update is off'."""
    target = isolate / "state" / "probe" / "autoupdate.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    real_replace = version_policy.os.replace

    def _capture(src, dst):
        seen.add(str(src))
        return real_replace(src, dst)

    version_policy.os.replace = _capture
    try:
        for index in range(5):
            version_policy.atomic_write_json(target, {"n": index})
    finally:
        version_policy.os.replace = real_replace
    assert len(seen) == 5, f"temp names collided: {seen}"


# ---------------------------------------------------------------------------
# Single-flight.
# ---------------------------------------------------------------------------


def test_only_one_of_many_concurrent_invocations_claims_the_refresh(isolate):
    """A sweep launching eight runs at once finds eight stale caches. Without
    this they all fetch the same 150-byte document."""
    claims = [version_policy.claim_refresh() for _ in range(8)]
    assert claims.count(True) == 1


def test_an_abandoned_claim_ages_out(isolate, monkeypatch):
    """A killed refresher must not suppress refreshes forever."""
    assert version_policy.claim_refresh() is True
    assert version_policy.claim_refresh() is False
    future = time.time() + version_policy.REFRESH_CLAIM_SECONDS + 5
    monkeypatch.setattr(version_policy.time, "time", lambda: future)
    assert version_policy.claim_refresh() is True


def test_releasing_a_claim_lets_the_next_caller_through(isolate):
    assert version_policy.claim_refresh() is True
    version_policy.release_refresh()
    assert version_policy.claim_refresh() is True
