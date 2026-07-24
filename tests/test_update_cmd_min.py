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
