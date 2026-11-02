"""MessageLedger: which server notices this computer's person and each agent session were shown (R19, R26),
in files every client shares. STDLIB ONLY, Python 3.9: the hooks copy it; pi's twin is `serverLedger.ts`."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import time
from enum import Enum
from pathlib import Path
from typing import Optional

#: The ledgers' folder under the Probe state folder (`$XDG_STATE_HOME/probe`, else `~/.local/state/probe`).
LEDGER_DIRNAME = "server-messages"
#: A claim not committed within this long was left by a process that died mid-show: another takes it.
CLAIM_TTL_S = 60
CLAIM_SUFFIX = ".claim"
SHOWN_SUFFIX = ".shown"
#: The account's last `GET /v1/client/messages` answer per client kind (the server matches notices by
#: kind), shared by every client on the computer: `messages-<kind>.json`.
LIST_FILENAME = "messages-{kind}.json"
#: A session's folder unused this long is removed when a list is saved (sessions end; files pile up).
SESSION_KEEP_S = 30 * 86400
#: A message id kept as it is in a key only when this plain; else a digest stands in
#: (`probe.shared.mailbox.make_server_message_id`'s rule, so a mailbox id minus its `n` is a key).
_PLAIN_ID = re.compile(r"[A-Za-z0-9_]{1,48}")
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")
_MAX_REVISION = 10**9


class Destination(str, Enum):
    """Who is shown a notice: the person once per computer, an agent once per session."""

    PERSON = "person"
    AGENT = "agent"

    def __str__(self) -> str:
        return str(self.value)


def make_account_key(base_url: str, token: str) -> str:
    """The ledger key of a server + credential (a token is one person on one team). Never stores the token."""
    return hashlib.sha256(f"{base_url.rstrip('/')}|{token}".encode()).hexdigest()[:16]


def make_message_key(message_id: str, revision: int = 0) -> str:
    """One message at one revision: an edited notice is a new key, so it shows once more (R26)."""
    key = message_id if _PLAIN_ID.fullmatch(message_id) else hashlib.sha256(message_id.encode()).hexdigest()[:16]
    return f"{key}_{min(max(0, int(revision)), _MAX_REVISION)}"


def get_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME", "").strip()
    return (Path(xdg) if xdg else Path.home() / ".local" / "state") / "probe"


class Claim:
    """This process's right to show one message to one destination. `commit` after the output went out,
    `release` when it did not (the next client shows it)."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @property
    def shown_path(self) -> Path:
        return self.path.with_name(self.path.name[: -len(CLAIM_SUFFIX)] + SHOWN_SUFFIX)

    def commit(self) -> None:
        try:
            os.replace(self.path, self.shown_path)
        except OSError:
            _touch(self.shown_path)  # taken over as stale meanwhile: it was shown all the same

    def release(self) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


class SavedList:
    """The account's last fetched message list: its set revision, when, the wire items as sent, and the
    client version that asked (the server matches notices by version: another version's list is not ours)."""

    def __init__(self, set_revision: Optional[str], fetched_at: float, items: list,
                 client_version: Optional[str] = None) -> None:
        self.set_revision = set_revision
        self.fetched_at = fetched_at
        self.items = items
        self.client_version = client_version


class MessageLedger:
    """One account's ledger folder. Every method is best effort and never raises: a ledger that cannot
    be written shows nothing (a claim it cannot make is a message not shown, never one shown twice)."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    @classmethod
    def from_account(cls, base_url: str, token: str, root: Optional[Path] = None) -> "MessageLedger":
        base = root if root is not None else get_state_dir() / LEDGER_DIRNAME
        return cls(base / make_account_key(base_url, token))

    def claim(self, message_key: str, destination: Destination, session_id: str = "") -> Optional[Claim]:
        """The claim to show `message_key` to `destination` (an agent: of `session_id`), or None when it
        was shown, another process is showing it, or the folder cannot be written."""
        folder = self._get_scope_folder(destination, session_id)
        if folder is None:
            return None
        path = folder / (message_key + CLAIM_SUFFIX)
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None
        claim = Claim(path)
        for _attempt in range(2):
            if claim.shown_path.exists():
                return None
            try:
                fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                if not _take_stale(path):
                    return None
                continue
            except OSError:
                return None
            with os.fdopen(fd, "w") as handle:
                handle.write(json.dumps({"pid": os.getpid(), "at": time.time()}))
            if claim.shown_path.exists():  # committed by a stale claim's owner after all
                claim.release()
                return None
            return claim
        return None

    def has_shown(self, message_key: str, destination: Destination, session_id: str = "") -> bool:
        folder = self._get_scope_folder(destination, session_id)
        return folder is not None and (folder / (message_key + SHOWN_SUFFIX)).exists()

    def load_list(self, client_kind: str) -> Optional[SavedList]:
        try:
            data = json.loads(self._list_path(client_kind).read_text(encoding="utf-8"))
            items = data["messages"]
            revision, version = data.get("set_revision"), data.get("client_version")
            if not isinstance(items, list) or not (revision is None or isinstance(revision, str)):
                return None
            return SavedList(revision, float(data.get("fetched_at") or 0.0), items,
                             version if isinstance(version, str) else None)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return None

    def save_list(self, set_revision: Optional[str], items: list, client_kind: str, now: Optional[float] = None,
                  client_version: Optional[str] = None) -> None:
        """Keep a fetched list for every client here (temp file + rename), and drop long-unused sessions."""
        at = time.time() if now is None else now
        payload = json.dumps({"set_revision": set_revision, "fetched_at": at, "messages": items,
                              "client_version": client_version})
        target = self._list_path(client_kind)
        temporary = target.with_name(f"{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            temporary.write_text(payload, encoding="utf-8")
            os.replace(temporary, target)
        except OSError:
            try:
                temporary.unlink()
            except OSError:
                pass
            return
        self._sweep_sessions(at)

    def _list_path(self, client_kind: str) -> Path:
        return self.folder / LIST_FILENAME.format(kind=_UNSAFE.sub("", client_kind) or "client")

    def _get_scope_folder(self, destination: Destination, session_id: str) -> Optional[Path]:
        if destination == Destination.PERSON:
            return self.folder / Destination.PERSON.value
        safe = _UNSAFE.sub("", session_id or "")
        return self.folder / Destination.AGENT.value / safe if safe else None

    def _sweep_sessions(self, now: float) -> None:
        sessions = self.folder / Destination.AGENT.value
        try:
            folders = [folder for folder in sessions.iterdir() if folder.is_dir()]
        except OSError:
            return
        for folder in folders:
            try:
                if now - folder.stat().st_mtime > SESSION_KEEP_S:
                    shutil.rmtree(folder, ignore_errors=True)
            except OSError:
                pass


def _take_stale(path: Path) -> bool:
    """Whether a claim in the way is gone or stale (then this process may try again): a stale one is
    renamed aside first, so of two takers exactly one moves it."""
    try:
        age = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if age < CLAIM_TTL_S:
        return False
    aside = path.with_name(f"{path.name}.{secrets.token_hex(4)}.stale")
    try:
        os.rename(str(path), str(aside))
    except FileNotFoundError:
        return True
    except OSError:
        return False
    try:
        aside.unlink()
    except OSError:
        pass
    return True


def _touch(path: Path) -> None:
    try:
        os.close(os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o600))
    except OSError:
        pass
