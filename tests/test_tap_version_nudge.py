"""The SessionStart hook must nudge a stale transcript tap.

The tap was the one component the staleness check did not cover, and it is the
one where staleness is invisible: a stale tap still authenticates, still links
sessions to projects, and simply never delivers a transcript. That is exactly
how 0.1.2 behaved — daemons died seconds after SessionStart — and the only
evidence was a line in a local log file. Every other signal stayed green.

Covered here:
  * a stale tap produces a nudge naming it;
  * a user WITHOUT the tap is never nudged about it (absence is not staleness);
  * the recommended fallback commands actually update the tap when it is the
    stale component, rather than naming it and handing over commands that
    cannot fix it.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_HOOK = (
    Path(__file__).resolve().parents[1]
    / "plugins" / "probe-research" / "hooks" / "version_check.py"
)


def _load_hook():
    """Load the hook the way session-start.sh runs it (its dir on sys.path)."""
    spec = importlib.util.spec_from_file_location("_version_check_tap_test", _HOOK)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    hooks_dir = str(_HOOK.parent)
    added = hooks_dir not in sys.path
    if added:
        sys.path.insert(0, hooks_dir)
    try:
        spec.loader.exec_module(module)
    finally:
        if added:
            sys.path.remove(hooks_dir)
    return module


@pytest.fixture
def hook():
    return _load_hook()


def _tap_dir(tmp_path: Path, version: str | None) -> Path:
    d = tmp_path / "probe-research-tap"
    d.mkdir(parents=True, exist_ok=True)
    if version is not None:
        (d / ".installed_version").write_text(version, encoding="utf-8")
    return d


def test_local_tap_reads_the_installed_version(hook, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "PROBE_RESEARCH_TAP_PLUGIN_DIR", str(_tap_dir(tmp_path, "0.1.2"))
    )
    assert hook._local_tap() == "0.1.2"


def test_local_tap_tolerates_trailing_whitespace(hook, tmp_path, monkeypatch) -> None:
    """session-start.sh writes with printf '%s', but a hand-edited or
    editor-touched file gains a newline. A version with a stray newline must not
    read as a different version."""
    d = _tap_dir(tmp_path, None)
    (d / ".installed_version").write_text("0.1.3\n", encoding="utf-8")
    monkeypatch.setenv("PROBE_RESEARCH_TAP_PLUGIN_DIR", str(d))
    assert hook._local_tap() == "0.1.3"


def test_no_tap_installed_reads_as_unknown_not_stale(hook, tmp_path, monkeypatch) -> None:
    """The tap is optional. A missing state dir must be None so main() skips it —
    nudging someone to update a plugin they never installed is noise."""
    monkeypatch.setenv(
        "PROBE_RESEARCH_TAP_PLUGIN_DIR", str(tmp_path / "does-not-exist")
    )
    assert hook._local_tap() is None


def test_manifest_carries_a_tap_entry() -> None:
    """Without this the hook has nothing to compare against and the tap is
    silently exempt from staleness checks — the gap this suite exists to close."""
    manifest = json.loads(
        (Path(__file__).resolve().parents[1] / "client-version.json").read_text()
    )
    assert "tap" in manifest, "client-version.json has no tap entry"
    assert manifest["tap"].get("latest"), "tap.latest is empty"


_MANIFEST = {
    "cli": {"latest": "9.9.9", "min": "0.6.0"},
    "plugin": {"latest": "9.9.9", "min": "0.6.0"},
    "tap": {"latest": "0.1.3", "min": "0.1.0"},
    "advisory": None,
}


def _drive(hook, monkeypatch, capsys, *, tap_version, cli="9.9.9", plugin="9.9.9",
           manifest=None):
    """Run the hook's main() against a stubbed manifest and capture its output.

    Drives the REAL decision path rather than grepping the source, so a
    component wired into `local` but forgotten in the comparison loop (or vice
    versa) is caught.
    """
    manifest = _MANIFEST if manifest is None else manifest
    monkeypatch.setattr(hook.version_policy, "read_cache",
                        lambda *a, **k: (manifest, 10.0**12, True))
    monkeypatch.setattr(hook.version_policy, "cache_is_fresh", lambda *a, **k: True)
    monkeypatch.setattr(hook, "_local_cli", lambda *_: cli)
    monkeypatch.setattr(hook, "_local_plugin", lambda *_: plugin)
    monkeypatch.setattr(hook, "_local_tap", lambda: tap_version)
    monkeypatch.setattr(hook, "_spawn_autoupdate", lambda *_: None)
    with pytest.raises(SystemExit):
        hook.main()
    return json.loads(capsys.readouterr().out)


def test_a_stale_tap_is_reported_in_the_session_message(hook, monkeypatch, capsys) -> None:
    """THE guard: 0.1.2 installed against 0.1.3 latest must produce a nudge that
    names the tap. This is the case that shipped silently."""
    out = _drive(hook, monkeypatch, capsys, tap_version="0.1.2")
    msg = out.get("systemMessage", "")
    assert "transcript tap" in msg, f"stale tap not reported: {out!r}"
    assert "0.1.2" in msg and "0.1.3" in msg, f"versions missing from nudge: {msg!r}"


def test_a_current_tap_produces_no_nudge(hook, monkeypatch, capsys) -> None:
    out = _drive(hook, monkeypatch, capsys, tap_version="0.1.3")
    assert out == {"continue": True}, f"expected silence, got {out!r}"


def test_a_user_without_the_tap_is_never_nudged_about_it(hook, monkeypatch, capsys) -> None:
    """Absence is not staleness — the tap is optional."""
    out = _drive(hook, monkeypatch, capsys, tap_version=None)
    assert out == {"continue": True}, f"nudged a user with no tap: {out!r}"


def test_a_manifest_without_a_tap_entry_disables_only_the_tap(hook, monkeypatch, capsys) -> None:
    """An older manifest (or a malformed field) must not break the CLI/plugin
    nudges — each key fails independently."""
    manifest = {k: v for k, v in _MANIFEST.items() if k != "tap"}
    manifest["plugin"] = {"latest": "9.9.9", "min": "0.6.0"}
    out = _drive(hook, monkeypatch, capsys, tap_version="0.1.2",
                 plugin="0.1.0", manifest=manifest)
    msg = out.get("systemMessage", "")
    assert "transcript tap" not in msg
    assert "plugin" in msg, "an absent tap entry broke the other nudges"


def test_fallback_commands_update_the_tap_when_it_is_stale(hook, monkeypatch, capsys) -> None:
    """An older CLI gets a raw command sequence instead of `probe update`. That
    sequence updated only probe-research, so a tap-staleness nudge would name a
    component and then hand over commands that cannot fix it."""
    out = _drive(hook, monkeypatch, capsys, tap_version="0.1.2", cli="0.7.0")
    msg = out.get("systemMessage", "")
    assert "probe update" not in msg.split("Update:")[1].split("(restart")[0].strip()[:13], (
        "expected the raw sequence for a pre-`probe update` CLI"
    )
    assert "claude plugin update probe-research-tap@research-os-agent" in msg, (
        f"fallback commands cannot fix the tap they just named: {msg!r}"
    )


def test_probe_update_actually_issues_the_tap_update(monkeypatch) -> None:
    """`probe update` is the command the nudge prefers for current CLIs, so it
    has to update every component the nudge can name.

    Asserts on the COMMANDS ISSUED. An earlier version of this test grepped the
    source for `TAP_PLUGIN_ID` after `def update_plugin` — which passed with the
    call deleted, because `manual_plugin_commands()` further down the file also
    names the constant. It certified nothing.
    """
    from probe.cli import claude_cli, updater

    issued: list[list[str]] = []

    def _fake_run(args, *, timeout):
        issued.append(list(args))
        return claude_cli.Result(ok=True)

    monkeypatch.setattr(updater.claude_cli, "run", _fake_run)
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr(updater, "installed_plugin_version", lambda: "9.9.9")

    updater.update_plugin("9.9.9")

    assert ["plugin", "update", updater.TAP_PLUGIN_ID] in issued, (
        "update_plugin() never issued the tap update, so `probe update` reports "
        f"success while leaving a stale tap in place. Issued: {issued!r}"
    )


def test_a_missing_tap_does_not_mark_the_update_incomplete(monkeypatch) -> None:
    """Most users have the CLI and plugin but no tap, so `claude` failing on the
    tap means "not installed" far more often than "update broken". That failure
    must not flip the run's `completed` flag.

    The installed version is stubbed BELOW the target on purpose. Stub it AT the
    target and update_plugin returns confirmed on the `at_target` branch before
    `completed` is ever read — so the test passes whether or not a tap failure
    is treated as fatal, which is exactly how an earlier version of this test
    survived its own mutant.
    """
    from probe.cli import claude_cli, updater

    def _fake_run(args, *, timeout):
        ok = updater.TAP_PLUGIN_ID not in args  # the tap is "not installed"
        return claude_cli.Result(ok=ok, detail="" if ok else "not installed")

    monkeypatch.setattr(updater.claude_cli, "run", _fake_run)
    monkeypatch.setattr(updater.shutil, "which", lambda _: "/usr/local/bin/claude")
    monkeypatch.setattr(updater, "installed_plugin_version", lambda: "1.0.0")

    result = updater.update_plugin("9.9.9")
    assert "did not complete" not in result.message, (
        "a `claude` failure on the OPTIONAL tap was reported as the whole plugin "
        f"update failing: {result.message!r}"
    )


def test_manual_commands_mention_the_tap() -> None:
    from probe.cli import updater

    assert updater.TAP_PLUGIN_ID == "probe-research-tap@research-os-agent"
    assert updater.TAP_PLUGIN_ID in updater.manual_plugin_commands()
