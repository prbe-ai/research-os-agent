"""Protocol-2 and -3 source reservations and immutable delivery, shared with the tap.

Old tap processes cannot enumerate this namespace. One SQLite transaction owns
the reservation cursor, retained-event ordinal and pending body. The network is
outside the transaction. Acknowledgement and retiring pending bytes are atomic.
Pending bodies are never evicted on capacity pressure, authorization or poison.
The one exception is final by construction: the server saying the session was
deleted at its customer's request (`SessionDeleted`, `Journal.mark_deleted`).

PROTOCOL 3 is protocol 2 with each retained event also sent as an ATIF fragment
(`fragments`, built by the vendored engine module `atif_fragment`), and the
events themselves only when the server asked for them. Which protocol a stream
uses is settled once: a new stream starts on what the receipts answer offers
(`accepts`, see `negotiate`), the server pins it with the first accepted batch,
and from then on every batch of that stream uses it, whatever is offered later.
Once the server holds a session's stream on protocol 3, the session is kept
where taps that predate it cannot see it (`_FRAGMENT_SESSIONS`): those drain
every session in the journal, and would stage protocol-2 batches the server
refuses for a protocol-3 stream. Until then it stays where they see it, so that
one stream is all any tap on this machine starts for it.

Protocol 2 can be retired for NEW streams (the engine's
`SESSION_PROTOCOL2_NEW_STREAMS=false`). A session a tap staged on protocol 2
before that, and the server never took, is then refused at batch 0 for good:
the journal drops that batch and sends the session again, from its batch 0, on
protocol 3 (`Journal._restart_retired_protocol2`).

PASTED IMAGES (prbe-knowledge #646, research-os #2647). When the receipts answer
says `accepts.media` and this machine has not turned it off (the tap passes
`pasted_images`), an image the researcher pasted into their own prompt keeps
its placeholder (`{type, mimeType, bytes}`) plus the SHA-256 of its bytes, and
a protocol-3 fragment (version 2) names it as an image part. The bytes never
enter an event, a fragment or a batch: `stage` keeps them in this journal
(`media`, beside the pending batch that names them, under
MEDIA_SPOOL_CAP_BYTES) and `deliver` uploads each one through the gateway's
media door before it posts that batch. Which fragment version a stream builds
is negotiated like its protocol (`fragment_version_for`): the highest one the
server and this builder share, recorded in the session's state; a state that
predates it builds version 1.

A transcript line over MAX_SOURCE_LINE is skipped (read past, never held) and
stands as an `oversize_line` system event, so one giant paste no longer stalls
its session's capture.
"""

from __future__ import annotations

import base64
import errno
import fcntl
import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import sqlite3
import ssl
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from . import capy_sanitize, codex_sanitize, kimi_sanitize, pi_sanitize, sanitize
from .sanitize import PastedImage
from .secrets import redact_event
from .transcript import nesting_exceeds

log = logging.getLogger(__name__)

#: Upload protocols with streams, cursors and receipts (prbe-knowledge
#: `kb/session_receipts.py`). 2 sends the sanitized probe-events/1 `events`;
#: 3 sends one ATIF fragment per event (`fragments`), plus the events only when
#: the server asked for them as the stream started (`accepts.events`).
PROTOCOL_EVENTS = 2
PROTOCOL_FRAGMENTS = 3
RECEIPTED_PROTOCOLS = (PROTOCOL_EVENTS, PROTOCOL_FRAGMENTS)
#: The protocol of a stream nothing else is recorded for: every stream a tap
#: before protocol 3 started.
PROTOCOL_VERSION = PROTOCOL_EVENTS
#: The server's 409 `detail` when it will not take a protocol-3 batch 0: the
#: customer is not (or no longer) offered protocol 3, or the stream already
#: exists on another protocol. Matched on the text until the door names them
#: with reason constants. Only batch 0 restarts on protocol 2 (`Journal.deliver`).
PROTOCOL3_REFUSALS = ("protocol 3 not enabled", "protocol mismatch")
#: The server's 409 `detail` when it will not START a stream on protocol 2
#: (prbe-knowledge `kb/session_receipts.py` `PROTOCOL2_RETIRED`): protocol 2 is
#: retired for new sessions of a customer offered protocol 3, while a stream
#: the server already holds on 2 keeps being accepted. Only batch 0 of a
#: stream the server does not hold restarts, on protocol 3 (`Journal.deliver`).
PROTOCOL2_RETIRED = "protocol 2 retired"
#: Where the journal keeps sessions whose stream the server holds on protocol 3
#: (`protocol_version` 3 in the state, never a mere offer). A tap that predates
#: protocol 3 reads only `sessions` and drains EVERY session there, so a
#: protocol-3 stream it could see would get protocol-2 batches the server
#: refuses (409 `protocol mismatch`), each one an immutable pending body no
#: process could then deliver. A session only OFFERED protocol 3 stays in
#: `sessions`: hidden there, an older tap's or `probe import`'s gap sweep would
#: start a second stream for it (the server holds none yet), while visible it
#: drains the one stream (on protocol 2, which the first acknowledgment then
#: pins). The snapshots of sessions offered or pinned to protocol 3 carry their
#: own suffix, because an older tap deletes every `*.snapshot.jsonl` its own
#: sessions do not name.
_FRAGMENT_SESSIONS = "fragment_sessions"
_SNAPSHOT_SUFFIXES = {PROTOCOL_EVENTS: ".snapshot.jsonl", PROTOCOL_FRAGMENTS: ".snapshot-p3.jsonl"}
EMPTY_HASH = hashlib.sha256(b"").hexdigest()
MAX_BODY_BYTES = 1024 * 1024
#: Protocol 3's packing target. A fragment carries its event's text about
#: twice (its Line and its message or parts), and the events ride along while
#: the server asks for them, so a protocol-3 batch of the same events is about
#: twice the bytes: twice the target keeps the events per batch near protocol
#: 2's. The route's ceiling (SERVER_FRAGMENT_BATCH_LIMIT) bounds a lone record.
MAX_FRAGMENT_BODY_BYTES = 2 * MAX_BODY_BYTES

#: What the INGEST ROUTE actually accepts, from `app/ingestion/sessions_router.py`
#: (`MAX_BODY_BYTES = 2_000_000`; the ingress in front of it allows 25m, so the
#: app is the binding limit). MAX_BODY_BYTES above is this client's PACKING
#: target and is deliberately smaller -- batching to the server's exact ceiling
#: leaves no room for the envelope and turns a good batch into a 413.
#:
#: The two are separated because one event that does not fit the packing target
#: used to be fatal rather than merely awkward: with no room to pack it beside
#: anything, `stage` refused, the session never imported, and no retry could
#: change that -- while the server would have taken it happily. A lone event is
#: now allowed the whole route budget, less the envelope around it.
SERVER_BATCH_LIMIT = 2_000_000
#: Envelope overhead: eleven small scalars, a session/stream id pair, a cwd, a
#: sha256, and (on the first batch) provenance. Measured at well under 2KB;
#: 16KB is slack, not an estimate, because being wrong here costs a 413 on a
#: batch that cannot be split.
ENVELOPE_HEADROOM = 16 * 1024
MAX_SOLO_EVENT_BYTES = SERVER_BATCH_LIMIT - ENVELOPE_HEADROOM
#: The route's budget for a protocol-3 batch (the gateway's
#: `MAX_FRAGMENT_BODY_BYTES`): one event's text can ride three times, as the
#: fragment's message, its index Line, and the event while the server asks for
#: events. Protocols 1 and 2 keep SERVER_BATCH_LIMIT.
SERVER_FRAGMENT_BATCH_LIMIT = 6_000_000
MAX_SOLO_FRAGMENT_BYTES = SERVER_FRAGMENT_BATCH_LIMIT - ENVELOPE_HEADROOM
MAX_SOURCE_LINE = 64 * 1024 * 1024
#: The system event that stands in for a line over MAX_SOURCE_LINE (D16).
OVERSIZE_LINE_SUBTYPE = "oversize_line"
#: Pasted images: the types the engine stores (its IMAGE_MEDIA_TYPES), the
#: largest one the media door takes, and how many bytes of images waiting to
#: upload this journal holds at most (over it, an image keeps its placeholder).
IMAGE_MEDIA_TYPES = ("image/gif", "image/jpeg", "image/png", "image/webp")
MEDIA_MAX_BYTES = 5 * 1024**3
MEDIA_SPOOL_CAP_BYTES = 512 * 1024 * 1024
#: An image whose upload keeps failing for reasons that may pass (no network,
#: the door's 503, storage's 5xx) holds its batch back until it has failed
#: this many times over at least this long; then it is given up (the engine
#: shows it as not available) and the batch goes. Capture never stalls on one.
MEDIA_RETRY_MIN_ATTEMPTS = 3
MEDIA_RETRY_WINDOW_SECONDS = 3600
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")
DEFAULT_CAP_BYTES = 100 * 1024 * 1024
# Raw history can be much larger than its sanitized pending delivery. Its
# immutable disk copy needs space for the source, not the upload queue limit.
SNAPSHOT_FREE_RESERVE_BYTES = 100 * 1024 * 1024
_FIELDS = (
    "session_id",
    "batch_seq",
    "cwd",
    "events",
    "fragments",
    "fragment_version",
    "finalize",
    "protocol_version",
    "stream_id",
    "source_byte_start",
    "source_byte_end",
    "source_line_start",
    "source_line_end",
    "event_start",
    "event_end",
    "prefix_sha256",
    "provenance",
    "snapshot_byte_end",
    "snapshot_sha256",
)
#: The sanitizer modules a registry row can name (`capture.sanitizer`). A new
#: harness adds its module here and in the Makefile's sync-tap-core list.
_SANITIZER_MODULES = {
    "sanitize": sanitize,
    "codex_sanitize": codex_sanitize,
    "pi_sanitize": pi_sanitize,
    "kimi_sanitize": kimi_sanitize,
    "capy_sanitize": capy_sanitize,
}


def _harness(source: str):
    """The registry row for a capture source. KeyError for an unknown source:
    an upload never guesses a route or a sanitizer."""
    from . import harness_registry  # the copy beside this module (tap_core and the tap both carry one)

    return harness_registry.get_registry().get(source)


def _sanitizer(source: str):
    return _SANITIZER_MODULES[_harness(source).capture.sanitizer].sanitize_event


def _pasted_extractor(source: str):
    """The harness's reader of researcher-pasted images (`pasted_images`), or
    None for a harness that has none (its images keep their placeholders)."""
    return getattr(_SANITIZER_MODULES[_harness(source).capture.sanitizer], "pasted_images", None)


