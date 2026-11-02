"""ServerMessage: what the Probe server said when it answered with an error, read from any of its four
body shapes. STDLIB ONLY: the tap carries a copy (`plugins/probe-research-tap/tap/server_messages/`)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

try:  # the tap's copy sits beside its own generated copy of the vocabulary
    from .client_messages import Action, Audience, MessageCode, Severity
except ImportError:
    from probe.generated.client_messages import Action, Audience, MessageCode, Severity

#: A wire id, code or version is kept only when it is this plain: never a path, a line or markup.
_PLAIN = re.compile(r"[A-Za-z0-9_.+-]{1,64}")
#: The one link a message may carry (the server refuses any other: `app/capture/clients/messages`).
DOCS_URL_PREFIX = "https://docs.prbe.ai/"
#: Where FastAPI says a 422's bad value was; the field's own name follows it.
_REQUEST_PARTS = frozenset({"body", "query", "path", "header", "cookie"})


@dataclass(frozen=True)
class ServerMessage:
    """The server's sentence and the typed keys beside it. `message_id` is set only for a message
    from the server's registry (`is_registered`): those are the ones worth telling the coding agent."""

    text: str
    status: int | None = None
    code: str | None = None
    message_id: str | None = None
    severity: Severity = Severity.BLOCKING
    action: Action = Action.NONE
    audience: Audience = Audience.BOTH
    min_version: str | None = None
    revision: int = 0
    docs_url: str | None = None

    @property
    def is_registered(self) -> bool:
        return self.message_id is not None

    def to_fields(self) -> dict:
        """The typed keys as JSON values (`--json` errors, the Mac app's envelope), unset ones left out."""
        fields = {"code": self.code, "message_id": self.message_id, "action": self.action.value,
                  "severity": self.severity.value, "min_version": self.min_version, "docs_url": self.docs_url}
        return {key: value for key, value in fields.items() if value is not None}


def parse_server_message(status: int | None, body: object) -> ServerMessage:
    """The message in an error answer, whichever shape: flat `{detail: <sentence>, code, message_id,
    action, ...}`, nested `{"detail": {code, message, ...}}`, a plain string `detail`, or FastAPI's
    422 list (one readable sentence). `body` is the parsed JSON, or raw bytes / text."""
    if isinstance(body, (bytes, bytearray)):
        raw = bytes(body).decode("utf-8", "replace")
        try:
            body = json.loads(raw)
        except (ValueError, RecursionError):
            body = raw
    fields: dict = dict(body) if isinstance(body, dict) else {}
    detail = fields.get("detail") if isinstance(body, dict) else body
    if isinstance(detail, dict):
        fields.update(detail)  # nested keys win over any top-level twin
    code = _get_plain_value(fields.get("code"))
    return ServerMessage(
        text=describe_detail(detail),
        status=status,
        code=code,
        message_id=_get_plain_value(fields.get("message_id")),
        severity=_to_member(Severity, fields.get("severity"), Severity.BLOCKING),
        # A floor's body from before `action` was sent means update.
        action=_to_member(Action, fields.get("action"), Action.UPDATE if code == MessageCode.CLIENT_TOO_OLD else Action.NONE),
        audience=_to_member(Audience, fields.get("audience"), Audience.BOTH),
        min_version=_get_plain_value(fields.get("min_version")),
        revision=_to_revision(fields.get("revision")),
        docs_url=_to_docs_url(fields.get("docs_url")),
    )


def describe_detail(detail: object) -> str:
    """A `detail` as words, never a Python repr: the string, an object's `message` (else its `code`),
    or FastAPI's 422 list as `invalid request: name: Field required; ...`."""
    if detail is None:
        return ""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        for key in ("message", "code"):
            if isinstance(detail.get(key), str) and detail[key]:
                return detail[key]
    if isinstance(detail, list) and detail and all(isinstance(item, dict) for item in detail):
        return "invalid request: " + "; ".join(_describe_problem(item) for item in detail)
    return json.dumps(detail, sort_keys=True, default=str)


def _describe_problem(item: dict) -> str:
    location = item.get("loc") if isinstance(item.get("loc"), (list, tuple)) else []
    parts = [str(part) for part in location]
    if parts and parts[0] in _REQUEST_PARTS:
        parts = parts[1:]
    problem = item.get("msg") if isinstance(item.get("msg"), str) else "is not valid"
    return f"{'.'.join(parts)}: {problem}" if parts else problem


def _get_plain_value(value: object) -> str | None:
    return value if isinstance(value, str) and _PLAIN.fullmatch(value) else None


def _to_member(enum: type, value: object, default):
    """`value` as a member of `enum`; a value this client does not know reads as `default`."""
    try:
        return enum(value)
    except (ValueError, TypeError):
        return default


def _to_revision(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _to_docs_url(value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith(DOCS_URL_PREFIX) or len(value) > 300:
        return None
    return value if value.isprintable() and " " not in value else None
