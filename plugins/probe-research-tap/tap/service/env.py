"""the environment contract: what a context, a hook and a runtime may carry, and report consent."""

from __future__ import annotations

#: What a request may carry as its `context` (plan X2): the environment the
#: action's (or `status.get`'s) child runs with, on top of the service's own
#: clean one. Where Probe and the agents keep their files, and how the CLI,
#: uv and what they run reach the network -- the desktop app's IMPORTED_ENV
#: (prbe-ai/desktop `src-tauri/src/wizard.rs`), the variables a Finder-started
#: app imports from the login shell. Never a credential by name: no token, no
#: credentials, no PYTHONPATH. `PROBE_BASE_URL` and `base_url` both select only
#: the endpoint, matching Finder's fixed imported environment. A name outside this set is
#: refused, never dropped. The service also allows each coding agent's
#: session-id variable, read from the harness registry (`session_env`; not
#: written here: this file is the tap's too, and names no harness): a request
#: from inside an agent's session names it, so `probe.core.run` refuses what it
#: refuses there (`probe session default`, the wizard actions in a
#: daemon-recorded session). An id, never a credential.
#: `tests/test_service_actions.py` keeps both covering every harness.
CONTEXT_ENV = frozenset(
    {
        "PROBE_CONFIG_PATH",
        # The opt-out of Probe's entries in each agent's sandbox config (service-only M3).
        "PROBE_SANDBOX_ACCESS",
        "CLAUDE_CONFIG_DIR",
        "CODEX_HOME",
        "PI_CODING_AGENT_DIR",
        "KIMI_CODE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "UV_TOOL_DIR",
        "UV_TOOL_BIN_DIR",
        "UV_CACHE_DIR",
        "PIPX_HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
        "UV_DEFAULT_INDEX",
        "UV_INDEX",
        "UV_NATIVE_TLS",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "NPM_CONFIG_REGISTRY",
        "PROBE_SESSION_STATE",
        "PROBE_SESSION_TRACKING",
        "PROBE_TELEMETRY",
        "PROBE_BASE_URL",
    }
)
#: The credentials a process may hold in its environment (`probe.sdk.settings.settings`'s
#: env table), which outrank the CLI config's stored ones and which a request's
#: `context` never carries: a caller holding one does the work itself, since a
#: child of the service would act with another credential (or none).
#: `hooks/_service_call.py` keeps a copy (tests/test_service_cli_surfaces.py).
CREDENTIAL_ENV = frozenset(
    {"PROBE_TOKEN", "PROBE_MCP_TOKEN", "PROBE_SERVICE_TOKEN", "PROBE_INGEST_TOKEN", "PROBE_HMAC_SECRET"}
)
#: `base_url` reaches the child as this variable (`probe.sdk.settings.settings.resolve`).
BASE_URL_ENV = "PROBE_BASE_URL"
#: `1` on a child the Probe service starts for a FENCED caller (a sandboxed agent, another network
#: namespace: `policy.confined`), else `0`: `action.run` sets it, and `cli.run` for a command it
#: runs. Never the caller's: not in CONTEXT_ENV, so a request's `context` cannot name it.
#: `probe uninstall` reads it (`probe.cli.install.uninstall.run`): a fenced caller never discards
#: unsent research, and never forces.
CALLER_FENCED_ENV = "PROBE_CALLER_FENCED"
#: Never a fenced caller's (service-only D2, #4274): where a child sends the stored login (its
#: endpoint, the config naming one) and through what (proxies, CA files: `SSL_CERT_FILE` replaces
#: the trusted CAs). A fenced caller's child takes the machine's own instead (its login shell's,
#: its CLI config). The one list: `probe.service.actions.caller.Fence` and the delivery service read it.
FENCE_ENDPOINT_NAMES = frozenset({
    "PROBE_CONFIG_PATH", "PROBE_BASE_URL", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy",
    "https_proxy", "no_proxy", "all_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS", "GIT_SSL_CAINFO",
})
MAX_CONTEXT_VALUE = 4096