def _route(source: str) -> str:
    return _harness(source).route


def _fragment_module():
    """The vendored fragment builder, or None where it cannot run.

    Imported on use, never at module load, and any failure is contained:
    protocol 2 must keep working on an interpreter or install where the
    vendored engine code does not import (it is the engine's code, written
    for the engine's Python)."""
    try:
        from . import atif_fragment
    except Exception:  # noqa: BLE001
        return None
    return atif_fragment


def _builder_versions(module) -> tuple[int, ...]:
    """The fragment versions a vendored builder can build (one before v2)."""
    versions = getattr(module, "FRAGMENT_VERSIONS", None)
    return tuple(versions) if isinstance(versions, tuple) else (module.FRAGMENT_VERSION,)


def fragment_version_for(remote: dict) -> int | None:
    """The fragment version a stream builds, from a receipts answer: the
    highest one both the answer's `accepts.fragment_versions` and this
    builder hold (D21: 2 only where the server takes 2, else 1), or None when
    they share none, the answer offers none, or the builder does not import."""
    accepts = remote.get("accepts")
    versions = accepts.get("fragment_versions") if isinstance(accepts, dict) else None
    if not isinstance(versions, list):
        return None
    offered = {v for v in versions if type(v) is int}
    if not offered:
        return None
    module = _fragment_module()
    shared = offered.intersection(_builder_versions(module)) if module is not None else ()
    return max(shared) if shared else None


def negotiate(remote: dict) -> tuple[int, bool]:
    """(protocol, send events) for a NEW stream, from a receipts answer.

    Protocol 3 only when the answer offers it AND a fragment version this
    client builds (`fragment_version_for`), and the vendored builder imports
    here. No `accepts` at all is an older or self-hosted server: protocol 2.
    `send events` is the server's `accepts.events`, for protocol 3 only.
    """
    accepts = remote.get("accepts")
    if not isinstance(accepts, dict):
        return PROTOCOL_EVENTS, False
    protocols, versions = accepts.get("protocols"), accepts.get("fragment_versions")
    if not (
        isinstance(protocols, list)
        and any(type(p) is int and p == PROTOCOL_FRAGMENTS for p in protocols)
        and isinstance(versions, list)
    ):
        return PROTOCOL_EVENTS, False  # not offered: the builder is never imported
    if fragment_version_for(remote) is None:
        return PROTOCOL_EVENTS, False
    return PROTOCOL_FRAGMENTS, accepts.get("events") is True


def _record_terms(state: dict, remote: dict) -> None:
    """What a receipts answer lets this session's next batches carry, kept in
    its state: the fragment version (`fragment_version_for`; kept as it was
    when the answer names none this builder shares) and whether the server
    takes pasted images (`accepts.media is True`; an answer without `accepts`
    is a server that does not)."""
    accepts = remote.get("accepts")
    state["media"] = isinstance(accepts, dict) and accepts.get("media") is True
    version = fragment_version_for(remote) if isinstance(accepts, dict) else None
    if version is not None:
        state["fragment_version"] = version


def _stream_fragment_version(state: dict, module) -> int:
    """The fragment version a protocol-3 batch of this session is built in:
    the negotiated one, and 1 for a state recorded before versions were."""
    versions = _builder_versions(module)
    stored = state.get("fragment_version")
    if type(stored) is int and stored in versions:
        return stored
    return 1 if 1 in versions else module.FRAGMENT_VERSION


def _stream_protocol(state: dict) -> int:
    """The protocol a session's next batch uses.

    `protocol_version` is the stream's own, recorded once the server holds it
    (the first acknowledged batch, or an existing stream adopted from the
    receipts answer). Without it, a stream the server already holds was started
    by a tap that predates protocol 3; one it does not hold yet starts on what
    was offered (`protocol_offer`), and may still restart on protocol 2.
    """
    pinned = state.get("protocol_version")
    if pinned in RECEIPTED_PROTOCOLS:
        return pinned
    if state.get("last_seq", -1) >= 0:
        return PROTOCOL_EVENTS
    offer = state.get("protocol_offer")
    return offer if offer in RECEIPTED_PROTOCOLS else PROTOCOL_EVENTS


class _Mark(NamedTuple):
    """Where a batch stands after one more packed source line."""

    events: int
    end: int
    lines: int
    event_no: int
    size: int
    records: int


def _encoded_bytes(value) -> int:
    """Bytes `value` takes inside a canonical payload."""
    return len(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    )


def _record_bytes(additions: list[dict]) -> tuple[int, int]:
    """(fragments, events): what one source line adds to a protocol-3 batch,
    estimated from its events alone, so packing never builds a fragment twice.

    A fragment repeats its event's text at most about twice, plus a small
    fixed part (2x + 64 bytes held for every event of the four harnesses'
    goldens; 1.46x overall). An estimate a little high only packs a little
    less; the built batch is measured exactly and trimmed (`Journal.stage`).
    """
    events = _encoded_bytes(additions)
    return 2 * events + 64 * len(additions), events


def _fragment(module, event: dict, version: int) -> dict:
    """`fragment(event, version)`, and never no fragment: every event ordinal is sent.

    The builder isolates each event's failures itself; this is the guard for
    one it did not foresee. The stand-in keeps the ordinal (its Line's
    `line_no`, which the server checks for contiguity) and the error's class,
    and has no `kind`, so the engine's fold reads it as one unparsed event.
    """
    try:
        return module.fragment(event, version)
    except MemoryError:
        raise  # may pass; never send an empty fragment for it
    except Exception as exc:  # noqa: BLE001
        return {
            "v": version,
            "line": {"line_no": event.get("line_no"), "text": ""},
            "error": type(exc).__name__,
        }


# -- pasted images -------------------------------------------------------------------


def oversize_line_event(length: int) -> dict:
    """The probe-events/1 event standing in for a transcript line too long to
    read (over MAX_SOURCE_LINE, `length` bytes with its newline): skipped, so
    the session's capture goes on, and said, so the gap is not silent."""
    return {
        "type": "system",
        "subtype": OVERSIZE_LINE_SUBTYPE,
        "content": (
            f"[transcript line not sent: {length:,} bytes, over the "
            f"{MAX_SOURCE_LINE:,}-byte limit]"
        ),
    }


def _rest_of_line(handle, consumed: int) -> int | None:
    """Read past the rest of a line `consumed` bytes into, a chunk at a time,
    never holding it: its whole length with the newline, or None when the
    file ends first (a line its producer is still writing)."""
    while True:
        chunk = handle.readline(1024 * 1024)
        if not chunk:
            return None
        consumed += len(chunk)
        if chunk.endswith(b"\n"):
            return consumed


def _image_bytes_agree(media_type: str, data: bytes) -> bool:
    """Whether the bytes are the image type the transcript says they are."""
    if media_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if media_type == "image/gif":
        return data[:6] in (b"GIF87a", b"GIF89a")
    if media_type == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


