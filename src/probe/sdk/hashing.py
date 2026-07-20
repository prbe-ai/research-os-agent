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
