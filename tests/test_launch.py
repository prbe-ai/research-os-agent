"""Launch-block capture: per-launch ephemera, never hashed into env identity."""
from __future__ import annotations

import sys

from probe.sdk.launch import capture_process, scrub_argv


def test_scrub_argv_redacts_flag_value_pairs():
    argv = ["train.py", "--api-key", "sk-abc12345678", "--lr", "3e-4"]
    out, scrubbed = scrub_argv(argv)
    assert out == ["train.py", "--api-key", "[redacted]", "--lr", "3e-4"]
    assert scrubbed is True


def test_scrub_argv_redacts_inline_and_bare_secrets():
    argv = ["run.sh", "--token=ghp_abcdefghij0123456789", "AKIAABCDEFGH12345678"]
    out, scrubbed = scrub_argv(argv)
    assert out == ["run.sh", "--token=[redacted]", "[redacted]"]
    assert scrubbed is True


def test_scrub_argv_clean_passthrough():
    argv = ["python", "train.py", "--seed", "42"]
    out, scrubbed = scrub_argv(argv)
    assert out == argv
    assert scrubbed is False


def test_capture_process_records_identity(tmp_path):
    info, errors = capture_process(argv=["python", "train.py"], cwd=str(tmp_path))
    assert info["argv"] == ["python", "train.py"]
    assert info["argv_scrubbed"] is False
    assert info["cwd"] == str(tmp_path)
    assert info["hostname"]
    assert info["user"]
    assert info["started_at"].endswith("+00:00") or info["started_at"].endswith("Z")
    assert errors == []


def test_capture_process_defaults_to_sys_argv():
    info, _ = capture_process()
    assert info["argv"] == scrub_argv(sys.argv)[0]
