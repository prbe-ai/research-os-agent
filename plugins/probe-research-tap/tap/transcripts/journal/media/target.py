"""where a pasted image may be PUT: the media door's answer, never a local or unresolved host."""

from __future__ import annotations
import ipaddress
import logging
import socket
from urllib.parse import urlsplit

log = logging.getLogger(__name__)


class Unresolved(Exception):
    """The PUT host did not resolve: no PUT is made, and the image waits like
    any network failure (bounded by MEDIA_RETRY_*), since every attempt asks
    the door for a fresh URL."""


def _resolve_host(host: str) -> list[str]:
    """Every address `host` resolves to, once (no PUT follows a host this
    client did not check). Raises Unresolved when it resolves to none."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError) as exc:
        raise Unresolved(type(exc).__name__) from None
    addresses = [info[4][0] for info in infos]
    if not addresses:
        raise Unresolved("no address")
    return addresses


def _local_address(address: str) -> bool:
    """Loopback, private, link-local (cloud metadata included), unspecified,
    multicast or reserved: never where a presigned storage URL points."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return True  # not an address this check understands: treated as local
    mapped = getattr(ip, "ipv4_mapped", None)
    ip = mapped or ip
    return (
        ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_unspecified
        or ip.is_multicast or ip.is_reserved
    )


def _local_host(host: str, *, resolve: bool) -> bool:
    """`localhost`, a local IP literal, or (with `resolve`) a name every one of
    whose addresses is local. Unresolved propagates."""
    host = host.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return _local_address(str(ipaddress.ip_address(host)))
    except ValueError:
        pass
    return resolve and all(_local_address(a) for a in _resolve_host(host))


class PutRefused(ValueError):
    """An upload answer this client will not follow: final for the image."""


def _put_target(
    answer: dict, data: bytes, base_url: str = ""
) -> tuple[str, dict[str, str]] | None:
    """(url, headers) of the media door's `upload` answer, or None when it is
    not one this client may follow: a PUT to an http(s) URL, string headers,
    never a credential header, a Content-Length (when given) that is exactly
    these bytes', and a URL that points where storage can be (`_storage_url`).
    Anything that cannot be parsed or checked is None too, never an error:
    a None is a final refusal for the image, so capture goes on."""
    try:
        url, headers = answer.get("url"), answer.get("headers")
        if answer.get("method") != "PUT" or not isinstance(url, str) or not isinstance(headers, dict):
            raise PutRefused("not a PUT answer")
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in headers.items()):
            raise PutRefused("headers are not strings")
        lowered = {k.lower(): v for k, v in headers.items()}
        if "authorization" in lowered or "cookie" in lowered:
            raise PutRefused("a credential header")
        if lowered.get("content-length", str(len(data))) != str(len(data)):
            raise PutRefused("a different Content-Length")
        _storage_url(url, base_url)
        return url, dict(headers)
    except Unresolved:
        raise
    except (ValueError, TypeError, UnicodeError) as exc:  # PutRefused included
        log.warning("pasted image: upload answer refused (%s); the image is not uploaded", _why(exc))
        return None


def _why(exc: Exception) -> str:
    return str(exc) if isinstance(exc, PutRefused) else f"unparseable URL ({type(exc).__name__})"


def _storage_url(url: str, base_url: str) -> None:
    """Raise PutRefused unless `url` may receive a pasted image's bytes.

    Under an https gateway the PUT is https. Its host is never `localhost` or
    an address that is loopback, private, link-local (169.254.169.254, the
    cloud metadata service), unspecified, multicast or reserved, as a literal
    or as every address it resolves to (resolved once, here), unless the
    gateway's own host is local or private: a development, test or
    self-hosted setup whose storage sits beside it. Otherwise a wrong or
    hostile answer could send the researcher's image into their own network.
    Unresolved propagates (a wait, not a refusal)."""
    parts = urlsplit(url)
    base = urlsplit(base_url) if base_url else None
    if parts.scheme not in ("https", "http"):
        raise PutRefused(f"scheme {parts.scheme or 'none'!r}")
    if base is not None and base.scheme == "https" and parts.scheme != "https":
        raise PutRefused("plain http under an https gateway")
    host, _port = parts.hostname, parts.port  # .port raises ValueError when malformed
    if not host or parts.username is not None or parts.password is not None:
        raise PutRefused("no host, or credentials in the URL")
    if not _local_host(host, resolve=True):
        return
    # A local target: only beside a gateway that is local too.
    base_host = base.hostname if base is not None else None
    try:
        allowed = bool(base_host) and _local_host(base_host, resolve=True)
    except Unresolved:
        allowed = False
    if not allowed:
        raise PutRefused(f"a local or private host ({host})")


def _media_refusal(code: int) -> bool:
    """The media door's final answer for one image (D23: 403 switched off,
    409, 413 too large, 415 type, 422, 429 the team's daily budget; also a
    gateway that has no media route yet, 404/405). The batch still goes and
    the image keeps its placeholder; 401 and 408 are not about the image."""
    return 400 <= code < 500 and code not in (401, 408, DELETED_STATUS)
from ..errors import DELETED_STATUS  # noqa: E402 -- pieces need each other; the front door loads this one first
