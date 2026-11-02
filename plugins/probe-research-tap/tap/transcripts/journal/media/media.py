"""lines too long to send, and pasted images: decoded, hashed onto their placeholders, uploaded before their batch."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from ...sanitizers.claude_code import PastedImage
MAX_SOURCE_LINE = 64 * 1024 * 1024
#: The system event that stands in for a line over MAX_SOURCE_LINE (D16).
OVERSIZE_LINE_SUBTYPE = "oversize_line"
#: Pasted images: the types the engine stores (its IMAGE_MEDIA_TYPES), the
#: largest one the media door takes, and how many bytes of images waiting to
#: upload this journal holds at most (over it, an image keeps its placeholder).
IMAGE_MEDIA_TYPES = ("image/gif", "image/jpeg", "image/png", "image/webp")
MEDIA_MAX_BYTES = 5 * 1024**3
#: An image whose upload keeps failing for reasons that may pass (no network,
#: the door's 503, storage's 5xx) holds its batch back until it has failed
#: this many times over at least this long; then it is given up (the engine
#: shows it as not available) and the batch goes. Capture never stalls on one.
MEDIA_RETRY_MIN_ATTEMPTS = 3
MEDIA_RETRY_WINDOW_SECONDS = 3600
_SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


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


class MediaUploadMixin:
    """Part of Journal: its methods run with `self` the Journal; compose() folds them into Journal."""

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
from ..errors import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    DeliveryPending,
    SessionDeleted,
    _retryable_status,
    deleted_response,
)
from .target import Unresolved, _media_refusal, _put_target  # noqa: E402 -- pieces need each other; the front door loads this one first
from ..wire import Wire  # noqa: E402 -- pieces need each other; the front door loads this one first
