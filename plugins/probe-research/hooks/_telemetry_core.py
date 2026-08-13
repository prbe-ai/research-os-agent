"""Shared client-telemetry core: the contract both surfaces must agree on.

ONE source of truth for everything that keeps CLI events (`wizard.*`,
`backfill.*`) and plugin events (`plugin.*`) joinable in PostHog: the
ingestion key and host, the killswitch spellings, the hosted-only gate, the
config/identity/machine-id readers and their cache paths, and the batch-entry
property names (which also match app/core/analytics.py and the dashboard).

TWO COPIES OF THIS FILE EXIST ON PURPOSE, and they must stay byte-identical:

    src/probe/cli/_telemetry_core.py                     (canonical — edit here)
    plugins/probe-research/hooks/_telemetry_core.py      (vendored — never edit)

The plugin hook runs under the SYSTEM python3 with no `probe` package
importable, so it cannot import this module from the package; it carries a
vendored copy instead. `make sync-telemetry-core` refreshes the copy, and
tests/test_telemetry_core_parity.py fails CI whenever the two files differ —
so a stale sync is a red build, not a silently split funnel.

STDLIB ONLY. This file must import nothing outside the standard library, in
either location. Any core change is plugin-visible at the plugin's next
release: add a line to the plugin CHANGELOG in the same PR.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
import uuid

# The same public write-only ingestion project the dashboard posts to (see
# dashboard/src/lib/analytics/posthog-config.ts for why embedding is fine; this
# file is mirrored into the public plugin repo, and that is acceptable for a
# capture-only key exactly as it is for the browser bundle).
POSTHOG_HOST = os.environ.get("PROBE_TELEMETRY_HOST") or "https://us.i.posthog.com"
POSTHOG_KEY = "phc_pCSs24bQtPaxoJ59PaTtTpJDS3dfzymfZeY74XQQ956K"
SEND_TIMEOUT = 3  # seconds; senders are detached/daemonized so this bounds nothing visible
ME_CACHE_TTL = 24 * 3600
DEFAULT_BASE = "https://api.research.prbe.ai"


def telemetry_disabled() -> bool:
    return (os.environ.get("PROBE_TELEMETRY") or "").strip().lower() in {
        "off",
        "0",
        "false",
        "no",
        "disabled",
    }


def hosted_base_url(base_url: str | None) -> bool:
    """Whether client telemetry may fire for this backend.

    The self-host egress contract (tests/selfhost/test_egress.py) promises the
    running system never calls back to the vendor; this extends that promise to
    the client surfaces. Gate on the RESOLVED base_url: an unset config
    resolves to the hosted default downstream and MUST emit — fresh installs
    are the population the install funnel exists to measure.

    Hostname matching is dot-boundary, never substring: `api.research.prbe.ai`
    passes, `evil-notprbe.ai` does not, and ports/paths cannot spoof it.
    HTTPS only: the hosted service is https-only, and the identity resolver
    sends a bearer token to this URL — an `http://` spelling that passed the
    gate would put that token on the wire in cleartext.
    """
    if base_url is None or base_url == "":
        return True  # resolves to DEFAULT_BASE downstream
    try:
        parts = urllib.parse.urlsplit(base_url)
    except Exception:
        return False
    if parts.scheme != "https":
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    return host == "prbe.ai" or host.endswith(".prbe.ai")


def effective_base_url(cfg: dict) -> str:
    """The base_url the client is actually talking to: env > config > default.

    PROBE_BASE_URL outranks the config file everywhere else in the client
    (sdk/config.resolve), so the gate and the identity resolver must honor it
    too — a self-hoster configured via env must not leak to the vendor just
    because the config file is empty.
    """
    return (
        os.environ.get("PROBE_BASE_URL") or cfg.get("base_url") or DEFAULT_BASE
    ).rstrip("/")


def effective_token(cfg: dict) -> str | None:
    """The bearer the client would use: env > config, matching sdk resolve()."""
    return (
        os.environ.get("PROBE_TOKEN")
        or os.environ.get("PROBE_MCP_TOKEN")
        or cfg.get("token")
        or cfg.get("mcp_token")
    )


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


def _state_home() -> str | None:
    """The telemetry state dir, or None when no home can be resolved.

    None (arbitrary-uid containers where ``~`` does not expand) means SKIP
    persistence entirely — minting under a literal ``./~/`` directory in the
    user's project is worse than an ephemeral id. Known accepted skew: an
    unwritable/absent state dir means every process mints its own ephemeral
    id, so one such machine can appear as several — dashboards counting
    machines should treat it as a floor, not an exact denominator.
    """
    base = os.environ.get("XDG_STATE_HOME")
    if not base:
        home = os.path.expanduser("~")
        if home == "~":
            return None
        base = os.path.join(home, ".local", "state")
    return os.path.join(base, "probe-telemetry")


def _write_private(path: str, data: str) -> None:
    """Atomic 0600 write under a 0700 dir — identity data must never be
    world-readable on shared machines. Unique tmp name: two cold-start
    processes (wizard sender + freshly installed plugin hook) race exactly on
    the first session."""
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    os.chmod(parent, 0o700)
    tmp = f"{path}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise
    os.replace(tmp, path)


def machine_id() -> str:
    """Stable anonymous fallback id, minted once per machine/user.

    Shared between the CLI and the plugin hook (same state file), so the
    "installed but never logged in" machine is one id across both surfaces —
    and stamped as a plain property on every event, it is the cross-surface
    join key that works without person merges (anonymous events are personless
    and there is no alias from `machine:<id>` to the post-login user UUID).

    Minting is first-writer-wins (O_EXCL + re-read): the wizard's sender and
    the plugin's SessionStart sender race on the very first session, and a
    last-write-wins mint would split the join key on exactly the session the
    install funnel exists to measure.
    """
    home = _state_home()
    if home is None:
        return uuid.uuid4().hex  # ephemeral: no resolvable home to persist in
    path = os.path.join(home, "machine_id")

    def _read() -> str | None:
        try:
            with open(path, encoding="utf-8") as f:
                mid = f.read().strip()
            return mid or None
        except Exception:
            return None

    existing = _read()
    if existing:
        return existing
    mid = uuid.uuid4().hex
    try:
        os.makedirs(home, exist_ok=True)
        os.chmod(home, 0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(mid)
        return mid
    except FileExistsError:
        return _read() or mid  # another process won the mint; use its id
    except Exception:
        return mid  # ephemeral id this session; still countable, just not stable


def resolve_identity(
    cfg: dict,
    *,
    fallback_customer_id: str | None = None,
    machine: str | None = None,
) -> dict:
    """{distinct_id, customer_id, workspace_id, authenticated} — fail-soft.

    Runs only off the hot path (a detached sender or a daemon thread), so the
    /v1/me call and its timeout never sit anywhere user-visible. The result is
    cached for 24h keyed on (base_url, token), so steady state is one network
    call per machine per day — and a token minted by login changes the key,
    which is what flips identity to the user UUID without any cache TTL wait.

    Token and base_url honor the same env-over-config precedence as
    sdk/config.resolve (PROBE_TOKEN/PROBE_MCP_TOKEN, PROBE_BASE_URL). The
    /v1/me call additionally requires the hosted gate: a bearer must never be
    sent to a URL telemetry would refuse to emit for. Pass `machine` (the id
    already stamped on the events) so a fallback distinct_id can never
    disagree with the events' machine_id property.
    """
    token = effective_token(cfg)
    base_url = effective_base_url(cfg)
    ws = cfg.get("workspace")
    workspace_id = ws.get("id") if isinstance(ws, dict) else None

    fallback = {
        "distinct_id": f"machine:{machine or machine_id()}",
        "customer_id": fallback_customer_id,
        "workspace_id": workspace_id,
        "authenticated": False,
    }
    if not token or not hosted_base_url(base_url):
        return fallback

    home = _state_home()
    cache_key = hashlib.sha256(f"{base_url}|{token}".encode()).hexdigest()[:16]
    cache_path = os.path.join(home, "identity.json") if home else None
    if cache_path:
        try:
            with open(cache_path, encoding="utf-8") as f:
                cached = json.load(f)
            age = time.time() - cached.get("fetched_at", 0)
            if (
                cached.get("key") == cache_key
                and 0 <= age < ME_CACHE_TTL  # a future fetched_at must not pin forever
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
            headers={"Authorization": f"Bearer {token}", "User-Agent": "probe-client-telemetry"},
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
        if cache_path:
            try:
                _write_private(cache_path, json.dumps(record))
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


# ---------------------------------------------------------------------------
# Batch building (pure; unit-tested from both surfaces)
# ---------------------------------------------------------------------------


def build_batch(
    events: list[dict],
    ident: dict,
    *,
    client_kind: str,
    lib: str,
    client_version: str | None,
    cli_version: str | None = None,
    machine: str | None = None,
) -> list[dict]:
    """Enrich observed events into PostHog batch entries.

    Deterministic given its args EXCEPT the `agent` default, which falls back
    to the PROBE_AGENT environment variable when the event did not stamp one.
    Property names match app/core/analytics.py (client_kind, client_version,
    team group, $process_person_profile) so client events share breakdowns and
    group rollups with the server-side events. An event carrying a `timestamp`
    key keeps it on the entry — emit-time stamping is what keeps funnels
    ordered when delivery is asynchronous.
    """
    batch = []
    for e in events:
        props = dict(e.get("properties") or {})
        props.setdefault("agent", os.environ.get("PROBE_AGENT") or "claude_code")
        props["client_kind"] = client_kind
        props["client_version"] = client_version
        if cli_version is not None:
            props["cli_version"] = cli_version
        if machine is not None:
            props.setdefault("machine_id", machine)
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
        props["$lib"] = lib
        props["$lib_version"] = client_version or "unknown"
        entry = {
            "event": e["event"],
            "distinct_id": ident["distinct_id"],
            "properties": props,
        }
        if e.get("timestamp"):
            entry["timestamp"] = e["timestamp"]
        batch.append(entry)
    return batch


def post_batch(entries: list[dict]) -> None:
    """POST one batch to PostHog. The wire shape is part of the shared
    contract — both surfaces must send the identical envelope or a future
    endpoint/header change drifts them apart."""
    payload = json.dumps({"api_key": POSTHOG_KEY, "batch": entries})
    req = urllib.request.Request(
        POSTHOG_HOST.rstrip("/") + "/batch/",
        data=payload.encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req, timeout=SEND_TIMEOUT).read()
