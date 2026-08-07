"""Coding-agent session attribution for outbound Probe Research requests.

Every request carries the id of the coding-agent session driving it, when
there is one, so the backend can record which conversation produced a run and
search can walk from a session to the work that came out of it.

Same contract shape as :mod:`probe.client_headers`: validated, bounded, and
fail-open. Malformed input produces NO header rather than a bad one, and the
backend treats what does arrive as untrusted — it is recorded against the run
and never consulted for authorization. A spoofed id yields a dead link, never
access to someone else's transcript.

ONLY agents whose transcripts are actually captured are reported. Claude Code
ships with capture support. Codex is reported only when its tap has a local
credential, which is the durable evidence that this device completed pairing;
merely running under Codex must not create a transcript link that can never
resolve. Cursor remains detectable but uncaptured.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

#: Request headers naming the coding agent and its session.
AGENT_HEADER = "X-Probe-Agent"
AGENT_SESSION_HEADER = "X-Probe-Agent-Session"

# Session ids are uuids today. The bound and charset are the real guard: this
# value is interpolated into an HTTP header, so anything with whitespace,
# control characters or header delimiters is rejected outright rather than
# escaped. Deliberately looser than a strict uuid match so a future agent that
# uses a different id shape still works without a client release.
_MAX_SESSION_LENGTH = 200
# \A/\Z, not ^/$: `$` also matches just before a trailing newline, and this
# exact string composes the cross-repo graph canonical_id, where a stray
# newline yields a node that silently never merges with the transcript side.
_SESSION_RE = re.compile(r"\A[A-Za-z0-9._:-]{8,200}\Z")

# Leading semver out of a version string that may carry a suffix. Claude Code
# reports "2.1.219 (Claude Code)", NOT a bare semver, so a strict parse finds
# nothing and every client looks too old.
_LEADING_SEMVER_RE = re.compile(r"^\s*(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class AgentSpec:
    """One coding agent and how to read its session out of the environment."""

    label: str
    """Matches the engine connector's agent label, so both sides compose the
    same graph canonical_id (``agent_session:{label}:{session_id}``)."""

    detect_env: tuple[str, ...]
    """Any one of these being set means we are running under this agent."""

    session_env: str | None
    """Where the session id lives, or None if the agent does not expose one."""

    captured: bool
    """Whether transcripts for this agent actually reach Research OS."""

    version_env: str | None = None
    min_version: tuple[int, int, int] | None = None
    """Below this version the agent does not export its session id at all."""

    display: str = ""


# Claude Code only exports CLAUDE_CODE_SESSION_ID to Bash subprocesses from
# 2.1.132 onward. Below that the variable is simply absent, which is
# indistinguishable from "not Claude Code" unless we also read the version —
# hence version_env, so an old client gets a "you need to upgrade" answer
# instead of silence.
#
# There is deliberately no child-session handling. CLAUDE_CODE_CHILD_SESSION
# is a BOOLEAN FLAG ("1"), not an id: when it is set, CLAUDE_CODE_SESSION_ID
# still holds the real session id, so there is nothing to resolve back to.
AGENTS: tuple[AgentSpec, ...] = (
    AgentSpec(
        label="claude_code",
        detect_env=("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"),
        session_env="CLAUDE_CODE_SESSION_ID",
        captured=True,
        version_env="CLAUDE_CODE_VERSION",
        min_version=(2, 1, 132),
        display="Claude Code session",
    ),
    AgentSpec(
        label="cursor",
        detect_env=("CURSOR_TRACE_ID",),
        session_env="CURSOR_TRACE_ID",
        captured=False,
        display="Cursor session",
    ),
    AgentSpec(
        label="codex",
        detect_env=("CODEX_SANDBOX", "CODEX_THREAD_ID"),
        session_env="CODEX_THREAD_ID",
        captured=True,
        display="Codex session",
    ),
)


def _codex_capture_paired(env: Mapping[str, str]) -> bool:
    """Whether the Codex tap has a credential without ever reading the secret.

    The tap accepts an explicit environment token or its mode-0600 token file.
    Attribution mirrors those two sources but checks only presence/non-emptiness.
    ``PRBE_CODEX_TAP_PLUGIN_DIR`` is also the tap's test/development override,
    so this stays deterministic without touching a user's real Codex state.
    """
    if (env.get("PRBE_CODEX_TAP_TOKEN") or "").strip():
        return True
    configured = env.get("PRBE_CODEX_TAP_PLUGIN_DIR")
    if configured:
        root = Path(configured)
    else:
        state = Path.home() / ".codex" / "state"
        current = state / "probe-research-tap"
        legacy = state / "prbe-codex-tap-plugin"
        root = legacy if legacy.exists() and not current.exists() else current
    try:
        return bool((root / ".token").read_text(encoding="utf-8").strip())
    except OSError:
        return False


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def detect_agent(env: Mapping[str, str] | None = None) -> AgentSpec | None:
    """The coding agent driving this process, or None."""
    e = _env(env)
    for spec in AGENTS:
        if any(e.get(key) for key in spec.detect_env):
            return spec
    return None


def parse_version(raw: object) -> tuple[int, int, int] | None:
    """Leading ``major.minor.patch`` from a version string, tolerating a suffix."""
    if not isinstance(raw, str):
        return None
    match = _LEADING_SEMVER_RE.match(raw)
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def valid_session_id(raw: object) -> bool:
    """Whether a value is safe and plausible enough to send as a header."""
    return isinstance(raw, str) and len(raw) <= _MAX_SESSION_LENGTH and bool(_SESSION_RE.match(raw))


def resolve_agent_session(
    env: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """``(agent_label, session_id)`` for the current session, or None.

    None whenever we are not under a supported agent, the agent's transcripts
    are not captured, the client is too old to export its session, or the value
    present is not header-safe. Never raises: attribution is telemetry, and a
    run must never fail to be created because of it.
    """
    spec = detect_agent(env)
    if spec is None or not spec.captured or spec.session_env is None:
        return None
    values = _env(env)
    if spec.label == "codex" and not _codex_capture_paired(values):
        return None
    session_id = values.get(spec.session_env)
    if not valid_session_id(session_id):
        return None
    assert isinstance(session_id, str)  # narrowed by valid_session_id
    return (spec.label, session_id)


def outdated_client(env: Mapping[str, str] | None = None) -> tuple[str, str] | None:
    """``(display_name, minimum_version)`` when the agent is too old to attribute.

    Distinguishes "your Claude Code predates session export" from "you are not
    running a coding agent", so the one-time upgrade nudge only fires at people
    it can actually help. None when there is nothing to say.
    """
    spec = detect_agent(env)
    if spec is None or not spec.captured or spec.min_version is None or spec.version_env is None:
        return None
    # Already attributing: nothing to nudge about.
    if resolve_agent_session(env) is not None:
        return None
    current = parse_version(_env(env).get(spec.version_env))
    if current is None or current >= spec.min_version:
        # Unknown or new-enough version: the session is missing for some other
        # reason, and telling someone to upgrade would be wrong.
        return None
    return (spec.display, ".".join(str(part) for part in spec.min_version))


def agent_session_headers(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Headers naming the current agent session, or ``{}`` — never a blank header."""
    resolved = resolve_agent_session(env)
    if resolved is None:
        return {}
    agent, session_id = resolved
    return {AGENT_HEADER: agent, AGENT_SESSION_HEADER: session_id}
