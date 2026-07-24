"""Tests for `probe update` internals (cli/updater.py) + the command's --check codes.

Covers the hardening the eng review demanded: install detection from the running
package path (H4), the legacy probe-agent dance (H3), editable/managed guards
(H5/H6), the plugin post-condition + non-TTY spawn (H1/H2), and --check exit
codes distinct from main()'s 1/2 (H7).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import probe
from probe import cli
from probe.cli import updater


# -- version compare --------------------------------------------------------
@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("0.10.0", "0.9.0", True),   # NOT a string compare
        ("0.9.0", "0.10.0", False),
        ("0.7.0", "0.7.0", False),
        ("0.8.0", "0.8", False),     # 0.8 normalizes to 0.8.0
        (None, "0.7.0", False),
        ("0.7.0", None, False),
    ],
)
def test_is_newer(a, b, expected):
    assert updater.is_newer(a, b) is expected


# -- install detection (H4/H3) ---------------------------------------------
def _patch_pkg(monkeypatch, path: str):
    monkeypatch.setattr(updater, "_probe_pkg_dir", lambda: Path(path))


def test_detect_uv_tool(monkeypatch):
    _patch_pkg(monkeypatch, "/home/u/.local/share/uv/tools/probe-research/lib/python3.12/site-packages/probe")
    assert updater.detect_install().method == updater.Method.UV_TOOL


def test_detect_uv_tool_legacy(monkeypatch):
    _patch_pkg(monkeypatch, "/home/u/.local/share/uv/tools/probe-agent/lib/python3.12/site-packages/probe")
    assert updater.detect_install().method == updater.Method.UV_TOOL_LEGACY


def test_detect_pipx(monkeypatch):
    _patch_pkg(monkeypatch, "/home/u/.local/share/pipx/venvs/probe-research/lib/python3.12/site-packages/probe")
    assert updater.detect_install().method == updater.Method.PIPX


def test_detect_editable_source_tree(monkeypatch):
    _patch_pkg(monkeypatch, "/home/u/dev/research-os-agent/src/probe")
    assert updater.detect_install().method == updater.Method.EDITABLE


def test_detect_pip_vs_managed(monkeypatch, tmp_path):
    # build a realistic venv layout: <proj>/.venv/lib/python3.12/site-packages/probe
    pkg = tmp_path / ".venv" / "lib" / "python3.12" / "site-packages" / "probe"
    pkg.mkdir(parents=True)
    _patch_pkg(monkeypatch, str(pkg))
    assert updater.detect_install().method == updater.Method.PIP  # no lockfile
    (tmp_path / "uv.lock").write_text("")  # now it's a managed project
    assert updater.detect_install().method == updater.Method.MANAGED


def test_detect_managed_out_of_project_poetry(monkeypatch):
    # Poetry/Pipenv keep the venv OUTSIDE the project, so no lockfile at venv.parent —
    # must still be recognized as managed (H6), not fall through to `pip install -U`.
    _patch_pkg(
        monkeypatch,
        "/home/u/.cache/pypoetry/virtualenvs/proj-abc123-py3.12/lib/python3.12/site-packages/probe",
    )
    assert updater.detect_install().method == updater.Method.MANAGED


def test_venv_root_both_layouts():
    assert updater._venv_root(
        Path("/home/u/app/.venv/lib/python3.12/site-packages/probe")
    ) == Path("/home/u/app/.venv")
    # Windows Lib/site-packages is one level shallower — must not overshoot to the project.
    assert updater._venv_root(
        Path("/c/proj/.venv/Lib/site-packages/probe")
    ) == Path("/c/proj/.venv")


# -- CLI upgrade dispatch (H3/H5/H6) ---------------------------------------
def _record_run(monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(
        updater, "_run",
        lambda cmd, timeout: (calls.append(cmd) or subprocess.CompletedProcess(cmd, 0)),
    )
    return calls


def _stub_installed(monkeypatch, versions):
    """Feed _installed_cli_version() a sequence — what it reports after each upgrade."""
    it = iter(versions)
    monkeypatch.setattr(updater, "_installed_cli_version", lambda: next(it))


def test_upgrade_uv_tool_advances(monkeypatch):
    calls = _record_run(monkeypatch)
    _stub_installed(monkeypatch, ["0.8.2"])
    res = updater.upgrade_cli(updater.Install(updater.Method.UV_TOOL), "0.8.1", "0.8.2")
    assert res.ok and res.changed and res.after == "0.8.2"
    assert calls == [["uv", "tool", "upgrade", "probe-research"]]


def test_upgrade_uv_tool_pinned_noop_then_force(monkeypatch):
    # `uv tool upgrade` no-ops on a version pin (exits 0, version unchanged) -> force @latest
    calls = _record_run(monkeypatch)
    _stub_installed(monkeypatch, ["0.8.1", "0.8.2"])  # after upgrade (no move), after force (moved)
    res = updater.upgrade_cli(updater.Install(updater.Method.UV_TOOL), "0.8.1", "0.8.2")
    assert res.ok and res.changed and res.after == "0.8.2"
    assert calls == [
        ["uv", "tool", "upgrade", "probe-research"],
        ["uv", "tool", "install", "--force", "probe-research@latest"],
    ]


def test_upgrade_uv_tool_stuck_is_honest_not_a_lie(monkeypatch):
    # even after the force reinstall the version never moves -> report failure, don't claim success
    _record_run(monkeypatch)
    _stub_installed(monkeypatch, ["0.8.1", "0.8.1"])
    res = updater.upgrade_cli(updater.Install(updater.Method.UV_TOOL), "0.8.1", "0.8.2")
    assert not res.ok and not res.changed and "still 0.8.1" in res.message


def test_upgrade_uv_tool_already_latest(monkeypatch):
    _record_run(monkeypatch)
    _stub_installed(monkeypatch, ["0.8.2"])  # already at target, nothing moved
    res = updater.upgrade_cli(updater.Install(updater.Method.UV_TOOL), "0.8.2", "0.8.2")
    assert res.ok and not res.changed and "already" in res.message


def test_upgrade_legacy_does_uninstall_then_install(monkeypatch):
    calls = _record_run(monkeypatch)
    _stub_installed(monkeypatch, ["0.8.2"])
    updater.upgrade_cli(updater.Install(updater.Method.UV_TOOL_LEGACY), "0.7.0", "0.8.2")
    assert calls == [
        ["uv", "tool", "uninstall", "probe-agent"],
        ["uv", "tool", "install", "--force", "probe-research"],
    ]


def test_upgrade_pipx(monkeypatch):
    calls = _record_run(monkeypatch)
    _stub_installed(monkeypatch, ["0.8.2"])
    updater.upgrade_cli(updater.Install(updater.Method.PIPX), "0.8.1", "0.8.2")
    assert calls == [["pipx", "upgrade", "probe-research"]]


@pytest.mark.parametrize("method", [updater.Method.EDITABLE, updater.Method.MANAGED, updater.Method.UNKNOWN])
def test_upgrade_refuses_and_never_runs(monkeypatch, method):
    calls = _record_run(monkeypatch)
    res = updater.upgrade_cli(updater.Install(method), "0.8.1", "0.8.2")
    assert not res.ran and calls == [] and res.message  # instruction, no mutation


# -- plugin update: post-condition + non-TTY (H1/H2) -----------------------
class _FakeRun:
    def __init__(self):
        self.kwargs = None

    def __call__(self, cmd, **kwargs):
        self.kwargs = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def _seq(values):
    it = iter(values)
    return lambda: next(it)


def test_plugin_confirmed_when_version_advances(monkeypatch):
    fake = _FakeRun()
    monkeypatch.setattr(updater.shutil, "which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr(updater.subprocess, "run", fake)
    monkeypatch.setattr(updater, "installed_plugin_version", _seq(["0.6.0", "0.7.0"]))
    res = updater.update_plugin("0.7.0")
    assert res.confirmed and res.changed and res.after == "0.7.0"
    assert fake.kwargs["stdin"] is subprocess.DEVNULL  # H2: no TTY on the child


def test_plugin_noop_not_confirmed_even_on_zero_exit(monkeypatch):
    # claude exits 0 but the version never moves (nested-session no-op) -> NOT confirmed (H1)
    monkeypatch.setattr(updater.shutil, "which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr(updater.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(updater, "installed_plugin_version", lambda: "0.6.0")
    res = updater.update_plugin("0.7.0")
    assert res.attempted and not res.confirmed


def test_plugin_absent_claude(monkeypatch):
    monkeypatch.setattr(updater.shutil, "which", lambda _n: None)
    res = updater.update_plugin("0.7.0")
    assert not res.attempted and not res.confirmed


def test_plugin_already_current_is_confirmed_but_unchanged(monkeypatch):
    # at target already: confirmed=True (post-condition holds) but changed=False, so
    # the caller won't falsely tell the user to restart for a no-op.
    monkeypatch.setattr(updater.shutil, "which", lambda _n: "/usr/bin/claude")
    monkeypatch.setattr(updater.subprocess, "run", lambda cmd, **k: subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(updater, "installed_plugin_version", lambda: "0.8.1")
    res = updater.update_plugin("0.8.1")
    assert res.confirmed and not res.changed and "already" in res.message


# -- --check exit codes (H7) -----------------------------------------------
def _patch_fetch(monkeypatch, manifest=None, exc=None):
    def fake(_base):
        if exc:
            raise exc
        return manifest
    monkeypatch.setattr(updater, "fetch_latest", fake)


def test_check_current_exit_0(monkeypatch):
    _patch_fetch(monkeypatch, {"cli": {"latest": probe.__version__}})
    assert cli.main(["update", "--check"]) == updater.CHECK_CURRENT


def test_check_behind_exit_10(monkeypatch):
    _patch_fetch(monkeypatch, {"cli": {"latest": "999.0.0"}})
    assert cli.main(["update", "--check"]) == updater.CHECK_BEHIND


def test_check_error_exit_1(monkeypatch):
    _patch_fetch(monkeypatch, exc=RuntimeError("network down"))
    assert cli.main(["update", "--check"]) == updater.CHECK_ERROR
