"""settle a session whose own transcript file is gone: stop retrying it and say one line, once."""

from __future__ import annotations
import json
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

#: When the row's file was found gone. A marked row is skipped while its path is still missing.
SOURCE_GONE_AT = "source_gone_at"
#: What the one line said (`COMPLETE`, `NOT_COMPARED: ...`, `DIFFERS`, `incomplete: ...`,
#: `NOT_CHECKED`): a restarted tap finds it and neither logs nor asks the server again.
SOURCE_GONE_NOTE = "source_gone_note"
COMPLETE = "complete"
NOT_COMPARED = "not compared"
DIFFERS = "differs"
NOT_CHECKED = "not checked"
#: Notes said at info level; every other one is a warning.
_SETTLED_NOTES = (COMPLETE, NOT_COMPARED)


class MissingSourceMixin:
    """Part of Journal: its methods run with `self` the Journal; compose() folds them into Journal."""

    def is_source_gone(self, state: dict) -> bool:
        """Skip this row? Marked gone and its path still missing (one stat). A file back at the
        same path drains as before; any other error reading it is retried as before."""
        return bool(state.get(SOURCE_GONE_AT)) and _is_missing(state.get("path"))

    def settle_missing_source(
        self, state: dict, wire, is_folder_disabled: Callable[[Path], bool]
    ) -> None:
        """Raise `SourceMissing` when the row's own file is gone, after marking the row.

        The first time only, asks the server (`wire.receipts`) to word one line, and only for a
        session whose recorded folder may be captured (`is_folder_disabled` false); otherwise,
        or on a network error, the note is "not checked". A 410 or `state: deleted` raises
        `SessionDeleted` (the existing deleted path). A file back at its path clears the marks.
        """
        path = state.get("path")
        session_id = state["session_id"]
        if not _is_missing(path):
            if state.get(SOURCE_GONE_AT) or state.get(SOURCE_GONE_NOTE):
                self._clear_source_gone(session_id)
            return
        current = self.get(session_id) or state
        note = current.get(SOURCE_GONE_NOTE)
        if note is None:
            folder = self._load_recorded_folder(current)
            note = (
                NOT_CHECKED if folder is None or is_folder_disabled(Path(folder))
                else self._check_server_copy(current, wire)
            )
            _log_source_gone(session_id, path, note)
        self.update(session_id, **{SOURCE_GONE_AT: time.time(), SOURCE_GONE_NOTE: note}, error=None)
        raise SourceMissing(f"session {session_id}: transcript {path} is gone; not retried")

    def _clear_source_gone(self, session_id: str) -> None:
        with self.transaction():
            current = self.get(session_id)
            if current is not None:
                current.pop(SOURCE_GONE_AT, None)
                current.pop(SOURCE_GONE_NOTE, None)
                self._save(current)

    def _load_recorded_folder(self, state: dict) -> str | None:
        """The session's folder on record (its state, else its pending batch); never the file."""
        folder = state.get("cwd")
        if not folder:
            pending = self.pending(state["session_id"])
            folder = json.loads(pending).get("cwd") if pending else None
        return folder if isinstance(folder, str) and os.path.isabs(folder) else None

    def _check_server_copy(self, state: dict, wire) -> str:
        """Compare the server's stream with every end this row knows (acknowledged, pending,
        pinned history). `COMPLETE` needs the same prefix hash at the same end; byte counts
        alone are no proof. Local and server stream ids and cursors need not agree: the
        importer's coverage path never updates this journal."""
        session_id = state["session_id"]
        try:
            remote = wire.receipts(session_id)
        except SessionDeleted:
            raise
        except (DeliveryPending, ReconciliationRequired):
            return NOT_CHECKED
        self._deleted_remote(session_id, remote)
        pending = self.pending(session_id)
        body = json.loads(pending) if pending else {}
        known = {
            end: digest
            for end, digest in (
                (state.get("source_byte_end"), state.get("prefix_sha256")),
                (body.get("source_byte_end"), body.get("prefix_sha256")),
                (state.get("historical_end"), state.get("historical_hash")),
            )
            if type(end) is int
        }
        needed = max(known, default=0)
        stream = remote.get("stream") or {}
        held = stream.get("source_byte_end")
        held = held if type(held) is int else 0
        if held < needed:
            return f"incomplete: missing bytes {held}-{needed}"
        if stream.get("finalized") is not True:
            return "incomplete: not finalized"
        if not known.get(held):
            return f"{NOT_COMPARED}: Probe holds a finalized copy ({held} bytes)"
        return COMPLETE if stream.get("prefix_sha256") == known[held] else DIFFERS


def _is_missing(path: str | None) -> bool:
    """No such file, or a parent turned into a file: gone. A row with no path, or any other
    error (permissions, a name too long), is not missing and keeps today's retry."""
    if not path:
        return False
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError:
        return False
    return False


def _log_source_gone(session_id: str, path: str, note: str) -> None:
    said = {
        COMPLETE: "it is already complete in Probe, so nothing more is sent",
        DIFFERS: "Probe's copy differs from what this machine sent",
        NOT_CHECKED: "it was not checked against Probe",
    }.get(note)
    if note.startswith(NOT_COMPARED):
        said = note.split(": ", 1)[1] + "; not compared with this file, which is gone"
    if said is None:
        said = f"Probe's copy is {note}; nothing more can be sent"
    if note.startswith(_SETTLED_NOTES):
        log.info("session %s: its transcript %s is gone; %s", session_id, path, said)
    else:
        log.warning(
            "session %s: its transcript %s is gone; %s; capture of this session stopped",
            session_id, path, said,
        )


from .errors import DeliveryPending, ReconciliationRequired, SessionDeleted, SourceMissing  # noqa: E402 -- pieces need each other; the front door loads this one first
