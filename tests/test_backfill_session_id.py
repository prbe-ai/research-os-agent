"""The session id Claude Code will actually accept.

`uuid4().hex` shipped and died on the first real run: `--session-id` validates
the DASHED canonical form and rejects the 32-char hex with "Invalid session ID.
Must be a valid UUID". The agent exits before reading a single file, so the
classify pass returns no plan and the whole import stops having done nothing.

Nothing caught it because every other test fakes `launch_agent`, so the id never
reached the binary that validates it. These tests check the FORMAT contract
directly, and the last one runs the real binary when it is installed.
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from probe.cli import backfill as bf
from probe.cli import backfill_run as br


def test_a_session_id_is_the_dashed_canonical_uuid():
    sid = br.new_session_id()
    assert uuid.UUID(sid)
    assert sid.count("-") == 4, "the 32-char hex form is rejected by Claude Code"
    assert sid == str(uuid.UUID(sid)), "canonical form, lowercase, dashed"


def test_ids_are_unique():
    assert len({br.new_session_id() for _ in range(100)}) == 100


def test_the_argv_carries_a_valid_uuid():
    sid = br.new_session_id()
    argv = bf.agent_argv(bf.Agent.CLAUDE, "/bin/claude", "P", Path("/tmp"), session_id=sid)
    assert uuid.UUID(argv[argv.index("--session-id") + 1])


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude not installed")
def test_the_real_binary_accepts_what_we_generate():
    """The check that would have caught this.

    A REAL `-p` invocation, because `--help` short-circuits before validation --
    which is how the first version of this test passed against the very bug it
    was written for. Whatever else the run does is irrelevant; all that is
    asserted is that the id got past the parser."""
    proc = subprocess.run(
        ["claude", "-p", "say OK", "--session-id", br.new_session_id(),
         "--allowedTools", "Read"],
        capture_output=True, text=True, timeout=90,
    )
    assert "Invalid session ID" not in (proc.stdout + proc.stderr)


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude not installed")
def test_the_real_binary_rejects_the_hex_form_this_bug_shipped():
    """Proves the test above has teeth: the shape we shipped IS refused, and
    refused at argument parse, so this costs no API call."""
    proc = subprocess.run(
        ["claude", "-p", "say OK", "--session-id", uuid.uuid4().hex,
         "--allowedTools", "Read"],
        capture_output=True, text=True, timeout=60,
    )
    assert "Invalid session ID" in (proc.stdout + proc.stderr)
