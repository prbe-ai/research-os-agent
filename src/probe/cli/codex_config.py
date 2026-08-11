"""The one table in ``$CODEX_HOME/config.toml`` this CLI owns.

Codex has no credential-helper hook. The Claude Code plugin ships a
``headersHelper`` that mints an ``Authorization`` header at connect time from the
token the wizard already holds, so Claude never sends anyone to a browser for
the MCP. A plugin-declared HTTP MCP under Codex can only say ``auth: "oauth"``,
which is a SECOND browser approval stacked on the one the wizard just ran --
and it is the step that fails, because it is a three-minute window on a page
nobody explained.

A user-level ``[mcp_servers.<name>]`` entry overrides the plugin-declared one
(same name, one row in ``codex mcp list``, ``auth_status`` flips from
``not_logged_in`` to ``bearer_token``), and it accepts a static header. So the
read token minted by the single approval can serve Codex exactly the way the
headers helper serves Claude Code, with the MCP still hosted by us.

Two Codex behaviours shape everything below, both verified against codex-cli
0.147.0:

* A literal ``bearer_token`` key is not ignored for a streamable HTTP server --
  Codex refuses to load its ENTIRE configuration ("bearer_token is not
  supported for streamable_http") and will not start. The supported spelling is
  ``http_headers``. Nothing here may ever emit ``bearer_token``.
* config.toml belongs to the user, and one syntax error in it takes Codex down
  the same way. Every write parses its own output before replacing anything,
  and a file that does not parse on the way IN is left alone -- a broken config
  is someone else's bug until we touch it, and ours forever after.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

#: What `codex mcp list --json` reports once a static header is in place. The
#: wizard treats it as "authenticated" and skips the OAuth flow.
BEARER_STATUS = "bearer_token"


class ConfigError(Exception):
    """A refusal to write, carrying the reason for the caller to surface.

    Never a crash: every caller has a working fallback (Codex's own OAuth), so
    the shortcut declining is a slower install, not a failed one.
    """


@dataclass(frozen=True)
class WriteResult:
    path: Path
    changed: bool
    #: The file's exact contents before this write, or None when it did not
    #: exist. Kept so a caller that cannot CONFIRM the result can undo it: a
    #: config we broke is not something to leave behind and fall back from,
    #: because "Codex will not start" outlives whatever we were installing.
    previous: str | None = None


def codex_home() -> Path:
    """``$CODEX_HOME``, else ``~/.codex`` -- the same resolution Codex uses."""
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def config_path() -> Path:
    return codex_home() / "config.toml"


def plugin_mcp_url(name: str, *, marketplace: str) -> str | None:
    """The URL the installed Codex plugin declares for `name`, if it can be read.

    Taken from the plugin's own cached manifest rather than a constant here, so
    the entry we register points at whatever that install actually uses -- a
    self-hosted deployment included. No manifest means no shortcut: registering
    a guessed URL would point Codex at the wrong server with a valid token.
    """
    root = codex_home() / "plugins" / "cache" / marketplace / name
    if not root.is_dir():
        return None
    for version_dir in sorted(root.iterdir(), reverse=True):
        manifest = version_dir / ".codex-plugin" / "plugin.json"
        try:
            declared = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        servers = declared.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        entry = servers.get(name)
        if isinstance(entry, dict) and isinstance(entry.get("url"), str):
            return entry["url"]
    return None


def _toml_string(value: str) -> str:
    """A TOML basic string. `json.dumps` escapes exactly what TOML needs here."""
    return json.dumps(value)


def _header_patterns(name: str) -> tuple[re.Pattern[str], re.Pattern[str]]:
    """Match `[mcp_servers.name]` and its sub-tables, quoted or bare.

    `codex mcp add` writes the bare spelling; a hand-edited file may quote it.
    Missing the quoted form would append a SECOND table for the same server.
    """
    escaped = re.escape(name)
    key = rf'(?:{escaped}|"{escaped}")'
    exact = re.compile(rf"^\s*\[\s*mcp_servers\s*\.\s*{key}\s*\]\s*(?:#.*)?$")
    child = re.compile(rf"^\s*\[\s*mcp_servers\s*\.\s*{key}\s*\.")
    return exact, child


def _table_span(lines: list[str], name: str) -> tuple[int, int] | None:
    """The half-open line range holding `name`'s table and any sub-tables."""
    exact, child = _header_patterns(name)
    start = next((i for i, line in enumerate(lines) if exact.match(line)), None)
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].lstrip()
        if stripped.startswith("[") and not child.match(lines[index]):
            end = index
            break
    return start, end


