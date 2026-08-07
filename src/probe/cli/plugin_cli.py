"""One source-aware interface for Claude Code and Codex plugin commands.

The two agents expose the same plugin lifecycle with a few spelling differences:
Claude uses ``install``/``uninstall``/``marketplace update`` while Codex uses
``add``/``remove``/``marketplace upgrade`` and can return JSON from ``list``.
Keeping those differences here prevents setup, doctor, capture, and updater from
each growing their own subprocess wrapper and verb table.
"""

from __future__ import annotations

import json
import shutil
import subprocess

from probe.cli import claude_cli

CLAUDE = "claude_code"
CODEX = "codex"


def binary_name(source: str) -> str:
    return "codex" if source == CODEX else "claude"


def available(source: str) -> bool:
    return shutil.which(binary_name(source)) is not None


def run(source: str, args: list[str], *, timeout: float) -> claude_cli.Result:
    """Run an agent CLI with captured output and closed stdin."""
    if source != CODEX:
        return claude_cli.run(args, timeout=timeout)
    binary = shutil.which("codex")
    if not binary:
        return claude_cli.Result(ok=False, detail="`codex` not found on PATH", reachable=False)
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, no shell
            [binary, *args],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return claude_cli.Result(
            ok=False, detail=f"timed out after {timeout:.0f}s", reachable=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return claude_cli.Result(ok=False, detail=str(exc), reachable=False)
    out = (completed.stdout or "").strip()
    err = (completed.stderr or "").strip()
    if completed.returncode != 0:
        return claude_cli.Result(ok=False, detail=err or out)
    return claude_cli.Result(ok=True, detail=out)


def list_plugins(source: str) -> claude_cli.Result:
    args = ["plugin", "list", "--json"] if source == CODEX else ["plugin", "list"]
    return run(source, args, timeout=claude_cli.LIST_TIMEOUT_S)


def add_marketplace(source: str, location: str) -> claude_cli.Result:
    return run(
        source,
        ["plugin", "marketplace", "add", location],
        timeout=claude_cli.REFRESH_TIMEOUT_S,
    )


def refresh_marketplace(source: str, marketplace: str) -> claude_cli.Result:
    verb = "upgrade" if source == CODEX else "update"
    return run(
        source,
        ["plugin", "marketplace", verb, marketplace],
        timeout=claude_cli.REFRESH_TIMEOUT_S,
    )


def install(source: str, plugin_id: str) -> claude_cli.Result:
    verb = "add" if source == CODEX else "install"
    return run(
        source,
        ["plugin", verb, plugin_id],
        timeout=claude_cli.INSTALL_TIMEOUT_S,
    )


def uninstall(source: str, plugin_id: str) -> claude_cli.Result:
    verb = "remove" if source == CODEX else "uninstall"
    return run(
        source,
        ["plugin", verb, plugin_id],
        timeout=claude_cli.INSTALL_TIMEOUT_S,
    )


def codex_mcp_auth_status(name: str) -> str | None:
    """Return Codex's auth status for one installed MCP, if observable."""
    result = run(CODEX, ["mcp", "list", "--json"], timeout=claude_cli.LIST_TIMEOUT_S)
    if not result.ok:
        return None
    try:
        servers = json.loads(result.detail)
    except (TypeError, ValueError):
        return None
    if not isinstance(servers, list):
        return None
    for server in servers:
        if isinstance(server, dict) and server.get("name") == name:
            status = server.get("auth_status")
            return str(status) if status is not None else None
    return None


def login_codex_mcp(name: str) -> claude_cli.Result:
    """Run Codex's supported OAuth flow for a plugin-provided MCP server."""
    return run(CODEX, ["mcp", "login", name], timeout=180.0)
