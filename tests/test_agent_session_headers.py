"""Coding-agent session attribution is captured-only, bounded, and fail-open."""

from __future__ import annotations

import httpx
import pytest

from probe.sdk.agent_session import (
    AGENT_HEADER,
    AGENT_SESSION_HEADER,
    agent_session_headers,
    detect_agent,
    outdated_client,
    parse_version,
    resolve_agent_session,
)
from probe.sdk.config import Settings
from probe.sdk.defaults import _agent_context
from probe.sdk.surface import Surface
from probe.sdk.transport import Transport

SESSION = "643ce138-4801-4409-b69c-ca30ab7605bc"

# Shape verified against a live Claude Code session: the version carries a
# trailing suffix, and CLAUDE_CODE_CHILD_SESSION is the string "1" — a flag,
# NOT a nested session id.
LIVE = {
    "CLAUDECODE": "1",
    "CLAUDE_CODE_ENTRYPOINT": "cli",
    "CLAUDE_CODE_SESSION_ID": SESSION,
    "CLAUDE_CODE_VERSION": "2.1.219 (Claude Code)",
}


def test_live_claude_code_session_is_attributed() -> None:
    assert resolve_agent_session(LIVE) == ("claude_code", SESSION)
    assert agent_session_headers(LIVE) == {
        AGENT_HEADER: "claude_code",
        AGENT_SESSION_HEADER: SESSION,
    }


def test_child_shell_flag_does_not_change_the_session() -> None:
    """CLAUDE_CODE_CHILD_SESSION is a boolean flag, not an id.

    An earlier design assumed it held a nested session that had to be resolved
    back to the parent. It does not: the variable is "1" and
    CLAUDE_CODE_SESSION_ID still carries the real session, so a subagent shell
    must attribute to exactly the same session as its parent.
    """
    assert resolve_agent_session({**LIVE, "CLAUDE_CODE_CHILD_SESSION": "1"}) == (
        "claude_code",
        SESSION,
    )


@pytest.mark.parametrize(
    "env",
    [
        pytest.param({}, id="no-agent"),
        pytest.param({"CURSOR_TRACE_ID": "abc12345"}, id="cursor-uncaptured"),
        pytest.param({"CODEX_THREAD_ID": "th_9911223344"}, id="codex-uncaptured"),
        pytest.param({k: v for k, v in LIVE.items() if k != "CLAUDE_CODE_SESSION_ID"},
                     id="claude-code-without-session"),
    ],
)
def test_no_attribution_without_a_capturable_session(env: dict[str, str]) -> None:
    """Cursor and Codex expose a session id but nothing captures their
    transcripts for Research OS, so attributing them would store a key that can
    never resolve."""
    assert resolve_agent_session(env) is None
    assert agent_session_headers(env) == {}


@pytest.mark.parametrize(
    "session_id",
    [
        pytest.param("abc\r\nX-Evil: 1", id="crlf-injection"),
        pytest.param("has spaces", id="whitespace"),
        pytest.param("", id="empty"),
        pytest.param("short", id="below-min-length"),
        pytest.param("a" * 201, id="over-max-length"),
        pytest.param("../../etc/passwd", id="path-traversal"),
    ],
)
def test_unsafe_session_ids_emit_no_header(session_id: str) -> None:
    """Absent must mean NO header, never a blank or mangled one."""
    assert agent_session_headers({**LIVE, "CLAUDE_CODE_SESSION_ID": session_id}) == {}


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2.1.219 (Claude Code)", (2, 1, 219)),
        ("2.1.132", (2, 1, 132)),
        ("  2.1.132 ", (2, 1, 132)),
        ("not-a-version", None),
        ("2.1", None),
        (None, None),
        (2.1, None),
    ],
)
def test_version_parsing_tolerates_the_real_suffix(raw: object, expected: object) -> None:
    """Claude Code reports "2.1.219 (Claude Code)". A strict semver parse finds
    nothing there and would make every client look too old."""
    assert parse_version(raw) == expected


def test_old_client_gets_a_nudge_not_silence() -> None:
    old = {k: v for k, v in LIVE.items() if k != "CLAUDE_CODE_SESSION_ID"}
    old["CLAUDE_CODE_VERSION"] = "2.0.9 (Claude Code)"
    assert outdated_client(old) == ("Claude Code session", "2.1.132")


@pytest.mark.parametrize(
    "env",
    [
        pytest.param(LIVE, id="already-attributing"),
        pytest.param({}, id="not-an-agent"),
        pytest.param({"CURSOR_TRACE_ID": "abc12345"}, id="uncaptured-agent"),
        pytest.param({k: v for k, v in LIVE.items() if k != "CLAUDE_CODE_SESSION_ID"},
                     id="new-enough-but-session-missing"),
    ],
)
def test_no_nudge_when_upgrading_would_not_help(env: dict[str, str]) -> None:
    assert outdated_client(env) is None


def test_agent_context_and_attribution_share_one_detection_table() -> None:
    """They intentionally DIFFER on captured-ness but must agree on detection:
    an agent is worth naming in a hypothesis even when uncaptured."""
    for env, label in (
        (LIVE, "claude_code"),
        ({"CURSOR_TRACE_ID": "x" * 10}, "cursor"),
        ({"CODEX_THREAD_ID": "y" * 10}, "codex"),
    ):
        spec = detect_agent(env)
        assert spec is not None and spec.label == label


def test_agent_context_still_names_uncaptured_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setenv("CURSOR_TRACE_ID", "abc12345")
    assert _agent_context() == "Cursor session"


def _transport_headers(monkeypatch: pytest.MonkeyPatch) -> httpx.Headers:
    # httpx.Headers, not a dict: iterating headers into a dict lowercases every
    # key, so a dict lookup on the canonical spelling misses.
    seen = httpx.Headers()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen
        seen = request.headers
        return httpx.Response(200, json={})

    transport = Transport(
        Settings(base_url="https://example.test", token="t"),
        surface=Surface.SDK,
        client=httpx.Client(
            base_url="https://example.test",
            transport=httpx.MockTransport(handler),
        ),
    )
    transport.request("GET", "/v1/runs")
    return seen


def test_transport_sends_the_session_header_when_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in LIVE.items():
        monkeypatch.setenv(key, value)
    seen = _transport_headers(monkeypatch)
    assert seen[AGENT_SESSION_HEADER] == SESSION
    assert seen[AGENT_HEADER] == "claude_code"


def test_transport_omits_the_header_entirely_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (*LIVE, "CURSOR_TRACE_ID", "CODEX_SANDBOX", "CODEX_THREAD_ID"):
        monkeypatch.delenv(key, raising=False)
    seen = _transport_headers(monkeypatch)
    assert AGENT_SESSION_HEADER not in seen
    assert AGENT_HEADER not in seen