def _validated(text: str, *, what: str) -> None:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{what} is not valid TOML ({exc})") from exc


def _replace_span(text: str, span: tuple[int, int] | None, block: str) -> str:
    lines = text.splitlines(keepends=True)
    if span is None:
        prefix = text if not text or text.endswith("\n") else text + "\n"
        separator = "\n" if prefix.strip() else ""
        return f"{prefix}{separator}{block}"
    start, end = span
    return "".join(lines[:start]) + block + "".join(lines[end:])


def write_mcp_bearer(name: str, *, url: str, token: str, path: Path | None = None) -> WriteResult:
    """Point Codex's `name` MCP at `url` with a static Authorization header.

    Replaces the whole table rather than patching keys inside it: a leftover
    `bearer_token_env_var` from an earlier shape would otherwise sit alongside
    the header, and a leftover `oauth` key would keep Codex asking to log in.
    """
    target = path or config_path()
    existed = True
    try:
        original = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        original, existed = "", False
    except OSError as exc:
        raise ConfigError(f"could not read {target}: {exc}") from exc

    if original.strip():
        # Refuse a file that is already broken. Rewriting part of it would make
        # us the author of a config Codex cannot load.
        _validated(original, what=str(target))

    block = (
        f"[mcp_servers.{name}]\n"
        f"url = {_toml_string(url)}\n"
        f"http_headers = {{ Authorization = {_toml_string(f'Bearer {token}')} }}\n"
    )
    span = _table_span(original.splitlines(keepends=True), name)
    updated = _replace_span(original, span, block)
    previous = original if existed else None
    if updated == original:
        return WriteResult(path=target, changed=False, previous=previous)

    _validated(updated, what="the updated Codex config")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".probe.tmp")
    try:
        temporary.write_text(updated, encoding="utf-8")
        # The file now holds a credential. It may not have before.
        temporary.chmod(0o600)
        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ConfigError(f"could not write {target}: {exc}") from exc
    return WriteResult(path=target, changed=True, previous=previous)


def restore(result: WriteResult) -> None:
    """Undo a `write_mcp_bearer`, byte for byte.

    The verification after a write is not decoration: `codex mcp list` failing
    is exactly what a config Codex cannot load looks like, and at that point we
    are one step away from having bricked its CLI. Restoring is unconditional
    and best-effort -- an exception here would replace a recoverable state with
    a traceback over the top of it.
    """
    try:
        if result.previous is None:
            result.path.unlink(missing_ok=True)
        else:
            result.path.write_text(result.previous, encoding="utf-8")
    except OSError:
        pass


def remove_mcp_server(name: str, *, path: Path | None = None) -> WriteResult:
    """Drop the table again, for uninstall.

    Leaving it behind would point Codex at the hosted MCP with a token the
    uninstall just orphaned -- a server that reads as installed and answers 401.
    """
    target = path or config_path()
    try:
        original = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return WriteResult(path=target, changed=False)
    except OSError as exc:
        raise ConfigError(f"could not read {target}: {exc}") from exc

    if not original.strip():
        return WriteResult(path=target, changed=False)
    _validated(original, what=str(target))

    span = _table_span(original.splitlines(keepends=True), name)
    if span is None:
        return WriteResult(path=target, changed=False)
    lines = original.splitlines(keepends=True)
    updated = "".join(lines[: span[0]]) + "".join(lines[span[1] :])
    _validated(updated, what="the updated Codex config")

    temporary = target.with_name(target.name + ".probe.tmp")
    try:
        temporary.write_text(updated, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ConfigError(f"could not write {target}: {exc}") from exc
    return WriteResult(path=target, changed=True)