#: `hooks.run`'s `lane`s and the hook event each one serves: `hooks/dispatch.py`
#: LANE_EVENT, the argument each frozen hooks.json entry passes its dispatcher, and
#: the lanes only `harness_io.py --event` runs (Kimi Code's TurnStarted); the
#: two are held equal (tests/test_service_hooks_run.py).
HOOK_LANES = {
    "session-start": "SessionStart",
    "prompt": "UserPromptSubmit",
    "pre-compact": "PreCompact",
    "pre-tool": "PreToolUse",
    "post-tool": "PostToolUse",
    "session-end": "SessionEnd",
    "stop": "Stop",
    "turn-started": "TurnStarted",
}
#: `hooks.run {routes: "service"}` (codex-capture-fixes PR 3): the service runs
#: the event from its own copy of Probe's routes, for a frozen hook shim
#: (`service/hooks/proxy/DESIGN.md` holds the v1 contract). `routes` is the field
#: that tells this shape from the `plugin_root` one.
HOOK_ROUTES_SERVICE = "service"
#: The proxy contract's version: a request or queue file names it as `v` (1 when left out).
HOOK_PROXY_V = 1
#: `hooks.run {routes: "service", v: 2}`: an extension's event (pi), answered with actions (`hook_actions`).
#: Not frozen (the extension updates with the CLI); v1 above is untouched.
HOOK_PROXY_V2 = 2
#: v2's `reply`: the answer carries `actions` beside v1's four keys (`hook_actions`).
HOOK_REPLY_ACTIONS = "actions"
#: A proxied event's name (the harness's own `hook_event_name`) is at most this long.
MAX_HOOK_EVENT_NAME = 64
#: `hooks.run`'s `timeout_ms` when the caller names none: plan D28's wait, the
#: one a dispatcher gives a warm host before it runs the event itself.
HOOK_WAIT_MS = 300
MAX_HOOK_WAIT_MS = 60_000
#: `hooks.run`'s `env` (the session's environment, as a harness hands its
#: hooks theirs): at most this many variables, each value at most this long,
#: as a recorder's (`recorders.MAX_ENV`).
MAX_HOOK_ENV = 200
MAX_HOOK_ENV_VALUE = 8192
MAX_HOOK_PATH = 4096
#: `hooks.queue`: one queued event, as JSON, is at most this big (its payload
#: and environment included: `hooks/dispatch.py` MAX_QUEUED_BYTES), and its id
#: is the dispatcher's: 32 lowercase hex digits.
MAX_HOOK_QUEUE_ITEM = 256 * 1024
MAX_HOOK_QUEUE_ENV = 1024
HOOK_EVENT_ID_LEN = 32
#: `hooks.queue`'s `env` keeps only what a record-keeping route reads (the rest
#: is dropped before it is stored: the queue is a file on disk, and an agent's
#: environment carries every credential its session was given): these names,
#: the harness's, Probe's, the XDG folders' and the locale's by prefix, and
#: never a name that looks like a secret (HOOK_QUEUE_ENV_SECRET) but Probe's own
#: API key, which the CLI already keeps on disk (0600) and `probe notes sync`
#: may need. `hooks/dispatch.py` `queue_env` keeps the same (a test holds them).
HOOK_QUEUE_ENV_NAMES = frozenset(
    {
        "HOME", "PATH", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "TZ", "TMPDIR", "TERM",
        "PLUGIN_ROOT", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "UV_CACHE_DIR", "PIPX_HOME",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
        "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "NODE_EXTRA_CA_CERTS", "CURL_CA_BUNDLE",
        "GIT_SSL_CAINFO",
        # A fenced caller's queued event (service-only D2, `caller.Fence`): never a user site of its own.
        "PYTHONNOUSERSITE",
    }
)
HOOK_QUEUE_ENV_PREFIXES = ("PROBE_", "CLAUDE", "CODEX_", "KIMI_", "PI_", "XDG_", "LC_")
HOOK_QUEUE_ENV_SECRET = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "COOKIE", "AUTH")
HOOK_QUEUE_ENV_CREDENTIAL = "PROBE_TOKEN"


