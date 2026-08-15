#!/usr/bin/env python3
"""Probe Research plugin telemetry: the session funnel, observed from hooks.

One event per funnel step, so drop-off says which fix is needed
(distribution, prompting, or a bug):

    plugin.session_started   SessionStart: plugin installed & running
    plugin.mcp_used          first probe-research MCP tool call this session
    plugin.skill_invoked     a research-tracking skill fired (which one)
    plugin.probe_write       a `probe` CLI write ran (entity, verb, outcome)
    plugin.session_summary   SessionEnd: the whole funnel as booleans

Contract (mirrors version_check.py, but stricter — this hook has no user-visible
output at all):
  * OBSERVABILITY ONLY. Never gates, never blocks, never prints hook JSON that
    could alter the session. Any failure exits 0 silently.
  * NO NETWORK on the hook path. The observing process parses stdin, updates a
    tiny state file, and hands the event to a DETACHED sender
    (start_new_session, the same escape hatch _spawn_autoupdate uses) which does
    the enrichment (identity, versions) and the POST with a short timeout. A
    PostHog outage costs the session nothing.
  * METADATA ONLY. Event properties are ids, versions, whitelisted skill/entity/
    verb names and booleans. No prompts, no commands, no paths, no contents.
  * KILLSWITCH. PROBE_TELEMETRY=off (or 0/false/no/disabled) disables everything.
  * HOSTED ONLY. The sender no-ops when the configured base_url is not the
    hosted service — self-host machines never call the vendor (the client-side
    extension of tests/selfhost/test_egress.py's promise).
  * STDLIB ONLY. Runs under the system python3; `probe` and `posthog` are not
    importable here and never will be (see test_plugin_telemetry.py). The
    shared contract (key, host, killswitch, identity, batch shape) lives in
    the VENDORED `_telemetry_core.py` beside this file — a byte-identical copy
    of src/probe/cli/_telemetry_core.py, refreshed by `make sync-telemetry-core`
    and pinned by tests/test_telemetry_core_parity.py. Never edit the copy.

Identity: distinct_id is the Probe user's UUID (GET /v1/me, cached 24h, resolved
only in the detached sender), the same value app/core/analytics.py and the
dashboard identify with, so plugin events merge with server and browser events.
Unauthenticated machines fall back to a stable per-machine id with
$process_person_profile off (the backend's convention for non-person actors) so
the "installed but never logged in" population stays countable without minting
fake person profiles. The machine id also rides every event as a plain property
(the cross-surface join key — see _telemetry_core.machine_id). The team group
key is the tenant slug (customer_id), matching analytics.capture(); it is
OMITTED when unknown, never null.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time


def _load_core():
    """The vendored contract module, loaded by EXPLICIT sibling path only.

    Never a bare `import _telemetry_core`: sys.path can carry the user's
    project (session env), and executing a same-named stranger module inside
    a hook would be arbitrary code execution — fail closed instead. Works
    under every real loading mode: script run (`python3 telemetry.py …`, how
    hooks and the detached sender execute) and the test suite's
    spec_from_file_location (no package, no path entry).
    """
    import importlib.util  # noqa: PLC0415

    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_telemetry_core.py")
    spec = importlib.util.spec_from_file_location("_telemetry_core", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    _core = _load_core()
except BaseException:  # noqa: BLE001 - a hook contracted to silence must not
    # traceback at import time: hooks.json execs this file with NO stderr
    # redirect, so a missing/corrupt/quarantined sibling (plugin-update window,
    # AV quarantine) would otherwise print + exit 1 on EVERY tool call.
    sys.exit(0)

POSTHOG_HOST = _core.POSTHOG_HOST
POSTHOG_KEY = _core.POSTHOG_KEY
SEND_TIMEOUT = _core.SEND_TIMEOUT
ME_CACHE_TTL = _core.ME_CACHE_TTL
DEFAULT_BASE = _core.DEFAULT_BASE
STATE_DIR = "/tmp/probe-telemetry"  # per-session funnel flags; pruned after 2 days

EVENT_SESSION_STARTED = "plugin.session_started"
EVENT_MCP_USED = "plugin.mcp_used"
EVENT_SKILL_INVOKED = "plugin.skill_invoked"
EVENT_WRITE = "plugin.probe_write"
EVENT_SESSION_SUMMARY = "plugin.session_summary"

# Our skills, by slug. Skill invocations arrive as "start-research-work" or
# namespaced "probe-research:start-research-work"; match the trailing slug.
RESEARCH_SKILLS = {
    "start-research-work",
    "track-research-work",
    "toggle-research-tracking",
    "capture-run-inputs",
    "instrument-training-runs",
    "show-research-timeline",
    "probe-research-setup",
}

MCP_TOOL_RE = re.compile(r"^mcp__.*probe[-_]research")

# `probe <entity> <verb>` pairs that mutate research state. Reads (get, show,
# list, status, check, download, versions) are deliberately not events — the
# funnel question is "did anything get RECORDED", not "was probe touched".
PROBE_ENTITIES = {
    "project",
    "experiment",
    "run",
    "runs",
    "note",
    "notes",
    "artifact",
    "span",
    "group",
    "version",
    "views",
    "wiki",
    "outbox",
}
WRITE_VERBS = {
    "create",
    "set",
    "use",
    "start",
    "end",
    "tag",
    "child",
    "write",
    "append",
    "add",
    "version-add",
    "pin-impact",
    "upload",
    "freeze",
    "unfreeze",
    "retry",
}
PROBE_CMD_RE = re.compile(r"(?:^|[;&|(\s])probe\s+([a-z][a-z-]*)\s+([a-z][a-z-]*)")

# Cap per-session write events: a shell loop logging metrics via the CLI must
# not turn telemetry into the noisiest thing in the project. The summary event
# still carries the true count.
MAX_WRITE_EVENTS = 20


def telemetry_disabled() -> bool:
    return _core.telemetry_disabled()


# ---------------------------------------------------------------------------
# Config / identity — thin wrappers over the vendored core (kept as module
# functions so existing callers and tests keep their seams)
# ---------------------------------------------------------------------------


def read_cli_config() -> dict:
    return _core.read_cli_config()


def machine_id() -> str:
    return _core.machine_id()


def _tap_customer_id() -> str | None:
    """Tenant slug from the tap's pairing state, read-only — the zero-network
    fallback for the team group when /v1/me is unreachable. Only present when
    the transcript tap is paired; None is a normal answer."""
    try:
        import sqlite3

        db = os.path.join(
            os.environ.get("PROBE_RESEARCH_TAP_PLUGIN_DIR")
            or os.path.join(os.path.expanduser("~"), ".claude", "plugins", "probe-research-tap"),
            "state.db",
        )
        if not os.path.exists(db):
            return None
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1)
        try:
            row = conn.execute("SELECT v FROM meta WHERE k = 'customer_id'").fetchone()
        finally:
            conn.close()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def resolve_identity(cfg: dict) -> dict:
    """See _telemetry_core.resolve_identity; the tap's tenant slug is the
    plugin-specific zero-network fallback for the team group."""
    return _core.resolve_identity(cfg, fallback_customer_id=_tap_customer_id())


def plugin_version() -> str | None:
    path = os.environ.get("PROBE_PLUGIN_JSON")
    if not path:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        flavor = ".codex-plugin" if os.environ.get("PROBE_AGENT") == "codex" else ".claude-plugin"
        path = os.path.join(root, flavor, "plugin.json")
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f).get("version")
    except Exception:
        return None


def cli_version() -> str | None:
    probe_bin = os.environ.get("PROBE_BIN") or "probe"
    try:
        out = subprocess.run(
            [probe_bin, "--version"], capture_output=True, text=True, timeout=SEND_TIMEOUT
        )
        if out.returncode == 0:
            v = (out.stdout or "").strip()
            return v.split()[-1] if v else None
    except Exception:
        return None
    return None


# ---------------------------------------------------------------------------
# Per-session funnel state
# ---------------------------------------------------------------------------


def _state_path(session_id: str) -> str:
    # session ids are Claude/Codex-issued UUIDs; sanitize anyway before using
    # one as a filename.
    safe = re.sub(r"[^0-9a-zA-Z-]", "", session_id)[:64]
    return os.path.join(STATE_DIR, f"{safe}.json")


def load_state(session_id: str) -> dict:
    try:
        with open(_state_path(session_id), encoding="utf-8") as f:
            state = json.load(f)
        if isinstance(state, dict):
            return state
    except Exception:
        pass
    return {"started_at": time.time(), "mcp_used": False, "skills": [], "writes": 0}


def save_state(session_id: str, state: dict) -> None:
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        tmp = _state_path(session_id) + f".{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, _state_path(session_id))
    except Exception:
        pass


def prune_state(max_age_days: float = 2.0) -> None:
    """Drop state for sessions that never reached SessionEnd (crash, kill).

    os.scandir, not find(1): `find /tmp` on macOS matches the /tmp symlink
    itself and silently descends into nothing (see the tap hook's war story).
    """
    try:
        cutoff = time.time() - max_age_days * 86400
        with os.scandir(STATE_DIR) as it:
            for entry in it:
                try:
                    if entry.is_file() and entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                except Exception:
                    continue
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Classification (pure; unit-tested)
# ---------------------------------------------------------------------------


def match_skill(tool_name: str, tool_input: dict) -> str | None:
    """The research skill slug this tool call invokes, if any."""
    candidates: list[str] = []
    if tool_name in ("Skill", "SlashCommand"):
        for field in ("skill", "command", "skill_name", "name"):
            v = tool_input.get(field)
            if isinstance(v, str):
                candidates.append(v)
    for cand in candidates:
        slug = cand.strip().lstrip("/").split()[0] if cand.strip() else ""
        slug = slug.split(":")[-1]  # "probe-research:start-research-work" -> slug
        if slug in RESEARCH_SKILLS:
            return slug
    return None


def match_probe_writes(command: str) -> list[tuple[str, str]]:
    """(entity, verb) for every probe CLI *write* in a shell command string.

    The regex sees through chains (&&, ;, |) but not through quoting or
    variable indirection — telemetry, not a shell parser. Misses under-count;
    they never invent a write that did not happen.
    """
    writes = []
    for entity, verb in PROBE_CMD_RE.findall(command or ""):
        if entity in PROBE_ENTITIES and verb in WRITE_VERBS:
            writes.append((entity, verb))
    return writes


def bash_outcome(tool_response) -> str:
    """success | error | unknown, defensively — the response shape is not ours."""
    if not isinstance(tool_response, dict):
        return "unknown"
    if tool_response.get("interrupted"):
        return "error"
    for key in ("exit_code", "exitCode", "code", "returnCode"):
        v = tool_response.get(key)
        if isinstance(v, int):
            return "success" if v == 0 else "error"
    if tool_response.get("is_error") is True:
        return "error"
    if tool_response.get("is_error") is False:
        return "success"
    return "unknown"


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def spawn_sender(events: list[dict]) -> None:
    """Hand events to a detached copy of this script; return immediately.

    start_new_session detaches it from the hook's process group (the tap
    daemon's lesson: anything less dies with the hook). The payload rides
    stdin — small enough to fit a pipe buffer, invisible to `ps`.
    """
    if not events:
        return
    try:
        proc = subprocess.Popen(  # noqa: S603 - sys.executable + own file
            [sys.executable, os.path.abspath(__file__), "send"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        proc.stdin.write(json.dumps(events).encode())
        proc.stdin.close()
    except Exception:
        pass


def build_batch(events: list[dict], ident: dict, pv: str | None, cv: str | None) -> list[dict]:
    """Enrich observed events into PostHog batch entries — via the shared core,
    so property names can never drift from the CLI's `wizard.*`/`backfill.*`
    events or app/core/analytics.py."""
    return _core.build_batch(
        events,
        ident,
        client_kind="plugin",
        lib="probe-plugin",
        client_version=pv,
        cli_version=cv,
        machine=_core.machine_id(),
    )


def send_from_stdin() -> None:
    """Detached sender: enrich with identity + versions, POST, fail silent."""
    events = json.load(sys.stdin)
    if not isinstance(events, list) or not events:
        return
    cfg = read_cli_config()
    # Hosted-only gate: a self-host base_url means the client never calls the
    # vendor. effective_base_url honors PROBE_BASE_URL like sdk resolve() —
    # a self-hoster configured through env must be muted too. Checked here,
    # off the hook path, where the config is read anyway.
    if not _core.hosted_base_url(_core.effective_base_url(cfg)):
        return
    ident = resolve_identity(cfg)
    pv = plugin_version()
    # `probe --version` costs a subprocess; pay it only on the two per-session
    # events where a stale CLI is the question being asked.
    enrich_cli = any(
        e.get("event") in (EVENT_SESSION_STARTED, EVENT_SESSION_SUMMARY) for e in events
    )
    cv = cli_version() if enrich_cli else None
    _core.post_batch(build_batch(events, ident, pv, cv))


# ---------------------------------------------------------------------------
# Hook entrypoints
# ---------------------------------------------------------------------------


def handle_session_start(payload: dict) -> None:
    session_id = payload.get("session_id") or ""
    cfg = read_cli_config()
    prune_state()
    if session_id:
        state = load_state(session_id)
        save_state(session_id, state)
    spawn_sender(
        [
            {
                "event": EVENT_SESSION_STARTED,
                "properties": {
                    "session_id": session_id,
                    "session_source": payload.get("source"),
                    "mcp_token_present": bool(cfg.get("mcp_token") or cfg.get("token")),
                },
            }
        ]
    )


def handle_post_tool(payload: dict) -> None:
    session_id = payload.get("session_id") or ""
    tool_name = payload.get("tool_name") or ""
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    if not session_id:
        return

    events: list[dict] = []
    state = load_state(session_id)
    changed = False

    if MCP_TOOL_RE.match(tool_name):
        if not state.get("mcp_used"):
            state["mcp_used"] = True
            changed = True
            events.append(
                {
                    "event": EVENT_MCP_USED,
                    "properties": {
                        "session_id": session_id,
                        "tool": tool_name.rsplit("__", 1)[-1],
                    },
                }
            )
    else:
        skill = match_skill(tool_name, tool_input)
        if skill:
            if skill not in state.get("skills", []):
                state.setdefault("skills", []).append(skill)
                changed = True
                events.append(
                    {
                        "event": EVENT_SKILL_INVOKED,
                        "properties": {"session_id": session_id, "skill": skill},
                    }
                )
        elif tool_name == "Bash":
            writes = match_probe_writes(tool_input.get("command") or "")
            if writes:
                outcome = bash_outcome(payload.get("tool_response"))
                prior = state.get("writes", 0)
                state["writes"] = prior + len(writes)
                changed = True
                for entity, verb in writes[: max(0, MAX_WRITE_EVENTS - prior)]:
                    events.append(
                        {
                            "event": EVENT_WRITE,
                            "properties": {
                                "session_id": session_id,
                                "entity_type": entity,
                                "verb": verb,
                                "outcome": outcome,
                                "via": "cli",
                            },
                        }
                    )

    if changed:
        save_state(session_id, state)
    spawn_sender(events)


def handle_session_end(payload: dict) -> None:
    session_id = payload.get("session_id") or ""
    if not session_id:
        return
    state = load_state(session_id)
    duration = max(0, int(time.time() - state.get("started_at", time.time())))
    spawn_sender(
        [
            {
                "event": EVENT_SESSION_SUMMARY,
                "properties": {
                    "session_id": session_id,
                    "reason": payload.get("reason"),
                    "duration_seconds": duration,
                    "mcp_used": bool(state.get("mcp_used")),
                    "skills_invoked": sorted(state.get("skills", [])),
                    "skill_invoked": bool(state.get("skills")),
                    "write_count": state.get("writes", 0),
                    "write_happened": state.get("writes", 0) > 0,
                },
            }
        ]
    )
    try:
        os.unlink(_state_path(session_id))
    except Exception:
        pass


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if telemetry_disabled():
        return
    if mode == "send":
        send_from_stdin()
        return
    payload = json.load(sys.stdin)
    if not isinstance(payload, dict):
        return
    if mode == "session-start":
        handle_session_start(payload)
    elif mode == "post-tool":
        handle_post_tool(payload)
    elif mode == "session-end":
        handle_session_end(payload)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # observability must never become observable
    sys.exit(0)
