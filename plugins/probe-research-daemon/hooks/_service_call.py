#!/usr/bin/env python3
"""Ask this machine's Probe service from a hook: one short call, or no.

Two hooks used to spawn detached work of their own on every event: the status
line's server read (`statusline_refresh.py`, a fetch per due tool call per
session) and the team note's sync (`version_check.py` at a session's start,
`team-note-sync.sh` at every Stop and at a session's end). Where a service
answers they ask it instead (`statusline.refresh`, `notes.sync`), and it runs
that work once per machine on its own schedule. They spawn it as before
whenever this says no:

    no service       no socket file (none runs), Windows, or a socket folder
                     that is not this user's alone
    no stand-in      this environment carries what a request's context cannot
                     (a credential: `PROBE_TOKEN`, ...), so the service's
                     child would not act as this hook's would
    no answer        an older service (no such method), a refusal (a caller
                     in a sandbox), a busy one, or nothing within TIMEOUT_S

It never starts the service, never raises and prints nothing. Stdlib only,
loaded by explicit path; `_service_paths.py` (the service's own `paths.py`,
byte for byte) says where the service listens.

    python3 _service_call.py notes-sync push|reconcile

is `team-note-sync.sh`'s: exit 0 when the service took the sync, 1 when the
hook must spawn its own.
"""

from __future__ import annotations

import json
import os
import socket
import sys

#: The whole exchange (connect, hello, one call): a hook's budget is small.
TIMEOUT_S = 0.5
#: What a hook hands the service of its environment (`protocol.CONTEXT_ENV`
#: less the per-session switches, which change neither job and would split
#: the machine's one job per session): where the config, the state, the
#: agents' folders are, and how the network is reached.
#: tests/test_service_cli_surfaces.py keeps it inside CONTEXT_ENV.
CONTEXT_ENV = (
    "PROBE_CONFIG_PATH", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "PI_CODING_AGENT_DIR", "KIMI_CODE_HOME",
    "XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "PROBE_TELEMETRY", "PROBE_BASE_URL",
)
#: A credential in the environment outranks the CLI config's stored token
#: (`_telemetry_core.effective_token`, `probe.sdk.config.resolve`); a request
#: never carries one, so with one set the hook does the work itself
#: (`protocol.CREDENTIAL_ENV`, held equal by tests/test_service_cli_surfaces.py).
CREDENTIAL_ENV = ("PROBE_TOKEN", "PROBE_MCP_TOKEN", "PROBE_SERVICE_TOKEN", "PROBE_INGEST_TOKEN", "PROBE_HMAC_SECRET")

_HERE = os.path.dirname(os.path.abspath(__file__))


def _paths():
    import importlib.util

    key = "_probe_hooks._service_paths"
    module = sys.modules.get(key)
    if module is None:
        spec = importlib.util.spec_from_file_location(key, os.path.join(_HERE, "_service_paths.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return module


def context(env=None) -> dict:
    env = os.environ if env is None else env
    return {name: env[name] for name in CONTEXT_ENV if env.get(name)}


def stands_in(env=None) -> bool:
    """Can the service's child act as this hook's would (no credential here)?"""
    env = os.environ if env is None else env
    return not any(env.get(name) for name in CREDENTIAL_ENV)


def call(method: str, params: dict, *, timeout: float = TIMEOUT_S):
    """`method`'s result (a dict), or None on anything else. Never raises."""
    if os.name != "posix":
        return None
    try:
        paths = _paths()
        path = paths.socket_path()
        if not os.path.exists(path):
            return None
        paths.require_private(paths.Path(path).parent)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(str(path))
            lines = [
                {"jsonrpc": "2.0", "id": 1, "method": "service.hello",
                 "params": {"client": "cli", "client_version": "hooks", "api": 1}},
                {"jsonrpc": "2.0", "id": 2, "method": method, "params": params},
            ]
            sock.sendall(b"".join(json.dumps(line).encode("utf-8") + b"\n" for line in lines))
            buffer = b""
            while True:
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    message = json.loads(line.decode("utf-8"))
                    if isinstance(message, dict) and message.get("id") == 1 and "error" in message:
                        return None
                    if isinstance(message, dict) and message.get("id") == 2:
                        result = message.get("result")
                        return result if isinstance(result, dict) else None
                chunk = sock.recv(65536)
                if not chunk or len(buffer) > 1 << 20:
                    return None
                buffer += chunk
        finally:
            sock.close()
    except Exception:  # noqa: BLE001 - a hook contracted to silence: the caller spawns its own
        return None


def notes_sync(mode: str, *, rules_for=(), feedback: bool = False, env=None) -> bool:
    """Hand the team note's sync to the service: True when it took it."""
    if not stands_in(env):
        return False
    params = {"mode": mode, "rules_for": list(rules_for), "feedback": bool(feedback)}
    found = context(env)
    if found:
        params["context"] = found
    answer = call("notes.sync", params)
    return isinstance(answer, dict) and answer.get("outcome") in ("scheduled", "queued")


def statusline_refresh(sid: str, plugin_root: str, *, agent=None, env=None) -> bool:
    """Hand a session's status-line read to the service: True when it took it."""
    if not stands_in(env):
        return False
    params = {"sid": sid, "plugin_root": plugin_root, "python": sys.executable}
    if agent:
        params["agent"] = agent
    found = context(env)
    if found:
        params["context"] = found
    answer = call("statusline.refresh", params)
    return isinstance(answer, dict) and answer.get("scheduled") is True


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) == 2 and argv[0] == "notes-sync" and argv[1] in ("push", "reconcile"):
        return 0 if notes_sync(argv[1]) else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
