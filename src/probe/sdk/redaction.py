"""Sensitive-value scrubbing for captured payloads (parity F5).

Ported from ``integrations/miles.py`` so redaction has ONE implementation;
the Miles adapter folds onto this module when it rebases onto the outbox
(P6). Opt in with ``Client(redact=True)`` (this scrubber) or a callable of
your own. Applied at CAPTURE -- before bytes hit the journal or the wire --
because the journal commonly lives on shared storage.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_CREDENTIAL_URI = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "aws_access_key_id",
    "authorization",
    "auth_token",
    "access_token",
    "bearer_token",
    "cookie",
    "credential",
    "credentials",
    "id_token",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
    "wandb_key",
}
#: Tokenizer vocabulary entries, not credentials -- "_token" suffixes that
#: must SURVIVE scrubbing (an eos_token in a training config is data).
_MODEL_TOKEN_KEYS = {
    "bos_token",
    "cls_token",
    "eos_token",
    "mask_token",
    "pad_token",
    "sep_token",
    "stop_token",
    "unk_token",
}


def is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", key.lower()).strip("_")
    parts = set(normalized.split("_"))
    token_secret = normalized.endswith("_token") and normalized not in _MODEL_TOKEN_KEYS
    signed_secret = normalized.endswith(("_signature", "_sig"))
    return (
        normalized in _SENSITIVE_KEYS
        or token_secret
        or signed_secret
        or bool(parts & {"password", "secret", "credential", "credentials"})
    )


def scrub_string(value: str) -> str:
    """user:pass@ credentials in URIs, and sensitive query parameters."""
    scrubbed = _CREDENTIAL_URI.sub(r"\g<scheme><redacted>@", value)
    try:
        parsed = urlsplit(scrubbed)
    except ValueError:
        return scrubbed
    if not parsed.scheme or not parsed.netloc or not parsed.query:
        return scrubbed
    query = [
        (key, "<redacted>" if is_sensitive_key(key) else item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
    ]
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment)
    )


def default_scrub(value: Any, *, key: str = "") -> Any:
    """Recursive scrub: sensitive keys -> "<redacted>", strings de-credentialed,
    unserializable values repr()'d. Shape-preserving otherwise."""
    if is_sensitive_key(key):
        return "<redacted>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return scrub_string(value)
    if isinstance(value, dict):
        return {
            str(item_key): default_scrub(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [default_scrub(item, key=key) for item in value]
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return repr(value)
    return value
