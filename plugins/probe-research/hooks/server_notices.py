"""SessionStart's server notices (plan T8, R9, R18, R19): the notices the Probe server holds for this
plugin, read once a session start (1 s at most, reused for FRESH_S), each shown once per computer to the
person (`systemMessage`) and once per session to the agent (`additionalContext`, quoted under its label).

Same ledger files as the CLI, the SDK and pi (`_server_ledger.py`, a copy of
`probe.foundation.server_messages.ledger`), so two plugins' hooks, a CLI and pi show a notice once.
A server without the route, or none reached, leaves the manifest's `message` (version_check) as before.
STDLIB ONLY, PYTHON 3.9, FAIL-OPEN.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import threading
import time
import urllib.request

#: `probe.foundation.server_messages.MESSAGES_PATH`.
MESSAGES_PATH = "/v1/client/messages"
#: The read's whole budget, DNS included (a thread joined this long): a session start never waits longer.
FETCH_TIMEOUT_S = 1.0
#: A list read this recently is reused, not read again.
FRESH_S = 60
#: After a read that failed (an older server, none reached), the next waits this long.
RETRY_AFTER_FAILURE_S = 300
FAILED_SUFFIX = ".failed"
#: When no read answers, the last list is shown from only while it is younger than this (the server may
#: have ended its notices since).
STALE_S = 86400
#: Claude Code's `-p` runs say "0" here: their `systemMessage` goes to a script, so the person's
#: once-per-computer showing is not spent there (a missing value reads as attended).
ATTENDED_ENV = "CLAUDE_CODE_SESSION_ATTENDED"
#: Control characters a terminal or banner would act on (the server allows none; a self-pointed
#: `PROBE_BASE_URL` could send them).
_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
#: Who asks: this plugin, at its own version (the server matches notices by client kind and version).
CLIENT_KIND = "plugin"
CLIENT_KIND_HEADER = "X-Probe-Client"
CLIENT_VERSION_HEADER = "X-Probe-Client-Version"
#: contracts' `Audience` values: who a notice is written for.
PERSON_AUDIENCES = ("person", "both")
AGENT_AUDIENCES = ("agent", "both")
_PLAIN_ID = re.compile(r"[A-Za-z0-9_.+-]{1,64}")
_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.+-]{1,40})?")
_MAX_TEXT = 500
HERE = os.path.dirname(os.path.abspath(__file__))


class Notices:
    """What one session start shows: the person's lines, the agent's, and the claims to commit once
    they went out (`commit`), or to give back when they did not (`release`)."""

    def __init__(self) -> None:
        self.person_lines: list = []
        self.agent_lines: list = []
        self.claims: list = []

    def commit(self) -> None:
        for claim in self.claims:
            claim.commit()
        self.claims = []

    def release(self) -> None:
        for claim in self.claims:
            claim.release()
        self.claims = []


def _load(name: str):
    """A sibling module by explicit path: a test may load this hook with the folder off `sys.path`."""
    key = "_probe_hooks." + name
    if key in sys.modules:
        return sys.modules[key]
    spec = importlib.util.spec_from_file_location(key, os.path.join(HERE, name + ".py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    spec.loader.exec_module(module)
    return module


def read_item(item: object) -> "dict | None":
    """One wire `ClientMessage`, as the hooks need it; None without a plain id or a sentence.
    `probe.foundation.server_messages.parse_client_message` reads the same fields (a test pins them)."""
    if not isinstance(item, dict):
        return None
    message_id, text, revision = item.get("message_id"), item.get("text"), item.get("revision", 0)
    if not isinstance(message_id, str) or not _PLAIN_ID.fullmatch(message_id):
        return None
    text = _CONTROL.sub("", text) if isinstance(text, str) else ""
    if not text.strip():
        return None
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        revision = 0
    audience = item.get("audience") if item.get("audience") in PERSON_AUDIENCES + AGENT_AUDIENCES else "both"
    return {"message_id": message_id, "revision": revision, "text": text[:_MAX_TEXT], "audience": audience}


def read_account() -> "tuple[str, str] | None":
    """(base URL, token) the ledger is keyed by, in the SDK's order (`sdk.settings.resolve`): `PROBE_TOKEN`,
    then the CLI config's `token`; a read-only MCP token only where neither exists."""
    core = _load("_telemetry_core")
    config = core.read_cli_config()
    token = (os.environ.get("PROBE_TOKEN") or config.get("token")
             or os.environ.get("PROBE_MCP_TOKEN") or config.get("mcp_token"))
    base_url = core.effective_base_url(config)
    if not isinstance(token, str) or not token or not base_url.startswith(("https://", "http://")):
        return None
    return base_url, token


