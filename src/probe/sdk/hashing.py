"""Content fingerprinting for the upload flow.

Its own module because both ``sdk/client.py`` (anchored uploads) and ``sdk/run.py``
(run-anchored uploads) need it, and they cannot import each other: ``client`` imports
``Run`` at the bottom of the file, so a module-level import back the other way
deadlocks depending on which module the caller reaches first. A dependency-free
module sidesteps that and, more usefully, keeps ONE definition of the hash the wire
contract is written against.
"""

from __future__ import annotations

import hashlib
import os
import socket
from pathlib import Path

#: Read size for streaming a file. Bounded so a multi-GB artifact never has to fit
#: in memory just to be hashed.
_CHUNK_BYTES = 1024 * 1024


def fingerprint(path: str) -> tuple[str, int]:
    """``(sha256 hex, size_bytes)`` for a local file.

    The digest is lowercase hex: the server validates the format and 422s on
    anything else, so this is contract, not preference.
    """
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_BYTES), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def local_file_uri(abspath: str) -> str:
    """RFC 8089 ``file://`` URI for an absolute local path.

    ``pathlib`` percent-encodes spaces, ``#``, ``%`` and non-ASCII, so the stored
    pointer round-trips whatever characters the path contains and the client and
    server agree on one encoding. The RAW path is kept separately in
    ``meta.local_path`` -- that, not this URI, is what an agent resolves.
    """
    return Path(abspath).as_uri()


def reference_fields(
    path: str, *, hash_content: bool = False, allow_missing: bool = False
) -> dict:
    """Artifact fields for a local-PATH reference (bytes NOT uploaded).

    Returns ``uri`` (a ``file://`` pointer), ``meta`` (the raw ``local_path`` plus the
    ``host`` that recorded it), and ``size_bytes``/``content_hash`` when known. Only
    ``os.stat`` runs by default -- recording a 16 GB checkpoint reference must not read
    16 GB. ``hash_content`` opts into a full fingerprint (enables content dedup and a
    later reference->managed adoption). Raises ``FileNotFoundError`` when the path is
    missing unless ``allow_missing`` (it may live on a mount/host this machine cannot see).
    """
    local = os.path.abspath(path)
    exists = os.path.exists(local)
    if not exists and not allow_missing:
        raise FileNotFoundError(
            f"cannot reference '{local}': no such file. Pass allow_missing to record it "
            "anyway (e.g. it lives on a mount or host this machine does not see)."
        )
    fields: dict = {
        "uri": local_file_uri(local),
        "meta": {"local_path": local, "host": socket.gethostname()},
    }
    if exists:
        if hash_content:
            digest, size = fingerprint(path)
            fields["content_hash"] = digest
            fields["size_bytes"] = size
        else:
            fields["size_bytes"] = os.path.getsize(local)
    return fields
