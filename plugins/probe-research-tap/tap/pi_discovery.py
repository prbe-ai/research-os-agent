"""Find pi session files by SHAPE, across configurable roots.

Claude Code and Codex each write sessions to exactly one directory we can
hardcode. pi does not: `SessionManager` is a published library, so whoever
embeds pi chooses where sessions land —

  SessionManager.create(cwd)  the default `~/.pi/agent/sessions/`
  SessionManager.open(path)   anywhere
  a project-local directory   e.g. `.sessions/`
  SessionManager.inMemory()   nowhere at all

A rebranded fork moves the whole tree. This is the fork-compatibility half of
pi capture, and the reason it exists at all: Claude Code and Codex have no
equivalent, because their session locations are not something an embedder
chooses.

So discovery tests the FILE, not the path: a pi session's first line is a
`{"type": "session", ...}` header, and nothing else this plugin ingests looks
like that. Roots come from `PROBE_PI_SESSION_ROOTS` (os.pathsep-separated,
same convention as PATH) and default to the upstream location.

`SessionManager.inMemory()` sessions write nothing to disk and are therefore
uncapturable by ANY file watcher, this one included. That is a documented
limitation of watching files, not a bug this module can work around: there is
no file to discover.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

#: os.pathsep-separated list of directories to scan for pi session files.
#: Unset -> just the upstream default (DEFAULT_ROOT).
SESSION_ROOTS_ENV = "PROBE_PI_SESSION_ROOTS"

#: Where SessionManager.create(cwd) writes when the embedder does not
#: override it. Not the only place a session can live — see the module
#: docstring — just the one worth defaulting to.
DEFAULT_ROOT = Path.home() / ".pi" / "agent" / "sessions"

#: Enough to hold the header line of any real pi session. Reading the whole
#: file to classify it would mean reading every session on every scan; a pi
#: header is a small fixed-shape object well under this.
_HEADER_PROBE_BYTES = 8192


def session_roots() -> list[Path]:
    """Configured pi session roots, or just the upstream default."""
    raw = os.environ.get(SESSION_ROOTS_ENV, "").strip()
    if not raw:
        return [DEFAULT_ROOT]
    return [Path(p).expanduser() for p in raw.split(os.pathsep) if p.strip()]


def is_pi_session_file(path: Path) -> bool:
    """True when `path`'s first line is a pi session header.

    Reads only the probe window, not the whole file: a session file can be
    many MB, and every candidate `.jsonl` under every root gets this check on
    every scan.
    """
    try:
        with path.open("rb") as fh:
            head = fh.read(_HEADER_PROBE_BYTES)
    except OSError:
        return False
    if not head:
        return False
    newline = head.find(b"\n")
    if newline == -1:
        # No terminator inside the probe window: either the first line is
        # still being written (a session file is created before its header
        # line is flushed) or this file has no line-oriented shape at all.
        # Either way, not (yet) a recognizable pi session.
        return False
    try:
        first = json.loads(head[:newline])
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(first, dict) and first.get("type") == "session"


def discover_session_files() -> list[Path]:
    """Every pi session file under every configured root, deduplicated.

    Deduplication is by RESOLVED path — the key a symlink cannot dodge — but
    the returned paths are the as-discovered (unresolved) ones, so a caller
    still sees the path it configured. Two roots that overlap (a root and a
    subdirectory of it, or a symlink into another root) must not yield the
    same file twice: keying the `seen` dict by the unresolved candidate
    instead would miss exactly that case, since a symlinked alias reaches
    the same file through a literally different path.
    """
    seen: dict[Path, Path] = {}
    for root in session_roots():
        if not root.is_dir():
            continue
        for candidate in sorted(root.rglob("*.jsonl")):
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            if is_pi_session_file(candidate):
                seen[resolved] = candidate
    return list(seen.values())
