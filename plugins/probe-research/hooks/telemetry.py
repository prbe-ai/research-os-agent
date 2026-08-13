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
  * STDLIB ONLY. Runs under the system python3; `probe` and `posthog` are not
    importable here and never will be (see test_plugin_telemetry.py).

Identity: distinct_id is the Probe user's UUID (GET /v1/me, cached 24h, resolved
only in the detached sender), the same value app/core/analytics.py and the
dashboard identify with, so plugin events merge with server and browser events.
Unauthenticated machines fall back to a stable per-machine id with
$process_person_profile off (the backend's convention for non-person actors) so
the "installed but never logged in" population stays countable without minting
fake person profiles. The team group key is the tenant slug (customer_id),
matching analytics.capture(); it is OMITTED when unknown, never null.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import uuid

# The same public write-only ingestion project the dashboard posts to (see
# dashboard/src/lib/analytics/posthog-config.ts for why embedding is fine; this
# file is mirrored into the public plugin repo, and that is acceptable for a
# capture-only key exactly as it is for the browser bundle).
POSTHOG_HOST = os.environ.get("PROBE_TELEMETRY_HOST") or "https://us.i.posthog.com"
POSTHOG_KEY = "phc_pCSs24bQtPaxoJ59PaTtTpJDS3dfzymfZeY74XQQ956K"
SEND_TIMEOUT = 3  # seconds; the sender is detached so this bounds nothing visible
ME_CACHE_TTL = 24 * 3600
STATE_DIR = "/tmp/probe-telemetry"  # per-session funnel flags; pruned after 2 days
DEFAULT_BASE = "https://api.research.prbe.ai"

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
    return (os.environ.get("PROBE_TELEMETRY") or "").strip().lower() in {
        "off",
        "0",
        "false",
        "no",
        "disabled",
    }


# ---------------------------------------------------------------------------
# Config / identity (all read-only, all fail-soft)
# ---------------------------------------------------------------------------


def _config_path() -> str:
    p = os.environ.get("PROBE_CONFIG_PATH")
    if p:
        return p
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "probe", "config.json")


def read_cli_config() -> dict:
    """The active context of the CLI config: v2 (named contexts) or v1 (flat).

    Mirrors bin/probe-mcp-headers and tap/config.py — reading only v1 silently
    lost every wizard-produced install once, so both shapes forever.
    """
    try:
        with open(_config_path(), encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        contexts = data.get("contexts")
        if isinstance(contexts, dict):
            active = contexts.get(data.get("current_context") or "default")
            return active if isinstance(active, dict) else {}
        return data
    except Exception:
        return {}


def _state_home() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(
        os.path.expanduser("~"), ".local", "state"
    )
    return os.path.join(base, "probe-telemetry")


def machine_id() -> str:
    """Stable anonymous fallback id, minted once per machine/user."""
    path = os.path.join(_state_home(), "machine_id")
    try:
        with open(path, encoding="utf-8") as f:
            mid = f.read().strip()
        if mid:
            return mid
    except Exception:
        pass
    mid = uuid.uuid4().hex
    try:
        os.makedirs(_state_home(), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(mid)
        os.replace(tmp, path)
    except Exception:
        pass  # ephemeral id this session; still countable, just not stable
    return mid


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
    """{distinct_id, customer_id, workspace_id, authenticated} — fail-soft.

    Runs ONLY in the detached sender, so the /v1/me call and its timeout never
    sit on the hook path. The result is cached for 24h keyed on (base_url,
    token), so steady state is one network call per machine per day.
    """
    token = cfg.get("token") or cfg.get("mcp_token")
    base_url = (cfg.get("base_url") or DEFAULT_BASE).rstrip("/")
    ws = cfg.get("workspace")
    workspace_id = ws.get("id") if isinstance(ws, dict) else None

    fallback = {
        "distinct_id": f"machine:{machine_id()}",
        "customer_id": _tap_customer_id(),
        "workspace_id": workspace_id,
        "authenticated": False,
    }
    if not token or not base_url.startswith(("http://", "https://")):
        return fallback

    import hashlib

    cache_key = hashlib.sha256(f"{base_url}|{token}".encode()).hexdigest()[:16]
    cache_path = os.path.join(_state_home(), "identity.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        if (
            cached.get("key") == cache_key
            and time.time() - cached.get("fetched_at", 0) < ME_CACHE_TTL
            and cached.get("user_id")
        ):
            return {
                "distinct_id": cached["user_id"],
                "email": cached.get("email"),
                "customer_id": cached.get("customer_id"),
                "workspace_id": workspace_id,
                "authenticated": True,
            }
    except Exception:
        pass

    try:
        req = urllib.request.Request(
            base_url + "/v1/me",
            headers={"Authorization": f"Bearer {token}", "User-Agent": "probe-plugin-telemetry"},
        )
        with urllib.request.urlopen(req, timeout=SEND_TIMEOUT) as resp:
            me = json.load(resp)
        user_id = me.get("user_id")
        if not user_id:
            return fallback
        record = {
            "key": cache_key,
            "fetched_at": time.time(),
            "user_id": user_id,
            "email": me.get("email"),
            "customer_id": me.get("customer_id"),
        }
        try:
            os.makedirs(_state_home(), exist_ok=True)
            tmp = cache_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f)
            os.replace(tmp, cache_path)
        except Exception:
            pass
        return {
            "distinct_id": user_id,
            "email": me.get("email"),
            "customer_id": me.get("customer_id"),
            "workspace_id": workspace_id,
            "authenticated": True,
        }
    except Exception:
        return fallback


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
    """Enrich observed events into PostHog batch entries. Pure; unit-tested.

    Property names match app/core/analytics.py (client_kind, client_version,
    team group, $process_person_profile) so plugin events share breakdowns and
    group rollups with the server-side events.
    """
    batch = []
    for e in events:
        props = dict(e.get("properties") or {})
        props.setdefault("agent", os.environ.get("PROBE_AGENT") or "claude_code")
        props["client_kind"] = "plugin"
        props["client_version"] = pv
        if cv is not None:
            props["cli_version"] = cv
        props["authenticated"] = ident.get("authenticated", False)
        if not ident.get("authenticated"):
            props["$process_person_profile"] = False
        if ident.get("workspace_id"):
            props["workspace_id"] = ident["workspace_id"]
        if ident.get("customer_id"):
            props["team"] = ident["customer_id"]
            props["$groups"] = {"team": ident["customer_id"]}
        if ident.get("email"):
            props["$set"] = {"email": ident["email"]}
        props["$lib"] = "probe-plugin"
        props["$lib_version"] = pv or "unknown"
        batch.append(
            {
                "event": e["event"],
                "distinct_id": ident["distinct_id"],
                "properties": props,
            }
        )
    return batch


def send_from_stdin() -> None:
    """Detached sender: enrich with identity + versions, POST, fail silent."""
    events = json.load(sys.stdin)
    if not isinstance(events, list) or not events:
        return
    cfg = read_cli_config()
    ident = resolve_identity(cfg)
    pv = plugin_version()
    # `probe --version` costs a subprocess; pay it only on the two per-session
    # events where a stale CLI is the question being asked.
    enrich_cli = any(
        e.get("event") in (EVENT_SESSION_STARTED, EVENT_SESSION_SUMMARY) for e in events
    )
    cv = cli_version() if enrich_cli else None

    payload = json.dumps({"api_key": POSTHOG_KEY, "batch": build_batch(events, ident, pv, cv)})
    req = urllib.request.Request(
        POSTHOG_HOST.rstrip("/") + "/batch/",
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=SEND_TIMEOUT).read()


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
