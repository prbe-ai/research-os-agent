"""How a backfill agent is actually launched.

Two things are load-bearing and neither is obvious from reading the argv:
the context flags (measured, and NOT --bare, which breaks auth), and the
mutual exclusion between minting a session and resuming one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from probe.cli import backfill


def _claude(**kw):
    return backfill.agent_argv(
        backfill.Agent.CLAUDE, "/bin/claude", "PROMPT", Path("/tmp/x"), **kw
    )


def test_the_event_stream_is_still_requested():
    argv = _claude()
    assert "--output-format" in argv and "stream-json" in argv
    assert "--verbose" in argv, "stream-json emits only the result without it"


def test_the_tool_allowlist_is_unchanged():
    """The promise in AGENT_COPY is that it cannot write, delete or fetch."""
    argv = _claude()
    assert argv[argv.index("--allowedTools") + 1] == backfill.AGENT_TOOLS
    assert "Bash(probe:*)" in backfill.AGENT_TOOLS
    assert "Write" not in backfill.AGENT_TOOLS and "Edit" not in backfill.AGENT_TOOLS


def test_autocompact_is_on_so_a_long_unit_survives_its_own_context():
    argv = _claude()
    assert argv[argv.index("--autocompact") + 1] == "auto"


def test_the_measured_context_flags_are_sent():
    argv = _claude()
    for flag in backfill.CONTEXT_FLAGS:
        assert flag in argv


def test_bare_is_never_sent_because_it_breaks_auth():
    """--bare removes more context but never reads OAuth or the keychain, so a
    normal subscription install exits 1 with 'Not logged in'. Verified."""
    assert "--bare" not in _claude()
    assert "--bare" not in backfill.CONTEXT_FLAGS


def test_a_session_id_is_minted_when_one_is_asked_for():
    argv = _claude(session_id="sess-1")
    assert argv[argv.index("--session-id") + 1] == "sess-1"
    assert "--resume" not in argv


def test_resume_adopts_a_session_instead_of_minting_one():
    argv = _claude(session_id="sess-1", resume="sess-0")
    assert argv[argv.index("--resume") + 1] == "sess-0"
    assert "--session-id" not in argv, "a session cannot be both new and pre-existing"


def test_neither_flag_appears_when_neither_is_asked_for():
    argv = _claude()
    assert "--session-id" not in argv and "--resume" not in argv


def test_codex_is_unchanged_and_ignores_resume():
    """Codex has no --resume equivalent, so its units restart clean rather than
    being handed something that merely looks similar."""
    argv = backfill.agent_argv(
        backfill.Agent.CODEX, "/bin/codex", "PROMPT", Path("/tmp/x"), resume="sess-0"
    )
    assert argv[:3] == ["/bin/codex", "exec", "--json"]
    assert "--resume" not in argv and "--session-id" not in argv
    assert "-C" in argv and "/tmp/x" in argv


@pytest.mark.parametrize("flag", ["--strict-mcp-config", "--disable-slash-commands"])
def test_the_context_flags_are_the_measured_pair(flag):
    assert flag in backfill.CONTEXT_FLAGS
