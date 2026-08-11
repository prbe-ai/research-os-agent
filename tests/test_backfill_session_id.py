"""The session id Claude Code will actually accept.

`uuid4().hex` shipped and died on the first real run: `--session-id` validates
the DASHED canonical form and rejects the 32-char hex with "Invalid session ID.
Must be a valid UUID". The agent exits before reading a single file, so the
classify pass returns no plan and the whole import stops having done nothing.

Nothing caught it because every other test fakes `launch_agent`, so the id never
reached the binary that validates it. These tests pin the FORMAT contract
directly: `new_session_id()` must emit the dashed canonical UUID (the 32-char hex
form is the shape Claude Code rejects). Confirming that against the real `claude`
binary is deliberately NOT done here -- tests never spawn a real agent CLI (it
authenticates against the macOS keychain, and a suite that shelled out to
`claude -p` once wedged the login keychain hard enough to force a reboot).
"""

from __future__ import annotations

import uuid
from pathlib import Path

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
