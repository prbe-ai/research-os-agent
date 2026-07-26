"""H9 guard: the SessionStart hook's `UPDATE_CMD_MIN_CLI` must never be newer than
the CLI version this repo actually ships `probe update` in.

The hook nudges "run `probe update`" when the installed CLI >= UPDATE_CMD_MIN_CLI.
If that constant is set ahead of the CLI release that contains the command, the
nudge points people at a command their CLI doesn't have (`No such command 'update'`).
This test fails CI on that drift — the load-bearing half of the version-gate.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import probe
from probe.cli import updater


def _load_hook():
    path = (
        Path(__file__).resolve().parents[1]
        / "plugins" / "probe-research" / "hooks" / "version_check.py"
    )
    spec = importlib.util.spec_from_file_location("_version_check_under_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_update_cmd_min_not_ahead_of_shipped_cli():
    vc = _load_hook()
    assert not updater.is_newer(vc.UPDATE_CMD_MIN_CLI, probe.__version__), (
        f"UPDATE_CMD_MIN_CLI={vc.UPDATE_CMD_MIN_CLI!r} is newer than this tree's CLI "
        f"version ({probe.__version__!r}); the hook would nudge users to `probe update` "
        f"before the released CLI has it. Set UPDATE_CMD_MIN_CLI to the release that "
        f"introduces the command (and cut that CLI release)."
    )


def test_probe_update_actually_runs(monkeypatch, tmp_path):
    """`probe update` is what the plugin's SessionStart hook spawns, so a
    signature drift here breaks auto-update on every machine and reports
    nothing: the hook runs detached, with no terminal to print a traceback to.

    Nothing else executes this command's body — a `confirm=None` argument
    outlived the parameter it was passed to and every test still went green.
    """
    from typer.testing import CliRunner

    from probe.cli import updater
    from probe.cli.main import app

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(updater, "fetch_latest", lambda base: {})
    monkeypatch.setattr(
        updater,
        "detect_install",
        lambda: updater.Install(method=updater.Method.EDITABLE),
    )
    monkeypatch.setattr(
        updater,
        "update_plugin",
        lambda target: updater.PluginResult(
            attempted=True,
            confirmed=True,
            changed=False,
            before=None,
            after=None,
            message="already current",
        ),
    )

    result = CliRunner().invoke(app, ["update"])
    assert result.exit_code == 0, result.output + repr(result.exception)
    assert "TypeError" not in result.output