def proxy_userinfo(name: str, value: str) -> bool:
    """Is this a proxy variable whose URL carries a user (and a password):
    `http://user:secret@proxy:8080`, `user@proxy`? A credential, never stored."""
    if not name.upper().endswith("_PROXY"):
        return False
    return "@" in value.split("://", 1)[-1].split("/", 1)[0]


def hook_queue_env(env: dict) -> dict:
    """The part of a queued event's environment that is kept (HOOK_QUEUE_ENV_*,
    never a proxy URL with credentials in it)."""
    kept = {}
    for name, value in env.items():
        if proxy_userinfo(name, value):
            continue
        if name != HOOK_QUEUE_ENV_CREDENTIAL:
            upper = name.upper()
            if any(word in upper for word in HOOK_QUEUE_ENV_SECRET):
                continue
            if name not in HOOK_QUEUE_ENV_NAMES and not name.startswith(HOOK_QUEUE_ENV_PREFIXES):
                continue
        kept[name] = value
    return kept

#: `outbox.drain` / `box.watch`'s `env`: the caller's environment as far as
#: the SDK's drainer and box watcher read it (its PROBE_* settings and knobs,
#: where config and state live, the interpreter's own variables, the network's
#: proxy and certificates), so the child runs as the detached one the caller
#: would have forked. Never a credential (`secret_env`): the drainer delivers
#: with the stored login, as the outbox's ops already pin it, and a caller
#: holding a credential in its environment keeps forking its own drainer.
RUNTIME_ENV_NAMES = frozenset(
    {
        "PATH",
        "LANG",
        "TZ",
        "TMPDIR",
        "VIRTUAL_ENV",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
        "all_proxy",
    }
)
RUNTIME_ENV_PREFIXES = ("PROBE_", "XDG_", "PYTHON", "LC_", "CONDA_", "SSL_", "LD_", "DYLD_")
#: A PROBE_* name with one of these in it is a credential (PROBE_TOKEN,
#: PROBE_INGEST_TOKEN, PROBE_HMAC_SECRET, ...): never sent, never accepted.
SECRET_ENV_WORDS = ("TOKEN", "SECRET", "PASSWORD", "KEY")
#: `telemetry.send`'s `entries`: at most this many (the SDK sender's own queue).
MAX_TELEMETRY_ENTRIES = 256


def secret_env(name: str) -> bool:
    """A Probe credential's variable (`PROBE_TOKEN` and the like)."""
    return name.startswith("PROBE_") and any(word in name for word in SECRET_ENV_WORDS)


def runtime_env_name(name: str) -> bool:
    """A variable `outbox.drain` / `box.watch` take in `env`."""
    return (name in RUNTIME_ENV_NAMES or name.startswith(RUNTIME_ENV_PREFIXES)) and not secret_env(name)


#: `session.register`'s optional `report_env` (crash-report plan B2): whether
#: the registrant's own environment allows the Probe service to send the Probe
#: team an automatic diagnostic report about its session. A bool; absent from
#: an older registrant, which then counts as "no".
REPORT_ENV = "report_env"
_REPORTS_OFF = frozenset({"off", "0", "false", "no", "disabled"})


def report_consent(env) -> bool:
    """`report_env` for a registrant whose environment is `env` (a tap, pi
    through the tap): `PROBE_TELEMETRY` or `PROBE_DIAGNOSTICS` off, or a
    `PROBE_BASE_URL` that is not Probe's hosted service (https, `prbe.ai` or a
    subdomain), says no. The rule of `probe.analytics.report_policy.allowed(env)`
    with no CLI config, restated for the tap, which cannot import `probe`
    (tests/test_service_watch.py holds the two equal). The service takes the
    bool as said (it holds none of the registrant's variables), and the report
    child, handed `{}` for such a session, checks only the CLI config's
    backend: this is the one place the session's own opt-outs are read."""
    for name in ("PROBE_TELEMETRY", "PROBE_DIAGNOSTICS"):
        if (env.get(name) or "").strip().lower() in _REPORTS_OFF:
            return False
    base_url = env.get(BASE_URL_ENV) or ""
    if not base_url:
        return True
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(base_url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    return parts.scheme == "https" and (host == "prbe.ai" or host.endswith(".prbe.ai"))
