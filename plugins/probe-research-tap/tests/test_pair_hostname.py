"""The name a paired capture device wears in the dashboard.

The tap pairs with `{"hostname": ...}` and the server keeps that as the capture
row's label. It used to send `socket.gethostname()`, which on macOS follows the
DHCP lease -- so a laptop that paired on a captive WiFi wore
`visitor-10-59-125-182` in the dashboard from then on. The CLI stopped doing
this in 0.134.0 and the capture surface is the other half of the same machine.
"""

from __future__ import annotations

import subprocess

import pytest

from tap import pair


def test_the_configured_name_beats_the_one_the_network_assigned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pair, "_configured_hostnames", lambda: ["Richards-MacBook-Pro"])
    monkeypatch.setattr(pair.socket, "gethostname", lambda: "visitor-10-59-125-182")
    assert pair._hostname() == "Richards-MacBook-Pro"


def test_the_transient_name_is_still_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pair, "_configured_hostnames", lambda: [])
    monkeypatch.setattr(pair.socket, "gethostname", lambda: "prbe-devbox.internal")
    assert pair._hostname() == "prbe-devbox"


@pytest.mark.parametrize("configured", [[""], ["   "], ["Not set"], ["not set"]])
def test_an_unset_configured_name_is_not_used(
    configured: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`scutil --get LocalHostName` prints `Not set` rather than failing."""
    monkeypatch.setattr(pair, "_configured_hostnames", lambda: configured)
    monkeypatch.setattr(pair.socket, "gethostname", lambda: "fallback-host")
    assert pair._hostname() == "fallback-host"


def test_a_hostname_file_that_is_not_utf8_falls_back_instead_of_failing_the_pair(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    bad = tmp_path / "hostname"
    bad.write_bytes(b"\xff\xfe not utf 8")
    monkeypatch.setattr(pair.sys, "platform", "linux")
    monkeypatch.setattr(pair, "Path", lambda _p: bad)
    assert pair._configured_hostnames() == []


def test_a_missing_scutil_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pair.sys, "platform", "darwin")

    def _boom(*_args, **_kwargs):
        raise FileNotFoundError("no scutil here")

    monkeypatch.setattr(pair.subprocess, "run", _boom)
    assert pair._configured_hostnames() == []


def test_scutil_is_called_with_a_fixed_argv_and_a_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pairing runs on a session-start path; a hung `scutil` must not sit on it."""
    seen: dict[str, object] = {}

    def _run(argv, **kwargs):
        seen["argv"] = argv
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, 0, stdout="my-laptop\n", stderr="")

    monkeypatch.setattr(pair.sys, "platform", "darwin")
    monkeypatch.setattr(pair.subprocess, "run", _run)

    assert pair._configured_hostnames() == ["my-laptop"]
    assert seen["argv"] == ["/usr/sbin/scutil", "--get", "LocalHostName"]
    assert seen["timeout"] == 2


def test_the_pair_request_sends_the_stable_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point: what actually goes on the wire is the stable name."""
    monkeypatch.setattr(pair, "_configured_hostnames", lambda: ["Richards-MacBook-Pro"])
    monkeypatch.setattr(pair.socket, "gethostname", lambda: "visitor-10-59-125-182")
    assert pair._hostname() == "Richards-MacBook-Pro"
    assert "gethostname()" not in _pair_source().split("body = json.dumps")[1][:200]


def _pair_source() -> str:
    from pathlib import Path

    return Path(pair.__file__).read_text(encoding="utf-8")


def test_the_tap_and_the_cli_agree_on_how_a_machine_is_named() -> None:
    """PARITY. The tap cannot import the CLI, so the logic is duplicated -- and a
    duplicate that drifts is worse than no duplicate at all, because the two
    halves of one machine then wear different names.

    Compared as source text rather than by importing `probe`: the tap package is
    installed on its own in the agent's plugin cache and the CLI is not
    importable from there.
    """
    from pathlib import Path

    cli = Path(__file__).resolve().parents[3] / "src" / "probe" / "sdk" / "device.py"
    if not cli.exists():  # installed tap, no monorepo alongside
        pytest.skip("CLI source not available beside the tap")

    cli_src = cli.read_text(encoding="utf-8")
    tap_src = _pair_source()
    for marker in (
        '["/usr/sbin/scutil", "--get", "LocalHostName"]',
        'Path("/etc/hostname").read_text(encoding="utf-8").strip()',
        'name.lower() != "not set"',
        'return name or "unknown-host"',
    ):
        assert marker in cli_src, f"CLI no longer contains {marker!r} -- update the tap copy"
        assert marker in tap_src, f"tap copy has drifted from the CLI: missing {marker!r}"
