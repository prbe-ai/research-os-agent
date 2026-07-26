# TODOs

Deferred work for the Probe Research agent (CLI, SDK, plugins). Each item states what
it is, why it matters, and roughly what it takes.

---

## P2 — coding-agent coverage

### Transcript capture for Cursor and Codex

**What:** A capture plugin for Cursor and for Codex, equivalent to
`plugins/probe-research-tap` for Claude Code.

**Why:** `sdk/defaults.py` `_agent_context()` already detects all three agents, and both
Cursor (`CURSOR_TRACE_ID`) and Codex (`CODEX_THREAD_ID`) expose a session identifier. But
nothing ships their transcripts for Research OS tenants, so those identifiers resolve to
nothing. Session↔run linking therefore deliberately records Claude Code only, gated behind
a capability table, so the id map never holds a permanently dead key.

**Context (2026-07-26, from the session-linking eng review):** the engine half partly
exists already — `prbe-knowledge` has a Codex transcript handler
(`kb/handlers/claude_code.py`, `_doc_id_prefix = "codex"`) alongside the Claude Code one.
What is missing on the Research OS side is the host-side capture plugin: transcript tail,
spool, device pairing, secret-scan gate. Once one exists, enabling that agent is a
one-line flip in the capability table, because the client already detects it and the
header/stamping path is agent-agnostic.

Before building: confirm the team actually uses these agents for research work. Neither is
confirmed today, which is why this is deferred rather than scoped.

**Effort:** L (Cursor), M (Codex — engine handler already exists)
**Priority:** P2
**Depends on:** session↔run linking landing first (establishes the capability table)