def _decoded_image(image: PastedImage) -> bytes | None:
    """A pasted image's bytes when they may be uploaded: a type the engine
    stores, strict base64, the bytes of that type, 1 byte to MEDIA_MAX_BYTES.
    None keeps the placeholder."""
    if image.media_type not in IMAGE_MEDIA_TYPES or len(image.data) > 4 * (MEDIA_MAX_BYTES // 3 + 1):
        return None
    try:
        data = base64.b64decode(image.data, validate=True)
    except ValueError:  # binascii.Error included
        return None
    if not data or len(data) > MEDIA_MAX_BYTES or not _image_bytes_agree(image.media_type, data):
        return None
    return data


def _image_placeholders(retained: list) -> list[dict]:
    """The top-level image placeholders (`{type, mimeType, bytes}`) of a
    line's user events, in order: where a pasted image's SHA-256 goes."""
    found = []
    for event in retained:
        message = event.get("message") if isinstance(event, dict) and event.get("type") == "user" else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if (
                isinstance(block, dict)
                and block.get("type") == "image"
                and "source" not in block
                and type(block.get("bytes")) is int
                and isinstance(block.get("mimeType"), str)
            ):
                found.append(block)
    return found


def _attach_pasted(
    images: list[PastedImage], retained: list, spool: dict[str, tuple[str, bytes]], room: int,
) -> int:
    """Put each pasted image's SHA-256 on its placeholder; returns the spool
    room left.

    `images` (the harness's `pasted_images` of the raw line) and the line's
    placeholders pair up in order, and only when they agree exactly: as many
    of each, every encoded size and media type the same. Anything else
    attaches nothing, so no digest can land on another image's placeholder.
    An image that fails its own checks (`_decoded_image`), or would take the
    spool past its room, keeps its placeholder alone. The decoded bytes go
    into `spool` by digest; nothing is changed before every check is done.
    """
    placeholders = _image_placeholders(retained)
    if not images or len(images) != len(placeholders) or any(
        block["bytes"] != image.encoded or block["mimeType"] != image.media_type
        for image, block in zip(images, placeholders, strict=True)
    ):
        return room
    digests: list[str | None] = []
    for image in images:
        data = _decoded_image(image)
        digest = hashlib.sha256(data).hexdigest() if data is not None else None
        if digest is not None and digest not in spool:
            if len(data) > room:
                digest = None
            else:
                spool[digest] = (image.media_type, data)
                room -= len(data)
        digests.append(digest)
    for block, digest in zip(placeholders, digests, strict=True):
        if digest is not None:
            block["sha256"] = digest
    return room


def named_media(payload: dict) -> set[str]:
    """The SHA-256 of every pasted image a batch names: on its events' image
    blocks and its fragments' image parts."""
    named: set[str] = set()
    for item in payload.get("events") or ():
        raw = item.get("raw") if isinstance(item, dict) else None
        message = raw.get("message") if isinstance(raw, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict) and block.get("type") == "image":
                named.add(block.get("sha256"))
    for fragment in payload.get("fragments") or ():
        parts = fragment.get("parts") if isinstance(fragment, dict) else None
        for part in parts if isinstance(parts, list) else ():
            if isinstance(part, dict) and part.get("type") == "image":
                named.add(part.get("sha256"))
    return {d for d in named if isinstance(d, str) and _SHA256_HEX.match(d)}


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


class ReconciliationRequired(RuntimeError):
    def __init__(
        self, message: str, *, safe_message: str | None = None, status_code: int | None = None,
    ):
        super().__init__(message)
        self.safe_message = message if safe_message is None else safe_message
        self.status_code = status_code


class SourceRewritten(ReconciliationRequired):
    """The transcript's bytes changed inside a range already uploaded or
    reserved: its producer rewrote the file in place. Kimi Code does, on
    resume, when the file's `protocol_version` is older than its own (an
    equal-size rewrite included, so file size proves nothing).

    Final for this session on this machine: the journal keeps refusing it, so
    nothing is re-uploaded and the pending bytes are kept, until the server
    can take a replacement stream. The tap says so once.
    """


class UnsafePending(ReconciliationRequired):
    """A pending batch that today's redaction would change.

    `Journal.reconcile_pending` settles it against the server's receipt.
    """


def recoverable(exc: ReconciliationRequired) -> bool:
    """Whether `Journal.reconcile_pending` may be able to settle `exc`.

    An unsafe pending batch, or a 409 (the server may already hold this
    batch_seq, from a lost response or from another producer).
    """
    return isinstance(exc, UnsafePending) or exc.status_code == 409


class DeliveryPending(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = False, safe_message: str | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.safe_message = message if safe_message is None else safe_message


class ProtocolRestarted(DeliveryPending):
    """The server would not take this new session's batch 0 on its protocol,
    so the journal dropped it and the session starts again on `protocol`:
    protocol 3 refused -> 2 (`Journal._restart_refused_offer`), protocol 2
    retired -> 3 (`Journal._restart_retired_protocol2`). Nothing is lost and
    nothing waits: the caller stages again at once. A DeliveryPending
    (retryable) only so a caller that does not know it still keeps the
    session for its next pass."""

    def __init__(self, message: str, protocol: int = PROTOCOL_EVENTS):
        refused = PROTOCOL_EVENTS if protocol == PROTOCOL_FRAGMENTS else PROTOCOL_FRAGMENTS
        super().__init__(
            message + f"; the session restarts on protocol {protocol}",
            retryable=True,
            safe_message=(
                f"the server did not take protocol {refused} for this new session; "
                f"it is sent again on protocol {protocol}"
            ),
        )
        self.protocol = protocol


#: How the server says a session was deleted at its customer's request (the
#: engine's `POST /api/session-deletions`, passed through unchanged by the
#: gateway `app/ingestion/sessions_router.py`): HTTP 410 with
#: {"detail": {"reason": "session_deleted", ...}} from the upload door, and
#: `state: "deleted"` from the receipts read. Both are FINAL: the engine refuses
#: every later byte of that session, forever.
DELETED_STATUS = 410
DELETED_REASON = "session_deleted"
DELETED_STATE = "deleted"


class SessionDeleted(DeliveryPending):
    """The server deleted this session at its customer's request. Final.

    Raised from a `Journal` method, the journal has already dropped the
    session's pending batch and snapshot and marked it done
    (`Journal.mark_deleted`), so nothing stages or sends it again. Raised from
    `Wire.receipts`, nothing is recorded yet: the caller marks the journal it
    holds (`mark_deleted` is idempotent). It subclasses DeliveryPending, never
    retryable, only so a caller that does not know it still contains it;
    callers catch it first and log it once. Retrying it was the bug: the upload
    was refused before a byte was stored, every tick, for as long as the daemon
    ran.
    """

    def __init__(self, session_id: str, *, newly: bool = True):
        super().__init__(
            f"session {session_id} was deleted at the customer's request; "
            "its pending upload was dropped and it will not be sent again",
            retryable=False,
            safe_message="session was deleted at the customer's request; it is not uploaded",
        )
        self.session_id = session_id
        #: False when this journal had already recorded the deletion, so a
        #: caller that logs it says so once, not once per process start.
        self.newly = newly


def deleted_response(code: int, body: object, session_id: str | None = None) -> bool:
    """Whether an HTTP answer is the server's final 'this session was deleted'.

    Matched on the body's reason as well as the status: only the server's own
    statement may make this client drop a session's data. With `session_id`,
    a statement naming ANOTHER session is not one about this session.
    """
    detail = body.get("detail") if isinstance(body, dict) else None
    return (
        code == DELETED_STATUS
        and isinstance(detail, dict)
        and detail.get("reason") == DELETED_REASON
        and (session_id is None or detail.get("session_id") in (None, session_id))
    )


def _validate_history(state: dict, expected: tuple[int, str] | None) -> None:
    if expected is not None and (
        state.get("historical_end"), state.get("historical_hash")
    ) != expected:
        raise ReconciliationRequired("approved transcript snapshot changed during reconciliation")


def _request_failure(wire, code: int) -> str:
    """Displayable diagnostics, never an HTTP body, URL or exception string."""
    if code == 0:
        kind = getattr(wire, "last_request_error", None)
        return f"request failed ({kind})" if kind else "request failed without an HTTP response"
    guidance = {
        401: "capture credentials were rejected; reconnect this agent",
        403: "capture access was refused; check this agent's pairing",
        409: "transcript conflicts with accepted data; reconciliation required",
        422: "transcript was rejected; reconciliation required",
    }.get(code, "transcript request was not accepted")
    return f"HTTP {code}: {guidance}"


def _retryable_status(code: int) -> bool:
    return code in (408, 429) or 500 <= code < 600


def _network_interruption(exc: Exception) -> bool:
    """Only connectivity failures; malformed responses and local files stay errors."""
    if isinstance(exc, urllib.error.URLError) and isinstance(exc.reason, Exception):
        return _network_interruption(exc.reason)
    if isinstance(exc, ssl.SSLError):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, socket.gaierror)):
        return True
    return isinstance(exc, OSError) and exc.errno in {
        errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTDOWN, errno.EHOSTUNREACH,
        errno.ECONNABORTED, errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT,
        errno.EPIPE,
    }


def canonical_payload(payload: dict) -> bytes:
    return json.dumps(
        {k: payload[k] for k in _FIELDS if k in payload},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


#: Scrub passes before giving up on a fixed point. Real content settles in two.
_MAX_SCRUB_PASSES = 8


def _scrub_content(payload: dict) -> dict:
    """Scrub content without rewriting stream IDs, source hashes or cursors.

    Repeats until nothing changes. One pass is not always stable: a marker such
    as `<redacted:probe-token>` contains the word "token" and anchors the next
    value, and a shortened span can bring an anchor within reach of a value.
    `_require_safe_pending` scrubs once more and demands no change, so a body
    staged from a single pass could be refused by its own daemon forever.

    The keys are scrubbed independently (the scrubber joins text only across
    the items of one list), so a part already at its fixed point is unchanged
    by scrubbing it again with the rest.
    """
    content = {key: payload[key] for key in ('events', 'fragments', 'cwd', 'provenance') if key in payload}
    for _ in range(_MAX_SCRUB_PASSES):
        scrubbed, _rules = redact_event(content)
        if scrubbed == content:
            break
        content = scrubbed
    return payload | content


def _require_safe_pending(body: bytes) -> None:
    """An old immutable reservation cannot be silently assigned a new digest.

    It may already have reached the server before a lost response. Preserve
    the exact bytes and require receipt reconciliation instead of replaying a
    detectable credential or rewriting the accepted batch's identity.
    """
    payload = json.loads(body)
    if _scrub_content(payload) != payload:
        raise UnsafePending(
            'pending transcript contains credentials requiring redaction; '
            'reconcile its receipt before rebuilding the reservation'
        )


def prefix_hash(path: Path, end: int) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        left = end
        while left:
            chunk = handle.read(min(left, 1024 * 1024))
            if not chunk:
                raise ReconciliationRequired(
                    "source truncated inside an acknowledged or pending range"
                )
            digest.update(chunk)
            left -= len(chunk)
    return digest.hexdigest()


def complete_line_end(path: Path, end: int) -> int:
    """Find the last complete source record without loading a partial giant line."""
    with path.open("rb") as handle:
        while end:
            start = max(0, end - 64 * 1024)
            handle.seek(start)
            tail = handle.read(end - start)
            newline = tail.rfind(b"\n")
            if newline >= 0:
                return start + newline + 1
            end = start
    return 0


def state_root() -> Path:
    return Path(
        os.environ.get("PROBE_TRANSCRIPT_STATE_DIR", "~/.probe/transcripts-v2")
    ).expanduser()


class Wire:
    """The existing ingest routes and receipt read, using the paired credential."""

    def __init__(
        self, base_url: str, token: str, source: str, timeout: float = 30,
        context: ssl.SSLContext | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.source = source
        self.timeout = timeout
        # TLS trust. The CLI passes probe.sdk.tls.ssl_context() (REQUESTS_CA_BUNDLE
        # and friends); the tap's vendored copy cannot import it and passes None,
        # which is urllib's default.
        self.context = context
        self.last_request_retryable = False
        self.last_request_error = None

    def _request(self, path: str, body: bytes | None = None) -> tuple[int, dict]:
        self.last_request_retryable = False
        self.last_request_error = None
        request = urllib.request.Request(
            self.base_url + path,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "probe-transcript/2",
            },
        )
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.context
            ) as response:
                body = json.loads(response.read())
                if not isinstance(body, dict):
                    raise ValueError("transcript endpoint returned a non-object response")
                return response.status, body
        except urllib.error.HTTPError as exc:
            self.last_request_retryable = _retryable_status(exc.code)
            try:
                body = json.loads(exc.read(8192))
                return exc.code, body if isinstance(body, dict) else {"detail": "invalid error response"}
            except (OSError, ValueError):
                return exc.code, {"detail": "invalid error response"}
        except (OSError, ValueError) as exc:
            self.last_request_retryable = _network_interruption(exc)
            cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            self.last_request_error = type(cause).__name__
            return 0, {"detail": str(exc)}

    def receipts(self, session_id: str, *, after: int = -1, limit: int | None = None) -> dict:
        query = f"?after={after}&limit={limit}" if limit is not None else ""
        code, body = self._request(
            f"/ingest/v1/sessions/{self.source}/{session_id}/receipts{query}"
        )
        if deleted_response(code, body, session_id):
            raise SessionDeleted(session_id)
        # `protocol_version` is the stream's own (2 or 3), and 2 for a session
        # the server holds no stream for; `accepts` says what a new one may use.
        if code != 200 or body.get("protocol_version") not in RECEIPTED_PROTOCOLS:
            raise DeliveryPending(
                f"stream receipts unavailable (http {code}); nothing newly staged",
                retryable=_retryable_status(code) or (code == 0 and self.last_request_retryable),
                safe_message=(
                    "server returned an unsupported receipt protocol"
                    if code == 200 else _request_failure(self, code)
                ),
            )
        if (
            body.get("session_id") != session_id
            or body.get("source") != self.source
            or not body.get("customer_id")
        ):
            raise ReconciliationRequired("receipt identity does not match the requested transcript")
        return body

    def post(self, body: bytes) -> tuple[int, dict]:
        return self._request(f"/ingest/v1/sessions/{_route(self.source)}", body)

    def media(self, session_id: str, sha256: str, size_bytes: int, media_type: str) -> tuple[int, dict]:
        """Ask the gateway's media door where one pasted image goes
        (research-os `capture_session_media`): `{"status": "have"}`, or
        `{"status": "upload", "method": "PUT", "url", "headers", ...}`."""
        body = json.dumps(
            {"sha256": sha256, "size_bytes": size_bytes, "media_type": media_type},
            separators=(",", ":"),
        ).encode()
        return self._request(f"/ingest/v1/sessions/{self.source}/{session_id}/media", body)

    def put(self, url: str, data: bytes, headers: dict[str, str]) -> int:
        """PUT an image's bytes to the presigned storage URL the media door
        gave, with exactly its headers (the store refuses a body, length, type
        or checksum other than the signed ones). Never the bearer token: the
        URL is the credential. The HTTP status, or 0 without a response."""
        self.last_request_retryable = False
        self.last_request_error = None
        request = urllib.request.Request(url, data=data, headers=headers, method="PUT")
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self.context
            ) as response:
                response.read(8192)
                return response.status
        except urllib.error.HTTPError as exc:
            self.last_request_retryable = _retryable_status(exc.code)
            exc.close()
            return exc.code
        except (OSError, ValueError) as exc:
            self.last_request_retryable = _network_interruption(exc)
            cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            self.last_request_error = type(cause).__name__
            return 0


