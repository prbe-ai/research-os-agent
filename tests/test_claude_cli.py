"""The one wrapper every `claude` invocation goes through.

Four modules used to carry their own copy of this, with four different
timeouts and only ONE of them closing stdin. These tests pin the two
properties that made consolidating it worth doing: stdin is always closed,
and "could not run it" stays distinguishable from "it ran and said no".
"""

from __future__ import annotations

import subprocess

import pytest

from probe.cli import claude_cli


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def test_stdin_is_always_closed(monkeypatch):
    """THE regression guard for the invisible-prompt hang.

    capture_output redirects stdout and stderr but NOT stdin, so a `claude`
    subcommand that decides to prompt inherits the user's terminal: the
    question lands in the captured (invisible) stdout while the child blocks
    on a TTY nobody knows is being read, until the timeout fires. Handing it
    DEVNULL turns a full-timeout hang into an immediate, diagnosable EOF.
    """
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return _completed()

    monkeypatch.setattr(claude_cli.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(claude_cli.subprocess, "run", fake_run)

    claude_cli.run(["plugin", "list"], timeout=5)
    assert seen["stdin"] is subprocess.DEVNULL


def test_the_caller_chooses_the_timeout(monkeypatch):
    """Listing plugins is a directory read; installing one is a network fetch.
    A single shared constant would be wrong for both, which is why the four
    copies had drifted to 20/90/180/90 in the first place."""
    seen: dict = {}
    monkeypatch.setattr(claude_cli.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_cli.subprocess, "run", lambda cmd, **kw: seen.update(kw) or _completed()
    )

    claude_cli.run(["plugin", "list"], timeout=claude_cli.LIST_TIMEOUT_S)
    assert seen["timeout"] == claude_cli.LIST_TIMEOUT_S
    claude_cli.run(["plugin", "install", "x"], timeout=claude_cli.INSTALL_TIMEOUT_S)
    assert seen["timeout"] == claude_cli.INSTALL_TIMEOUT_S
    assert claude_cli.LIST_TIMEOUT_S < claude_cli.INSTALL_TIMEOUT_S


def test_a_missing_binary_is_unreachable_not_a_failure(monkeypatch):
    """`claude` absent is NORMAL on a GPU pod. Reporting it as a failed command
    is what would let the wizard tell such a machine its setup broke."""
    monkeypatch.setattr(claude_cli.shutil, "which", lambda _: None)

    result = claude_cli.run(["plugin", "list"], timeout=5)
    assert result.ok is False
    assert result.reachable is False
    assert "not found" in result.detail


def test_a_timeout_is_unreachable(monkeypatch):
    """Nothing ran to completion, so the caller cannot conclude anything about
    the state on disk -- least of all "the plugin is not installed"."""
    monkeypatch.setattr(claude_cli.shutil, "which", lambda _: "/usr/bin/claude")

    def boom(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 1))

    monkeypatch.setattr(claude_cli.subprocess, "run", boom)

    result = claude_cli.run(["plugin", "list"], timeout=5)
    assert result.ok is False
    assert result.reachable is False
    assert "timed out" in result.detail


def test_a_nonzero_exit_is_reachable_but_not_ok(monkeypatch):
    """The command RAN and said no. That is a real answer, unlike a timeout."""
    monkeypatch.setattr(claude_cli.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_cli.subprocess,
        "run",
        lambda cmd, **kw: _completed(returncode=1, stderr="not found in marketplace"),
    )

    result = claude_cli.run(["plugin", "install", "x"], timeout=5)
    assert result.ok is False
    assert result.reachable is True
    assert result.detail == "not found in marketplace"


def test_uncaptured_streams_do_not_raise(monkeypatch):
    """`stdout`/`stderr` are None when the streams were not captured. This
    helper is on every `claude` path, so a None there must not become an
    AttributeError three modules away."""
    monkeypatch.setattr(claude_cli.shutil, "which", lambda _: "/usr/bin/claude")
    monkeypatch.setattr(
        claude_cli.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess([], 0, None, None),
    )

    assert claude_cli.run(["x"], timeout=5).ok is True


def test_os_error_is_unreachable(monkeypatch):
    monkeypatch.setattr(claude_cli.shutil, "which", lambda _: "/usr/bin/claude")

    def boom(cmd, **kwargs):
        raise OSError("exec format error")

    monkeypatch.setattr(claude_cli.subprocess, "run", boom)
    result = claude_cli.run(["x"], timeout=5)
    assert (result.ok, result.reachable) == (False, False)


@pytest.mark.parametrize("ok", [True, False])
def test_result_is_truthy_iff_ok(ok):
    assert bool(claude_cli.Result(ok=ok)) is ok