def is_person_watching() -> bool:
    return os.environ.get(ATTENDED_ENV) != "0"


def fetch_list(base_url: str, token: str, version: str) -> "tuple[str | None, list] | None":
    """(set revision, items) the server holds for this plugin, or None (no route, no answer, not JSON, or
    not within FETCH_TIMEOUT_S). Never follows a redirect: the token goes to the configured API only."""
    headers = {"Authorization": "Bearer " + token, "Accept": "application/json", CLIENT_KIND_HEADER: CLIENT_KIND}
    if _VERSION.fullmatch(version or ""):
        headers[CLIENT_VERSION_HEADER] = version
    request = urllib.request.Request(base_url.rstrip("/") + MESSAGES_PATH, headers=headers)
    opener = _reads()._opener()
    answers: list = []

    def read() -> None:
        try:
            with opener.open(request, timeout=FETCH_TIMEOUT_S) as response:
                answers.append(json.loads(response.read().decode("utf-8")))
        except Exception:  # noqa: BLE001 -- an older server, none reached, a redirect: the manifest speaks
            pass

    worker = threading.Thread(target=read, daemon=True)
    worker.start()
    worker.join(FETCH_TIMEOUT_S)
    body = answers[0] if answers else None
    items = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(items, list):
        return None
    revision = body.get("set_revision")
    return (revision if isinstance(revision, str) else None), items


def collect(session_id: str, plugin_version: "str | None", now: "float | None" = None) -> Notices:
    """The notices to show at this session start, each claimed for its destination. Never raises."""
    out = Notices()
    try:
        _collect(out, session_id, plugin_version or "", time.time() if now is None else now)
    except Exception:  # noqa: BLE001 -- a notice is never worth a broken session start
        out.release()
        out.person_lines, out.agent_lines = [], []
    return out


def _collect(out: Notices, session_id: str, plugin_version: str, now: float) -> None:
    account = read_account()
    if account is None:
        return
    ledger_module = _load("_server_ledger")
    ledger = ledger_module.MessageLedger.from_account(*account)
    items = _load_items(ledger, account, plugin_version, now)
    for item in map(read_item, items or []):
        if item is None:
            continue
        key = ledger_module.make_message_key(item["message_id"], item["revision"])
        if item["audience"] in PERSON_AUDIENCES and is_person_watching():
            claim = ledger.claim(key, ledger_module.Destination.PERSON)
            if claim is not None:
                out.claims.append(claim)
                out.person_lines.append(_reads().render_server_banner(item["text"]))
        if item["audience"] in AGENT_AUDIENCES and session_id:
            claim = ledger.claim(key, ledger_module.Destination.AGENT, session_id)
            if claim is not None:
                out.claims.append(claim)
                out.agent_lines.append(_reads().render_server_text(item["text"]))


def _load_items(ledger, account: "tuple[str, str]", plugin_version: str, now: float) -> list:
    """The list to show from: the saved one while fresh, else one read (none within RETRY_AFTER_FAILURE_S
    of a failed one), else the saved one while not stale; a saved list only when this version asked for it
    (the server matches notices by version: an old plugin's "please update" is not the new one's)."""
    saved = ledger.load_list(CLIENT_KIND)
    if saved is not None and saved.client_version != (plugin_version or None):
        saved = None
    if saved is not None and 0 <= now - saved.fetched_at < FRESH_S:
        return saved.items
    failed = ledger.folder / f"messages-{CLIENT_KIND}{FAILED_SUFFIX}"
    try:
        has_failed_lately = 0 <= now - failed.stat().st_mtime < RETRY_AFTER_FAILURE_S
    except OSError:
        has_failed_lately = False
    listed = None if has_failed_lately else fetch_list(account[0], account[1], plugin_version)
    if listed is not None:
        ledger.save_list(listed[0], listed[1], CLIENT_KIND, now=now, client_version=plugin_version or None)
        try:
            failed.unlink()
        except OSError:
            pass
        return listed[1]
    if not has_failed_lately:
        try:
            ledger.folder.mkdir(parents=True, exist_ok=True)
            failed.touch()
        except OSError:
            pass
    return saved.items if saved is not None and 0 <= now - saved.fetched_at < STALE_S else []


def _reads():
    """The reads hook: its server texts (`mailbox.render_server_text` / `render_server_banner`'s twins) and
    its no-redirect opener."""
    return _load("reads_hook")
