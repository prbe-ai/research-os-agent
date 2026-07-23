"""Small, fail-open client-version header contract.

These headers are telemetry, never identity or authorization.  Keeping their
validation here gives the CLI sender and hosted MCP receiver the same bounded
wire shape without teaching the generic SDK that it is always a CLI.
"""

from __future__ import annotations

import re

CLIENT_KIND_HEADER = "X-Probe-Client"
CLIENT_VERSION_HEADER = "X-Probe-Client-Version"

_CLIENT_KINDS = frozenset({"cli", "plugin"})
_MAX_VERSION_LENGTH = 64
_VERSION_RE = re.compile(
    r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def client_version_headers(kind: object, version: object) -> dict[str, str]:
    """Return a validated kind/version pair, or no telemetry on malformed input.

    The backend compares strict SemVer, so PEP 440-only forms such as
    ``0.8.0rc1`` and the source-tree fallback ``0.0.0.dev0`` intentionally
    produce no telemetry. Whitespace, control characters, header delimiters,
    and unbounded values are rejected rather than normalized.
    """

    if (
        not isinstance(kind, str)
        or kind not in _CLIENT_KINDS
        or not isinstance(version, str)
    ):
        return {}
    if not 1 <= len(version) <= _MAX_VERSION_LENGTH:
        return {}
    match = _VERSION_RE.fullmatch(version)
    if match is None:
        return {}
    prerelease = match.group(4)
    if prerelease is not None and any(
        identifier.isdigit()
        and len(identifier) > 1
        and identifier.startswith("0")
        for identifier in prerelease.split(".")
    ):
        return {}
    return {
        CLIENT_KIND_HEADER: kind,
        CLIENT_VERSION_HEADER: version,
    }
