"""SDK exceptions mapped from the research-os error contract.

The API returns a FastAPI envelope ``{"detail": <string | object>}``. 409 carries
an object ``{message, existing_id, suggestion?, deleted?}``; everything else is a
string or a validation error list. See ``CONTRACT.md`` (Error contract).
"""

from __future__ import annotations

from typing import Any


class RosError(Exception):
    """Base for every client error."""

    def __init__(self, message: str, *, status: int | None = None, detail: Any = None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class TransportError(RosError):
    """Network failure, timeout, or unreachable host (no HTTP status)."""


class AuthError(RosError):
    """401 - missing / invalid / revoked / expired credential, or membership gone."""


class ScopeError(RosError):
    """403 - valid credential but insufficient scope/role."""


class NotFoundError(RosError):
    """404 - absent, other-tenant, or archived/deleted on a hidden read."""


class ConflictError(RosError):
    """409 - natural-key conflict, archived-experiment push, deleted-run append,
    or a lifecycle violation. The detail is an object; the useful fields are
    surfaced as attributes."""

    def __init__(self, message: str, *, detail: Any = None):
        super().__init__(message, status=409, detail=detail)
        self.existing_id: str | None = None
        self.suggestion: str | None = None
        self.deleted: bool = False
        if isinstance(detail, dict):
            self.existing_id = detail.get("existing_id")
            self.suggestion = detail.get("suggestion")
            self.deleted = bool(detail.get("deleted", False))


class ValidationError(RosError):
    """422 - request validation (missing hypothesis, caps, malformed cursor, ...)."""


class ServerError(RosError):
    """5xx - the API or a database is down / behind schema."""


class CapabilityUnavailable(RosError):
    """A client surface exists but the deployed backend lacks the capability."""

    def __init__(self, capability: str, message: str | None = None):
        self.capability = capability
        super().__init__(message or f"research-os capability is unavailable: {capability}")


_BY_STATUS: dict[int, type[RosError]] = {
    401: AuthError,
    403: ScopeError,
    404: NotFoundError,
    409: ConflictError,
    422: ValidationError,
}


def _detail_message(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        return str(detail.get("message") or detail)
    return str(detail)


def error_for(status: int, detail: Any) -> RosError:
    """Build the right exception for an HTTP status + parsed ``detail``."""
    if status == 409:
        return ConflictError(_detail_message(detail), detail=detail)
    cls = _BY_STATUS.get(status)
    if cls is None:
        cls = ServerError if status >= 500 else RosError
    return cls(_detail_message(detail), status=status, detail=detail)