class Journal:
    def __init__(
        self,
        base_url: str,
        customer_id: str,
        source: str,
        *,
        directory: Path | None = None,
        cap_bytes: int = DEFAULT_CAP_BYTES,
    ):
        self.source = source
        self.customer_id = customer_id
        self.base_url = base_url.rstrip("/")
        self.cap_bytes = cap_bytes
        self._live_owners = {}
        namespace = hashlib.sha256(
            json.dumps([self.base_url, customer_id, source]).encode()
        ).hexdigest()
        self.directory = (directory or state_root()) / namespace
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db_path = self.directory / "state.sqlite3"
        self.conn = sqlite3.connect(self.db_path, isolation_level=None, timeout=5)
        if self.conn.execute("PRAGMA user_version").fetchone()[0] not in (0, 2):
            self.conn.close()
            raise ReconciliationRequired("unsupported transcript journal version; state preserved")
        os.chmod(self.db_path, 0o600)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.executescript(f"""
            CREATE TABLE IF NOT EXISTS sessions(session_id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS {_FRAGMENT_SESSIONS}(session_id TEXT PRIMARY KEY, state TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pending(session_id TEXT PRIMARY KEY, body BLOB NOT NULL, digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS media(
                session_id TEXT NOT NULL, sha256 TEXT NOT NULL, media_type TEXT NOT NULL,
                size INTEGER NOT NULL, bytes BLOB NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, first_failure REAL,
                PRIMARY KEY(session_id, sha256)
            );
        """)
        # `media` holds the bytes of the pasted images a session's pending
        # batch names, until `deliver` has uploaded them (or given them up).
        # A row never outlives its session's pending batch: every path that
        # retires one drops them (`_drop_media`), and `stage` sweeps rows an
        # older tap left behind when it delivered such a batch itself.
        # Still 2: taps that predate protocol 3 keep opening this journal for
        # their own protocol-2 streams, and never read the table above.
        self.conn.execute("PRAGMA user_version=2")

    def close(self):
        self.conn.close()
        for handle in self._live_owners.values():
            handle.close()
        self._live_owners.clear()

    def _try_owner(self, session_id: str):
        # Kernel ownership survives PID reuse and disappears on SIGKILL. Never
        # unlink lock files: another process may still hold the old inode.
        name = hashlib.sha256(session_id.encode()).hexdigest() + ".owner.lock"
        handle = (self.directory / name).open("a+b")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return None
        return handle

    def claim_live(self, session_id: str) -> bool:
        if session_id in self._live_owners:
            return True
        handle = self._try_owner(session_id)
        if handle is None:
            return False
        self._live_owners[session_id] = handle
        return True

    def pin_orphan(self, session_id: str, remote: dict) -> bool:
        """Freeze an observed prefix only while no local live producer owns it.

        The caller also checks wrapper liveness and source quiet time. This
        lock closes the missing/stale PID-file race with compatible daemons.
        A resumed producer finishes this immutable prefix before its new tail.
        """
        handle = self._try_owner(session_id)
        if handle is None:
            return False
        try:
            state = self.get(session_id)
            if state is None:
                raise ReconciliationRequired("unknown orphan session")
            self.ensure(
                session_id,
                Path(state["path"]),
                remote,
                historical=True,
                complete_lines_only=True,
            )
            return True
        finally:
            handle.close()

    @contextmanager
    def transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.execute("COMMIT")
        except BaseException:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
            raise

    def get(self, session_id: str) -> dict | None:
        for table in (_FRAGMENT_SESSIONS, "sessions"):
            row = self.conn.execute(
                f"SELECT state FROM {table} WHERE session_id=?", (session_id,)
            ).fetchone()
            if row:
                return json.loads(row[0])
        return None

    def _save(self, state: dict):
        # By the PINNED protocol, never the offer (see _FRAGMENT_SESSIONS): a
        # session moves once, when the server is known to hold it on 3.
        table, other = (
            (_FRAGMENT_SESSIONS, "sessions")
            if state.get("protocol_version") == PROTOCOL_FRAGMENTS
            else ("sessions", _FRAGMENT_SESSIONS)
        )
        if table == _FRAGMENT_SESSIONS:
            self._keep_snapshot_from_older_taps(state)
        self.conn.execute(
            f"INSERT INTO {table}(session_id,state) VALUES(?,?) "
            "ON CONFLICT(session_id) DO UPDATE SET state=excluded.state",
            (state["session_id"], json.dumps(state, separators=(",", ":"))),
        )
        self.conn.execute(f"DELETE FROM {other} WHERE session_id=?", (state["session_id"],))

    def _keep_snapshot_from_older_taps(self, state: dict) -> None:
        """Give a protocol-3 session's snapshot the name older taps never delete.

        An older tap that froze the session's history while it was still in
        its view (`pin_orphan`) named the copy `*.snapshot.jsonl`; once the
        session leaves its view, it deletes that copy as unreferenced. The same
        bytes get the protocol-3 name first: a hard link (no copy, atomic), or
        a durable copy where the filesystem refuses links. The old name is then
        unreferenced here too, and the next sweep removes it.
        """
        snapshot = state.get("snapshot_path")
        p2, p3 = _SNAPSHOT_SUFFIXES[PROTOCOL_EVENTS], _SNAPSHOT_SUFFIXES[PROTOCOL_FRAGMENTS]
        if not snapshot or not snapshot.endswith(p2):
            return
        source = Path(snapshot)
        if not source.exists():
            return  # already gone: `ensure` freezes the history again
        target = source.with_name(source.name[: -len(p2)] + p3)
        if not target.exists():
            try:
                os.link(source, target)
            except OSError:
                fd, temporary = tempfile.mkstemp(dir=self.directory)
                try:
                    with os.fdopen(fd, "wb") as copy, source.open("rb") as original:
                        shutil.copyfileobj(original, copy, 1024 * 1024)
                        copy.flush()
                        os.fsync(copy.fileno())
                    os.replace(temporary, target)
                finally:
                    Path(temporary).unlink(missing_ok=True)
            directory_fd = os.open(self.directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        state["snapshot_path"] = str(target)

    def is_deleted(self, session_id: str) -> bool:
        state = self.get(session_id)
        return bool(state and state.get("deleted"))

    def mark_deleted(self, session_id: str, path: Path | None = None) -> bool:
        """The server deleted this session: forget everything still owed to it.

        One transaction drops the pending batch and marks the session done
        (`deleted`, finalized, history complete, no live capture), so no path
        stages or sends it again; the local snapshot copy of its source is
        removed after the commit. Idempotent. Returns True only on the call
        that changed something, so a caller can log it once. The researcher's
        own transcript file is theirs and is never touched.
        """
        with self.transaction():
            state = self.get(session_id)
            dropped = self.conn.execute(
                "DELETE FROM pending WHERE session_id=?", (session_id,)
            ).rowcount + self._drop_media(session_id)
            if state is not None and state.get("deleted") and not dropped:
                return False
            snapshot = (state or {}).get("snapshot_path")
            if state is None:
                state = {
                    "session_id": session_id,
                    "stream_id": str(uuid.uuid4()),
                    "source_byte_end": 0,
                    "source_line_end": 0,
                    "event_end": 0,
                    "last_seq": -1,
                    "prefix_sha256": EMPTY_HASH,
                    "path": str(path) if path is not None else "",
                    "provenance": {},
                    "digest_state": "not_requested",
                }
            state.update(
                deleted=True,
                finalized=True,
                historical_complete=True,
                live_enabled=False,
                finalize_requested=False,
                snapshot_path=None,
                error=None,
            )
            self._save(state)
        if snapshot:
            Path(snapshot).unlink(missing_ok=True)
        return True

    def _drop_media(self, session_id: str) -> int:
        """Drop the pasted images waiting to upload for a session whose
        pending batch is going (acknowledged, dropped, or the session deleted)."""
        return self.conn.execute("DELETE FROM media WHERE session_id=?", (session_id,)).rowcount

    def _refuse_deleted(self, session_id: str) -> None:
        if self.is_deleted(session_id):
            raise SessionDeleted(session_id, newly=False)

    def _deleted_remote(self, session_id: str, remote: dict, path: Path | None = None) -> None:
        """Settle a receipts answer that says the server deleted the session."""
        if remote.get("state") == DELETED_STATE:
            raise SessionDeleted(session_id, newly=self.mark_deleted(session_id, path))

    def pending(self, session_id: str) -> bytes | None:
        row = self.conn.execute(
            "SELECT body FROM pending WHERE session_id=?", (session_id,)
        ).fetchone()
        return bytes(row[0]) if row else None

    def _validate_source(self, state: dict, path: Path):
        body = self.pending(state["session_id"])
        end = json.loads(body)["source_byte_end"] if body else state["source_byte_end"]
        expected = json.loads(body)["prefix_sha256"] if body else state["prefix_sha256"]
        try:
            actual = prefix_hash(path, end)
        except ReconciliationRequired as exc:  # shorter than what was already sent
            raise SourceRewritten(str(exc)) from exc
        if actual != expected:
            raise SourceRewritten("source changed inside an acknowledged or pending range")

    def _reconcile_finalization(self, state: dict, remote: dict) -> bool:
        """Retire only a no-data completion whose exact boundary is accepted.

        Called after source validation, inside ensure's write transaction. A
        different producer can finalize the same prefix with another envelope;
        that does not authorize acknowledging different transcript event bytes.
        """
        stream = remote.get("stream") or {}
        protocol = _stream_protocol(state)
        if (remote.get("protocol_version") != protocol or remote.get("state") != "ready"
                or stream.get("finalized") is not True):
            return False
        row = self.conn.execute(
            "SELECT body,digest FROM pending WHERE session_id=?", (state["session_id"],)
        ).fetchone()
        if row is None:
            return False
        body, digest = bytes(row[0]), row[1]
        if hashlib.sha256(body).hexdigest() != digest:
            return False
        payload = json.loads(body)
        sequence, remote_sequence = payload.get("batch_seq"), stream.get("last_seq")
        if (
            payload.get("protocol_version") != protocol or payload.get("finalize") is not True
            or payload.get("events") not in (None, [])
            or payload.get("fragments") not in (None, [])
            or payload.get("session_id") != state["session_id"]
            or payload.get("stream_id") != state["stream_id"]
            or stream.get("stream_id") != state["stream_id"]
            or type(sequence) is not int or sequence != state["last_seq"] + 1
            or type(remote_sequence) is not int or remote_sequence < sequence
            or payload.get("prefix_sha256") != state["prefix_sha256"]
            or stream.get("prefix_sha256") != state["prefix_sha256"]
        ):
            return False
        for cursor in ("source_byte", "source_line", "event"):
            end = state[cursor + "_end"]
            if (type(end) is not int or end < 0
                    or any(type(item) is not int for item in (
                        payload.get(cursor + "_start"), payload.get(cursor + "_end"),
                        stream.get(cursor + "_end"),
                    ))
                    or payload.get(cursor + "_start") != end
                    or payload.get(cursor + "_end") != end
                    or stream.get(cursor + "_end") != end):
                return False
        removed = self.conn.execute(
            "DELETE FROM pending WHERE session_id=? AND digest=? AND body=?",
            (state["session_id"], digest, body),
        )
        if removed.rowcount != 1:
            return False
        self._drop_media(state["session_id"])
        state.update(last_seq=remote_sequence, finalized=True, error=None)
        if (state.get("historical_end") == state["source_byte_end"]
                and state.get("historical_hash") == state["prefix_sha256"]):
            state["historical_complete"] = True
        return True

    def ensure(
        self,
        session_id: str,
        path: Path,
        remote: dict,
        *,
        historical: bool,
        provenance: dict | None = None,
        cwd: str | None = None,
        complete_lines_only: bool = False,
        reconcile_body: bytes | None = None,
        expected_history: tuple[int, str] | None = None,
    ) -> dict:
        if (remote.get("customer_id"), remote.get("source"), remote.get("session_id")) != (
            self.customer_id,
            self.source,
            session_id,
        ):
            raise ReconciliationRequired(
                "refusing adoption across transcript destination identities"
            )
        # Final, and before anything else is looked at: a deleted session is
        # never reserved, snapshotted or staged again, whatever else is true.
        self._deleted_remote(session_id, remote, path)
        self._refuse_deleted(session_id)
        if remote.get("state") == "legacy":
            raise ReconciliationRequired(
                "legacy transcript coverage is unverified; reconcile before replay"
            )
        with self.transaction():
            if reconcile_body is not None and (
                self.get(session_id) is None or self.pending(session_id) != reconcile_body
            ):
                raise ReconciliationRequired("pending reservation changed before reconciliation")
            # A process may die between snapshot fsync and the SQLite commit.
            # The write lock proves no other producer is still creating these.
            states = self.sessions()
            for item in states:
                if item.get("historical_complete") and not self.pending(item["session_id"]):
                    snapshot_path = item.get("snapshot_path")
                    if snapshot_path:
                        Path(snapshot_path).unlink(missing_ok=True)
            referenced = {item.get("snapshot_path") for item in states}
            for suffix in _SNAPSHOT_SUFFIXES.values():
                for orphan in self.directory.glob("*" + suffix):
                    if str(orphan) not in referenced:
                        orphan.unlink(missing_ok=True)
            for orphan in self.directory.glob("tmp*"):
                if orphan.is_file():
                    orphan.unlink(missing_ok=True)
            state = self.get(session_id)
            if state is None:
                stream = remote.get("stream") or {}
                state = {
                    "session_id": session_id,
                    "stream_id": stream.get("stream_id") or str(uuid.uuid4()),
                    "source_byte_end": stream.get("source_byte_end", 0),
                    "source_line_end": stream.get("source_line_end", 0),
                    "event_end": stream.get("event_end", 0),
                    "last_seq": stream.get("last_seq", -1),
                    "prefix_sha256": stream.get("prefix_sha256", EMPTY_HASH),
                    "finalized": bool(stream.get("finalized")),
                    "path": str(path),
                    "provenance": provenance or {},
                    "digest_state": "not_requested",
                    "error": None,
                }
                if remote.get("stream"):
                    # The server holds this stream (another device, or a journal
                    # lost here): it keeps the protocol the server pinned it to,
                    # whatever is offered today, and sends events if asked today.
                    state["protocol_version"] = remote.get("protocol_version")
                    accepts = remote.get("accepts")
                    state["send_events"] = (
                        state["protocol_version"] == PROTOCOL_FRAGMENTS
                        and isinstance(accepts, dict)
                        and accepts.get("events") is True
                    )
                else:
                    state["protocol_offer"], state["send_events"] = negotiate(remote)
                _record_terms(state, remote)
                self._validate_source(state, path)
            else:
                self._validate_source(state, path)
                # Fresh terms for the next batches: a fragment version and
                # pasted images follow the server, batch by batch (the engine
                # folds mixed versions); the protocol never does.
                _record_terms(state, remote)
                stream = remote.get("stream")
                if stream and stream["stream_id"] != state["stream_id"]:
                    raise ReconciliationRequired(
                        "server stream changed; existing pending bytes retained"
                    )
                if stream:
                    served, pinned = remote.get("protocol_version"), state.get("protocol_version")
                    if pinned is not None and pinned != served:
                        raise ReconciliationRequired(
                            "server stream uses another upload protocol; pending bytes retained"
                        )
                    if pinned is None and (served == _stream_protocol(state) or not self.pending(session_id)):
                        # The server holds this stream, so its protocol is settled:
                        # record it. An unacknowledged protocol-3 batch 0 the server
                        # will refuse is left to delivery, which restarts it on 2.
                        state["protocol_version"] = served
                        if served != PROTOCOL_FRAGMENTS:
                            state["send_events"] = False
                reconciled = self._reconcile_finalization(state, remote)
                if reconcile_body is not None and not reconciled:
                    raise ReconciliationRequired("pending finalization does not match accepted coverage")
                if (
                    stream
                    and stream["last_seq"] > state["last_seq"]
                    and not self.pending(session_id)
                ):
                    # Another compatible device continued this exact source. Receipt adoption
                    # requires matching its acknowledged raw prefix, not cwd/name similarity.
                    if prefix_hash(path, stream["source_byte_end"]) != stream["prefix_sha256"]:
                        raise ReconciliationRequired(
                            "remote stream advanced over a different source prefix"
                        )
                    state.update(
                        {
                            k: stream[k]
                            for k in (
                                "last_seq",
                                "source_byte_end",
                                "source_line_end",
                                "event_end",
                                "prefix_sha256",
                                "finalized",
                            )
                        }
                    )
                state["path"] = str(path)
            if not historical:
                state["live_enabled"] = True
                state["finalize_requested"] = False
            if not state.get("cwd"):
                pending = self.pending(session_id)
                pending_cwd = json.loads(pending).get("cwd") if pending else None
                for candidate in (pending_cwd, cwd, (provenance or {}).get("source_cwd")):
                    if isinstance(candidate, str) and Path(candidate).is_absolute():
                        state["cwd"] = candidate
                        break
            remote_stream = remote.get("stream") or {}
            remote_snapshot = (
                remote_stream.get("snapshot_byte_end")
                if not remote_stream.get("finalized")
                else None
            )
            freeze_history = historical or remote_snapshot is not None
            extended = (
                historical
                and state.get("historical_complete")
                and path.stat().st_size > state.get("historical_end", 0)
            )
            pinned_end = (
                state.get("historical_end")
                if not state.get("historical_complete") else None
            )
            if remote_snapshot is not None and pinned_end is not None and (
                remote_snapshot != pinned_end
                or remote_stream.get("snapshot_sha256") != state.get("historical_hash")
            ):
                raise ReconciliationRequired(
                    "remote historical snapshot differs from the local pinned prefix"
                )
            if freeze_history and (
                not state.get("snapshot_path")
                or not Path(state["snapshot_path"]).exists()
                or extended
            ):
                before = path.stat()
                # Losing a copy (for example after a crash) must not silently
                # extend the previously pinned history to a later live tail.
                snapshot_end = (
                    remote_snapshot if remote_snapshot is not None
                    else pinned_end if pinned_end is not None else before.st_size
                )
                expected_snapshot_hash = (
                    remote_stream.get("snapshot_sha256") if remote_snapshot is not None
                    else state.get("historical_hash") if pinned_end is not None else None
                )
                if complete_lines_only and remote_snapshot is None and pinned_end is None:
                    # A killed producer may leave half a JSONL record. Freeze
                    # only complete records, so the original partial tail can
                    # be completed and consumed after this prefix is finalized.
                    snapshot_end = complete_line_end(path, snapshot_end)
                    pending = self.pending(session_id)
                    pending_end = json.loads(pending)["source_byte_end"] if pending else 0
                    if snapshot_end < max(state["source_byte_end"], pending_end):
                        raise ReconciliationRequired(
                            "complete source prefix ends before an acknowledged or pending cursor"
                        )
                if (
                    state["finalized"]
                    and state["source_byte_end"] >= snapshot_end
                    and not self.pending(session_id)
                ):
                    # Receipt/source validation above already proved coverage.
                    # A completed retry needs no second copy of its raw history.
                    snapshot_hash = prefix_hash(path, snapshot_end)
                    if expected_snapshot_hash and snapshot_hash != expected_snapshot_hash:
                        raise ReconciliationRequired(
                            "source differs from the pinned historical snapshot"
                        )
                    state.update(
                        historical_end=snapshot_end,
                        historical_hash=snapshot_hash,
                        historical_complete=True,
                    )
                    _validate_history(state, expected_history)
                    self._save(state)
                    return state
                if (
                    shutil.disk_usage(self.directory).free
                    < snapshot_end + SNAPSHOT_FREE_RESERVE_BYTES
                ):
                    raise DeliveryPending(
                        "not enough free disk space for the transcript snapshot; "
                        "source and pending uploads retained. Free disk space, then resume"
                    )
                snapshot = self.directory / (
                    f"{state['stream_id']}-{uuid.uuid4().hex}"
                    + _SNAPSHOT_SUFFIXES[_stream_protocol(state)]
                )
                fd, temporary = tempfile.mkstemp(dir=self.directory)
                try:
                    with os.fdopen(fd, "wb") as target, path.open("rb") as source:
                        left = snapshot_end
                        while left:
                            chunk = source.read(min(left, 1024 * 1024))
                            if not chunk:
                                raise ReconciliationRequired(
                                    "source truncated while freezing historical prefix"
                                )
                            target.write(chunk)
                            left -= len(chunk)
                        target.flush()
                        os.fsync(target.fileno())
                    after = path.stat()
                    if (before.st_ino, before.st_size, before.st_mtime_ns) != (
                        after.st_ino,
                        after.st_size,
                        after.st_mtime_ns,
                    ):
                        raise ReconciliationRequired(
                            "transcript changed while freezing its historical prefix"
                        )
                    snapshot_hash = prefix_hash(Path(temporary), snapshot_end)
                    if expected_snapshot_hash and snapshot_hash != expected_snapshot_hash:
                        raise ReconciliationRequired(
                            "copied source differs from the pinned historical snapshot"
                        )
                    os.replace(temporary, snapshot)
                    directory_fd = os.open(self.directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    Path(temporary).unlink(missing_ok=True)
                state["snapshot_path"] = str(snapshot)
                state["historical_end"] = snapshot.stat().st_size
                state["historical_hash"] = snapshot_hash
                state["historical_complete"] = bool(
                    state["finalized"] and state["source_byte_end"] >= state["historical_end"]
                )
                if extended:
                    state.update(digest_state="not_requested", pending_digest=None)
            _validate_history(state, expected_history)
            self._save(state)
            return state

    @staticmethod
    def _fragment_builder(protocol: int):
        """The fragment builder a protocol-3 stream needs, None for protocol 2.

        A stream on protocol 3 cannot fall back to 2 (the server pinned it), so
        an install whose builder will not import keeps the source for one that can.
        """
        if protocol != PROTOCOL_FRAGMENTS:
            return None
        module = _fragment_module()
        if module is None:
            raise DeliveryPending(
                "this session uploads ATIF fragments (protocol 3), and the fragment "
                "builder does not import here; source retained"
            )
        return module

    def stage(
        self,
        session_id: str,
        *,
        cwd: str,
        finalize: bool = False,
        historical_only: bool = False,
        max_body_bytes: int | None = None,
        solo_limit: int | None = None,
        packing_cap: int | None = None,
        require_no_pending: bool = False,
        expected_history: tuple[int, str] | None = None,
        pasted_images: bool = False,
    ) -> bytes | None:
        """Reserve the session's next batch, or return the one still pending.

        `pasted_images`: this machine lets images the researcher pasted be
        uploaded (the tap reads `defaults.pasted_images` in the Probe config
        each pass). They are, only where the session's state also says the
        server takes them (`media`) and, on protocol 3, the stream builds a
        fragment version that carries them.
        """
        with self.transaction():
            state = self.get(session_id)
            if state is None:
                raise ReconciliationRequired("session has no validated source reservation")
            if state.get("deleted"):
                raise SessionDeleted(session_id, newly=False)
            _validate_history(state, expected_history)
            if not state.get("cwd") and cwd and Path(cwd).is_absolute():
                state["cwd"] = cwd
            self._validate_source(state, Path(state["path"]))
            pending = self.pending(session_id)
            if pending:
                _require_safe_pending(pending)
                if require_no_pending:
                    raise ReconciliationRequired("another transcript delivery was reserved during reconciliation")
                return pending
            historic = bool(state.get("snapshot_path") and not state.get("historical_complete"))
            if historical_only and state.get("historical_complete"):
                return None
            path = Path(state["snapshot_path"] if historic else state["path"])
            if historic and prefix_hash(path, state["historical_end"]) != state["historical_hash"]:
                raise ReconciliationRequired("historical snapshot changed")
            protocol = _stream_protocol(state)
            # Protocol 3: the fragment builder, and whether events go beside it.
            module = self._fragment_builder(protocol)
            send_events = module is None or bool(state.get("send_events"))
            version = _stream_fragment_version(state, module) if module is not None else None
            # Rows an older tap left when it delivered the batch naming them
            # (it knows no `media`): no pending batch names them any more.
            self.conn.execute("DELETE FROM media WHERE session_id NOT IN (SELECT session_id FROM pending)")
            extract = _pasted_extractor(self.source) if pasted_images else None
            images_since = getattr(module, "IMAGES_SINCE", None) if module is not None else None
            if extract is not None and state.get("media") is True and (
                module is None or (images_since is not None and version >= images_since)
            ):
                spool: dict[str, tuple[str, bytes]] = {}
                room = MEDIA_SPOOL_CAP_BYTES - self.conn.execute(
                    "SELECT COALESCE(sum(size),0) FROM media"
                ).fetchone()[0]
            else:
                extract = None
            # A lone event may use the whole ROUTE budget (the protocol's); a
            # packed batch may only use the packing target. A caller that lowers
            # `max_body_bytes` to exercise batching still gets the real solo
            # allowance unless it says otherwise, because the two answer
            # different questions: how much to pack, versus what the server
            # will take.
            if max_body_bytes is None:
                max_body_bytes = MAX_BODY_BYTES if module is None else MAX_FRAGMENT_BODY_BYTES
                if module is not None and packing_cap is not None:
                    # A caller's byte budget (`probe import --transcripts-budget-mb`)
                    # below protocol 3's larger target would otherwise never
                    # see one batch fit. The solo ceiling is unchanged.
                    max_body_bytes = min(max_body_bytes, packing_cap)
            if solo_limit is None:
                solo_limit = max(
                    max_body_bytes,
                    MAX_SOLO_EVENT_BYTES if module is None else MAX_SOLO_FRAGMENT_BYTES,
                )
            end = state["source_byte_end"]
            lines = state["source_line_end"]
            event_no = state["event_end"]
            events: list[dict] = []
            size = 2048  # envelope + bounded source provenance
            records = 0  # source lines that retained an event
            marks: list[_Mark] = []  # after each packed line, to give lines back
            sanitize = _sanitizer(self.source)  # an unknown source refuses, never guesses
            with path.open("rb") as handle:
                handle.seek(end)
                while True:
                    raw = handle.readline(MAX_SOURCE_LINE + 1)
                    length = len(raw)
                    if length > MAX_SOURCE_LINE:
                        # Too long to read (D16): skipped to its newline and
                        # said, never held, so the capture goes on. It used to
                        # stop this session's capture for good.
                        if not raw.endswith(b"\n"):
                            length = _rest_of_line(handle, length)
                        raw = b""
                        if length is None:
                            if historic:
                                raise DeliveryPending(
                                    "historical transcript has an incomplete trailing line"
                                )
                            break  # still being written: read it whole next pass
                        retained = [oversize_line_event(length)]
                    else:
                        if not raw:
                            break
                        if not raw.endswith(b"\n"):
                            if historic:
                                raise DeliveryPending(
                                    "historical transcript has an incomplete trailing line"
                                )
                            break
                        line = None
                        try:
                            line = json.loads(raw) if raw.strip() else None
                            value = sanitize(line) if raw.strip() else None
                            if value is not None and nesting_exceeds(value):
                                value = None  # the redactor below would hit the recursion limit
                        except (ValueError, UnicodeDecodeError):
                            value = None
                        except MemoryError:
                            raise  # may pass; never drop a line for it
                        except Exception:  # noqa: BLE001
                            # A sanitizer that trips on one hostile or unforeseen
                            # line costs that line, never the session: the cursor
                            # never moves past an exception, so raising here would
                            # stop this session's capture for good.
                            value = None
                        retained = (
                            value if isinstance(value, list) else ([] if value is None else [value])
                        )
                        if extract is not None and retained:
                            # A shape the reader did not foresee costs the
                            # line's images (placeholders), never the line.
                            with suppress(ValueError, TypeError, KeyError, AttributeError, IndexError):
                                room = _attach_pasted(extract(line), retained, spool, room)
                    additions = [
                        {"line_no": event_no + i, "raw": event} for i, event in enumerate(retained)
                    ]
                    if module is None:
                        increment = len(json.dumps(additions, separators=(",", ":")).encode())
                        solo = increment
                    else:
                        fragment_bytes, event_bytes = _record_bytes(additions)
                        increment = fragment_bytes + (event_bytes if send_events else 0)
                        # Events are optional per protocol-3 batch: a record
                        # that must go alone is held to the budget by its
                        # fragments, and goes without its events if need be.
                        solo = fragment_bytes
                    if size + increment > max_body_bytes:
                        if not events:
                            # NOTHING TO PACK IT BESIDE, so the packing target
                            # does not apply -- only what the route will take.
                            # Refusing here on `max_body_bytes` rejected batches
                            # the server would have accepted, and did it
                            # permanently: one such event stranded its whole
                            # session on every retry forever.
                            if solo > solo_limit and module is not None:
                                # Rare, so exact: the estimate may be high.
                                solo = _encoded_bytes(
                                    [_fragment(module, event, version) for event in additions]
                                )
                            if solo > solo_limit:
                                raise DeliveryPending(
                                    "sanitized event exceeds the gateway batch budget; "
                                    "source retained"
                                )
                        else:
                            break
                    events.extend(additions)
                    event_no += len(additions)
                    size += increment
                    end += length
                    lines += 1
                    records += bool(additions)
                    marks.append(_Mark(len(events), end, lines, event_no, size, records))
                    # Bound reads even when the sanitizer drops everything.
                    if end - state["source_byte_end"] >= 4 * 1024 * 1024:
                        break
            at_end = end == path.stat().st_size
            final = (
                not events and end == state["source_byte_end"] and at_end and (finalize or historic)
            )
            if end == state["source_byte_end"] and not final:
                return None
            if final and state["finalized"]:
                if historic:
                    state["historical_complete"] = True
                    self._save(state)
                return None
            while True:
                body = {
                    "protocol_version": protocol,
                    "session_id": session_id,
                    "stream_id": state["stream_id"],
                    "batch_seq": state["last_seq"] + 1,
                    "source_byte_start": state["source_byte_end"],
                    "source_byte_end": end,
                    "source_line_start": state["source_line_end"],
                    "source_line_end": lines,
                    "event_start": state["event_end"],
                    "event_end": event_no,
                    "prefix_sha256": prefix_hash(path, end),
                }
                if module is not None:
                    # Every protocol-3 batch, finalize included: the gateway requires it.
                    body["fragment_version"] = version
                if final:
                    body["finalize"] = True
                else:
                    body["cwd"] = state.get("cwd") or cwd
                    if state["last_seq"] == -1 and state.get("provenance"):
                        body["provenance"] = state["provenance"]
                if historic:
                    body["snapshot_byte_end"] = state["historical_end"]
                    body["snapshot_sha256"] = state["historical_hash"]
                if module is None:
                    if not final:
                        body["events"] = events
                    encoded = canonical_payload(_scrub_content(body))
                    # One event that could not be packed is allowed the route's budget;
                    # anything packed stays inside the smaller target.
                    envelope_limit = solo_limit if len(events) <= 1 else max_body_bytes
                    break
                envelope_limit = solo_limit if records <= 1 else max_body_bytes
                if final:
                    encoded = canonical_payload(_scrub_content(body))
                    break
                # The batch's events are scrubbed TOGETHER first (adjacent
                # messages are scanned as one text, as protocol 2 does), each
                # fragment is built from its scrubbed event, and the fragments
                # are then scrubbed to their own fixed point. Scrubbing a body
                # without its events gives the same fragments: the scrubber
                # never joins text across keys.
                scrubbed = _scrub_content({"events": events})["events"]
                body["fragments"] = [_fragment(module, event, version) for event in scrubbed]
                content = _scrub_content(body)
                if send_events:
                    content["events"] = scrubbed
                encoded = canonical_payload(content)
                if len(encoded) > envelope_limit and records <= 1 and send_events:
                    # One record whose event does not fit beside its fragments:
                    # this batch goes without events (optional per batch) rather
                    # than not at all.
                    del content["events"]
                    encoded = canonical_payload(content)
                if len(encoded) <= envelope_limit or records <= 1:
                    break
                # Lines were packed by an estimate (their unscrubbed records);
                # the built batch is what counts. Give back trailing lines
                # worth the overshoot and build it again.
                over = len(encoded) - envelope_limit
                while len(marks) > 1 and over > 0:
                    over -= marks.pop().size - marks[-1].size
                count, end, lines, event_no, size, records = marks[-1]
                del events[count:]
            if len(encoded) > envelope_limit:
                raise DeliveryPending("transcript envelope exceeds the batch budget")
            used = self.conn.execute(
                "SELECT COALESCE(sum(length(body)),0) FROM pending"
            ).fetchone()[0]
            if used + len(encoded) > self.cap_bytes:
                raise DeliveryPending("transcript queue full; pending bytes preserved")
            self.conn.execute(
                "INSERT INTO pending(session_id,body,digest) VALUES(?,?,?)",
                (session_id, encoded, hashlib.sha256(encoded).hexdigest()),
            )
            if extract is not None and spool:
                # The bytes of exactly the images this batch names (lines
                # given back above name theirs again in the next batch).
                for digest in sorted(named_media(json.loads(encoded)).intersection(spool)):
                    media_type, data = spool[digest]
                    self.conn.execute(
                        "INSERT INTO media(session_id,sha256,media_type,size,bytes) VALUES(?,?,?,?,?)",
                        (session_id, digest, media_type, len(data), data),
                    )
            # The reservation is represented by this row. It is committed together
            # with state; no source cursor can pass it until its receipt arrives.
            self._save(state)
            return encoded

    def acknowledge(
        self,
        session_id: str,
        result: dict,
        *,
        sent_body: bytes | None = None,
        content_may_differ: bool = False,
    ):
        """Retire the pending batch the server acknowledged.

        `content_may_differ` accepts a receipt for the same source range whose
        body differs only in content (another scrub of the same bytes): every
        cursor and the prefix hash must still match exactly.
        """
        with self.transaction():
            pending = self.pending(session_id)
            body = sent_body if sent_body is not None else pending
            if body is None:
                return
            payload = json.loads(body)
            state = self.get(session_id)
            if state is None or payload.get("stream_id") != state["stream_id"]:
                # Another producer's reservation for this session (the pending
                # row is per session, not per stream): its receipt says nothing
                # about the stream this journal continues.
                raise ReconciliationRequired(
                    "pending batch belongs to another stream of this session; bytes retained"
                )
            receipt = result.get("receipt") or {}
            # The protocol the server holds this stream on. A body of another
            # protocol is only ever adopted through a receipt for its range.
            accepted = result.get("protocol_version")
            if accepted not in RECEIPTED_PROTOCOLS or (
                not content_may_differ
                and (
                    accepted != payload.get("protocol_version")
                    or receipt.get("body_sha256") != hashlib.sha256(body).hexdigest()
                )
            ):
                raise ReconciliationRequired(
                    "server acknowledgment does not match the immutable pending batch"
                )
            for key in (
                "batch_seq",
                "source_byte_end",
                "source_line_end",
                "event_end",
                "prefix_sha256",
            ):
                if receipt.get(key) != payload[key]:
                    raise ReconciliationRequired(
                        "server acknowledgment cursor does not match the pending batch"
                    )
            if receipt.get("finalized") != bool(payload.get("finalize")):
                raise ReconciliationRequired(
                    "server acknowledgment finalized a different stream boundary"
                )
            if state["last_seq"] >= payload["batch_seq"]:
                # Another compatible drainer committed this receipt and may
                # already have reserved its successor. Never retire that row.
                return
            if pending != body:
                raise ReconciliationRequired("pending reservation changed before acknowledgment")
            if state.get("protocol_version") not in (None, accepted):
                raise ReconciliationRequired(
                    "server stream uses another upload protocol; pending bytes retained"
                )
            # The server holds the stream now, so its protocol is settled for good.
            state["protocol_version"] = accepted
            if accepted != PROTOCOL_FRAGMENTS:
                state["send_events"] = False
            state.update(
                {
                    k: payload[k]
                    for k in ("source_byte_end", "source_line_end", "event_end", "prefix_sha256")
                }
            )
            state.update(
                {
                    "last_seq": payload["batch_seq"],
                    "finalized": bool(payload.get("finalize")),
                    "error": None,
                }
            )
            if (
                state.get("historical_end") is not None
                and state["finalized"]
                and state["source_byte_end"] >= state["historical_end"]
            ):
                state["historical_complete"] = True
            self._save(state)
            self.conn.execute("DELETE FROM pending WHERE session_id=?", (session_id,))
            self._drop_media(session_id)

    def deliver(self, session_id: str, wire: Wire, *, expected_body: bytes | None = None) -> bool:
        # A compatible drainer may already have acknowledged the staged body
        # and reserved a successor. Replay the caller's immutable bytes only;
        # acknowledge's cursor guard leaves that newer reservation untouched.
        self._refuse_deleted(session_id)
        body = expected_body if expected_body is not None else self.pending(session_id)
        if body is None:
            return False
        _require_safe_pending(body)
        self._upload_media(session_id, body, wire)
        code, result = wire.post(body)
        if deleted_response(code, result, session_id):
            raise SessionDeleted(session_id, newly=self.mark_deleted(session_id))
        if code != 202:
            message = f"http {code}: {result.get('detail', 'transcript delivery pending')}"
            if code == 409 and self._restart_refused_offer(session_id, body, result, message):
                raise ProtocolRestarted(message)
            if code == 409 and self._restart_retired_protocol2(
                session_id, body, result, message, wire
            ):
                raise ProtocolRestarted(message, PROTOCOL_FRAGMENTS)
            self.update(session_id, error=message)
            if code in (409, 422):
                raise ReconciliationRequired(
                    message, safe_message=_request_failure(wire, code), status_code=code,
                )
            raise DeliveryPending(
                message,
                retryable=_retryable_status(code) or (
                    code == 0 and getattr(wire, "last_request_retryable", False)
                ),
                safe_message=_request_failure(wire, code),
            )
        self.acknowledge(session_id, result, sent_body=body)
        return True

    def _upload_media(self, session_id: str, body: bytes, wire: Wire) -> None:
        """Upload the pasted images `body` names that still wait here, before
        the batch goes (the engine promotes an image once a batch naming it is
        accepted and its bytes are staged).

        Per image, the gateway's media door answers:
          `have`                 -> already stored: done
          `upload`               -> PUT the bytes with exactly its headers:
                                    2xx done; 408/429/5xx/no answer wait;
                                    any other 4xx final
          403/409/413/415/422/429 (any other 4xx but 401/408)
                                 -> refused, final for this image
          410 session_deleted    -> the session's deletion (SessionDeleted)
          503, 5xx, no answer    -> wait: DeliveryPending, the batch waits
        Done or final drops the image's row; a final one keeps the batch going
        (the engine shows that image as not available). Waiting is bounded
        (MEDIA_RETRY_MIN_ATTEMPTS over MEDIA_RETRY_WINDOW_SECONDS), then the
        image is given up, so capture never stalls on one image.
        """
        waiting = [
            row[0]
            for row in self.conn.execute(
                "SELECT sha256 FROM media WHERE session_id=? ORDER BY sha256", (session_id,)
            )
        ]
        if not waiting:
            return
        named = named_media(json.loads(body))
        for digest in waiting:
            if digest in named:
                self._upload_one(session_id, digest, wire)

    def _upload_one(self, session_id: str, digest: str, wire: Wire) -> None:
        row = self.conn.execute(
            "SELECT media_type, bytes FROM media WHERE session_id=? AND sha256=?",
            (session_id, digest),
        ).fetchone()
        if row is None:
            return  # another drainer finished it
        media_type, data = row[0], bytes(row[1])
        code, answer = wire.media(session_id, digest, len(data), media_type)
        if deleted_response(code, answer, session_id):
            raise SessionDeleted(session_id, newly=self.mark_deleted(session_id))
        status = answer.get("status") if code == 200 else None
        if status == "upload":
            try:
                target = _put_target(answer, data, getattr(wire, "base_url", ""))
            except Unresolved as exc:
                target, unresolved = None, exc
            else:
                unresolved = None
                if target is None:
                    return self._media_settled(session_id, digest)  # not an answer to follow
            if unresolved is not None:
                failure, retryable = f"image storage host did not resolve ({unresolved})", True
            else:
                stored = wire.put(target[0], data, target[1])
                if 200 <= stored < 300 or (400 <= stored < 500 and stored not in (408, 429)):
                    return self._media_settled(session_id, digest)
                failure, retryable = f"image upload to storage failed (http {stored})", (
                    _retryable_status(stored) or (stored == 0 and wire.last_request_retryable)
                )
        elif code == 200 or _media_refusal(code):
            # `have`, a final refusal, or a 200 that is neither answer.
            return self._media_settled(session_id, digest)
        else:
            failure, retryable = f"image upload door unavailable (http {code})", (
                _retryable_status(code) or (code == 0 and getattr(wire, "last_request_retryable", False))
            )
        with self.transaction():
            row = self.conn.execute(
                "SELECT attempts, first_failure FROM media WHERE session_id=? AND sha256=?",
                (session_id, digest),
            ).fetchone()
            if row is None:
                return
            attempts, first = row[0] + 1, row[1] if row[1] is not None else time.time()
            if attempts >= MEDIA_RETRY_MIN_ATTEMPTS and time.time() - first >= MEDIA_RETRY_WINDOW_SECONDS:
                self.conn.execute(
                    "DELETE FROM media WHERE session_id=? AND sha256=?", (session_id, digest)
                )
                return  # given up: the batch goes without these bytes
            self.conn.execute(
                "UPDATE media SET attempts=?, first_failure=? WHERE session_id=? AND sha256=?",
                (attempts, first, session_id, digest),
            )
        raise DeliveryPending(
            f"{failure}; the batch naming it waits",
            retryable=retryable,
            safe_message="a pasted image could not be uploaded yet; its batch waits",
        )

    def _media_settled(self, session_id: str, digest: str) -> None:
        self.conn.execute(
            "DELETE FROM media WHERE session_id=? AND sha256=?", (session_id, digest)
        )

    def _restart_refused_offer(
        self, session_id: str, body: bytes, result: dict, message: str
    ) -> bool:
        """Restart on protocol 2 a new stream the server will not take on 3.

        Between a receipts answer offering protocol 3 and batch 0 arriving, the
        offer can be withdrawn (a settings rollout, the kill switch), or the
        stream can already exist on protocol 2: the door then answers 409 with
        one of PROTOCOL3_REFUSALS, and would answer it forever. Only batch 0 of
        a stream this journal never had acknowledged qualifies, so no byte the
        server holds is dropped: the pending body goes and the same source range
        is staged again on protocol 2 (which a server holding batch 0 already
        settles through its receipt). Anything else stays a reconciliation.
        """
        detail = result.get("detail") if isinstance(result, dict) else None
        payload = json.loads(body)
        if (
            detail not in PROTOCOL3_REFUSALS
            or payload.get("protocol_version") != PROTOCOL_FRAGMENTS
            or payload.get("batch_seq") != 0
        ):
            return False
        with self.transaction():
            state = self.get(session_id)
            if (
                state is None
                or state.get("protocol_version") is not None
                or state["last_seq"] != -1
                or self.pending(session_id) != body
            ):
                return False
            self.conn.execute("DELETE FROM pending WHERE session_id=?", (session_id,))
            self._drop_media(session_id)
            state.update(protocol_offer=PROTOCOL_EVENTS, send_events=False, error=message)
            self._save(state)
        return True

    def _never_held(self, state: dict | None, session_id: str, body: bytes) -> bool:
        """Whether `body` is batch 0 of a stream this journal never had the
        server acknowledge or report, and still the session's pending body."""
        return (
            state is not None
            and not state.get("deleted")
            and state.get("protocol_version") is None
            and state["last_seq"] == -1
            and self.pending(session_id) == body
        )

    def _restart_retired_protocol2(
        self, session_id: str, body: bytes, result: dict, message: str, wire: Wire
    ) -> bool:
        """Restart on protocol 3 a new stream the server will not START on 2.

        A tap before 0.9.14 (or this one, before protocol 3 was offered)
        staged the session's protocol-2 batch 0, and the server retired
        protocol 2 for new streams before taking it: the door answers 409
        PROTOCOL2_RETIRED, and would forever. Restarting is safe only for batch
        0 of a stream the server does not hold, so both are proven first: the
        journal never had a batch of it acknowledged (no pinned protocol,
        `last_seq` -1, the same pending body), and a fresh receipts answer says
        the server has no stream for the session. That answer also says what a
        new stream may use (`negotiate`); only protocol 3 restarts, anything
        else leaves the batch pending for a later offer.

        Then, in one transaction: the pending protocol-2 body goes, the session
        is offered protocol 3 with that answer's `events`, and a historical
        snapshot gets its protocol-3 name (same bytes, see
        `_keep_snapshot_from_older_taps`); the cursors stay at 0, so the WHOLE
        source is staged again, under the SAME stream id, from batch 0. The
        session stays in `sessions` until the server pins it to 3, so a tap
        before 0.9.14 on this machine keeps seeing it and never starts a
        second stream for it (it may re-send the staged protocol-3 batch 0,
        which the server takes once, as a duplicate after that).
        """
        detail = result.get("detail") if isinstance(result, dict) else None
        payload = json.loads(body)
        if (
            detail != PROTOCOL2_RETIRED
            or payload.get("protocol_version") != PROTOCOL_EVENTS
            or payload.get("batch_seq") != 0
            or not self._never_held(self.get(session_id), session_id, body)
        ):
            return False
        try:
            remote = wire.receipts(session_id)
        except SessionDeleted:
            raise SessionDeleted(session_id, newly=self.mark_deleted(session_id)) from None
        self._deleted_remote(session_id, remote)
        if (
            remote.get("customer_id") != self.customer_id
            or remote.get("state") != "absent"
            or remote.get("stream")
        ):
            return False  # the server holds something for it: reconcile, never restart
        protocol, send_events = negotiate(remote)
        if protocol != PROTOCOL_FRAGMENTS:
            return False  # nothing to restart on: the batch waits for an offer
        with self.transaction():
            state = self.get(session_id)
            if not self._never_held(state, session_id, body):
                return False
            self.conn.execute("DELETE FROM pending WHERE session_id=?", (session_id,))
            self._drop_media(session_id)
            state.update(
                protocol_offer=PROTOCOL_FRAGMENTS, send_events=send_events, error=message
            )
            _record_terms(state, remote)
            self._keep_snapshot_from_older_taps(state)
            self._save(state)
        return True

    def reconcile_pending(self, session_id: str, wire: Wire) -> str | None:
        """Settle a pending batch that cannot be replayed as it is.

        Pending bytes are immutable because the server may already hold them.
        So ask it, for this one batch_seq:

          it has a receipt  -> adopt it. Same cursors and prefix hash means the
                               same source range, even if another scrub of
                               those bytes was the body it accepted.
          it has none, and the body fails today's redaction
                            -> re-redact the SAME reservation in place: same
                               batch_seq, same cursors, only content changes.
                               An older daemon may still be sending the
                               original; whichever copy the server takes, the
                               other is then adopted through its receipt.
          it has none, and the body is clean
                            -> None. Nothing to settle here, and the caller's
                               own error is the more specific one.

        Before this, a batch staged by an older scrubber was refused by every
        newer daemon, and its session stopped shipping for good.
        """
        body = self.pending(session_id)
        state = self.get(session_id)
        if body is None or state is None:
            return "nothing pending"
        payload = json.loads(body)
        sequence = payload["batch_seq"]
        try:
            remote = wire.receipts(session_id, after=sequence - 1, limit=1)
        except SessionDeleted:
            self.mark_deleted(session_id)
            raise
        self._deleted_remote(session_id, remote)
        stream = remote.get("stream") or {}
        receipt = next(
            (item for item in remote.get("receipts") or [] if item.get("batch_seq") == sequence),
            None,
        )
        if receipt is not None:
            if stream.get("stream_id") != state["stream_id"]:
                raise ReconciliationRequired("server stream changed; existing pending bytes retained")
            self.acknowledge(
                session_id,
                {"protocol_version": remote.get("protocol_version"), "receipt": receipt},
                sent_body=body,
                content_may_differ=True,
            )
            return f"adopted the server's receipt for batch {sequence}"
        scrubbed = _scrub_content(payload)
        if scrubbed == payload:
            return None
        if _scrub_content(scrubbed) != scrubbed:
            raise ReconciliationRequired(
                "redaction does not settle on the pending batch; bytes retained"
            )
        encoded = canonical_payload(scrubbed)
        budget = (
            SERVER_FRAGMENT_BATCH_LIMIT
            if payload.get("protocol_version") == PROTOCOL_FRAGMENTS
            else SERVER_BATCH_LIMIT
        )
        if len(encoded) > budget:
            raise DeliveryPending("re-redacted batch exceeds the route budget; bytes retained")
        with self.transaction():
            updated = self.conn.execute(
                "UPDATE pending SET body=?, digest=? WHERE session_id=? AND body=?",
                (encoded, hashlib.sha256(encoded).hexdigest(), session_id, body),
            )
            if updated.rowcount != 1:
                raise ReconciliationRequired("pending reservation changed before reconciliation")
        return f"re-redacted batch {sequence} in place; the server holds no copy of it"

    def update(self, session_id: str, **values):
        with self.transaction():
            state = self.get(session_id)
            if state is None:
                raise ReconciliationRequired("unknown session")
            state.update(values)
            self._save(state)

    def release_snapshot(self, session_id: str):
        """Retain source proof, not another permanent raw transcript archive."""
        with self.transaction():
            state = self.get(session_id)
            if (
                state and state.get("historical_complete") and state.get("snapshot_path")
                and not self.pending(session_id)
            ):
                Path(state["snapshot_path"]).unlink(missing_ok=True)

    def sessions(self) -> list[dict]:
        found = {
            row[0]: json.loads(row[1])
            for table in ("sessions", _FRAGMENT_SESSIONS)  # a protocol-3 row wins, as in get()
            for row in self.conn.execute(f"SELECT session_id,state FROM {table}")
        }
        return list(found.values())
