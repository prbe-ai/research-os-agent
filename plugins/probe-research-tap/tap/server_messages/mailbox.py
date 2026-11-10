"""Tell a session's coding agent what the Probe server said when it refused the session's upload: a
post to the session's Probe mailbox, once per message (`probe.shared.mailbox.post_server_message`'s
twin; `agent/tests/test_tap_server_messages.py` pins the two)."""

from __future__ import annotations

import json
import os
import re
import secrets
import time
from pathlib import Path

from .server_messages import ServerMessage, parse_server_message

#: `probe.shared.mailbox` (the parity test pins them).
KIND_SERVER = "server"
SERVER_ID_PREFIX = "n"
SERVER_FILE_HEAD = "0" * 20
SERVER_TTL_S = 12 * 3600
MESSAGE_MAX_CHARS = 1200
POSTED_SUFFIX = ".posted"
_SERVER_KEY = re.compile(r"[A-Za-z0-9_]{1,48}")
#: `mailbox.post` must be done by this long after it is sent, and may answer this much later;
#: past both the file is written here (the service never writes past the deadline it was given).
POST_WAIT_S = 1.0
POST_GRACE_S = 0.5


def get_reads_root() -> Path:
    """`probe.shared.mailbox.layout.root`: `<state>/probe/reads`."""
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "probe" / "reads"


def _to_safe_name(session_id: str) -> str:
    return "".join(c for c in session_id if c.isalnum() or c in "-_") or "session"


def make_server_message_id(message_id: str, revision: int = 0) -> str:
    """`mailbox.make_server_message_id`: the same message and revision is always the same id."""
    import hashlib

    key = message_id if _SERVER_KEY.fullmatch(message_id) else hashlib.sha256(message_id.encode()).hexdigest()[:16]
    return f"{SERVER_ID_PREFIX}{key}_{min(max(0, int(revision)), 10**9)}"


def build_message(session_id: str, message: ServerMessage, now: float | None = None) -> dict:
    """`mailbox.build_server_message`, as the file's JSON."""
    made = time.time() if now is None else now
    return {
        "id": make_server_message_id(str(message.message_id), message.revision), "session": session_id,
        "kind": KIND_SERVER, "text": message.text[:MESSAGE_MAX_CHARS], "ask": None, "question": None,
        "reason": None, "made_at": made, "expires_at": made + SERVER_TTL_S, "origin_turn": None,
        "keep": False, "alerts": None, "origin": None, "daemon_instance": None,
        "severity": message.severity.value, "audience": message.audience.value, "partial": False,
    }


def post_server_message(session_id: str, message: ServerMessage) -> bool:
    """Post a registered server message to the session once (an O_EXCL marker beside its claims
    decides which producer posts it): through the Probe service when one answers, else here.
    False when it is not registered, was posted already, or could not be written."""
    if not message.is_registered:
        return False
    data = build_message(session_id, message)
    name = f"{SERVER_FILE_HEAD}-{data['id']}.json"
    root = get_reads_root()
    claimed = root / "claimed" / _to_safe_name(session_id)
    marker = claimed / f"{name[: -len('.json')]}.{int(data['made_at'] // SERVER_TTL_S)}{POSTED_SUFFIX}"
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except OSError:
        return False  # posted already in this window (FileExistsError), or nowhere to write
    unread = root / "messages" / _to_safe_name(session_id) / name
    _drop_expired(unread, data["made_at"])
    if _post_via_service(session_id, data, root) or _write_here(unread, claimed, data):
        return True
    marker.unlink(missing_ok=True)  # not posted: the next refusal tries again
    return False


def forward_refusal(session_id: str | None, status: int, body: object) -> bool:
    """`body` of a refused request for `session_id`, told to its agent when it is a message from
    the server's registry. Never raises: delivery goes on whatever happens here."""
    if not session_id or status < 400:
        return False
    try:
        message = parse_server_message(status, body)
        return message.is_registered and post_server_message(session_id, message)
    except Exception:  # noqa: BLE001 -- telling the agent is best effort; capture must go on
        return False


def _post_via_service(session_id: str, data: dict, root: Path) -> bool:
    """`mailbox.post` within POST_WAIT_S. True once the service wrote the file; False when it did
    not, and then only after its deadline passed, so the file is written here after it, never twice."""
    try:
        from tap import __version__
        from tap.service import client
        from tap.service.protocol import Client
    except Exception:  # noqa: BLE001 -- no client beside this file: written here
        return False
    conn = client.connect(0.3)
    if conn is None:
        return False
    sent = None
    try:
        conn.hello(Client.TAP, __version__)
        conn.sock.settimeout(POST_WAIT_S + POST_GRACE_S)
        sent = time.monotonic()
        answer = conn.call("mailbox.post", {"sid": session_id, "state_dir": str(root.parent.absolute()),
                                            "message": data, "deadline": time.time() + POST_WAIT_S,
                                            "budget": POST_WAIT_S})
    except client.ServiceError:
        return False  # an answer: it wrote nothing (refused, an older service)
    except Exception:  # noqa: BLE001 -- no answer: it may still be at work until its deadline
        if sent is not None:
            time.sleep(max(0.0, sent + POST_WAIT_S + POST_GRACE_S - time.monotonic()))
        return False
    finally:
        conn.close()
    return bool(isinstance(answer, dict) and answer.get("posted"))


def _drop_expired(path: Path, now: float) -> None:
    """An expired, unread copy of the message: this window's post replaces it."""
    try:
        earlier = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(earlier, dict) and 0 < float(earlier.get("expires_at") or 0) < now:
            path.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        pass


def _write_here(path: Path, claimed: Path, data: dict) -> bool:
    """The file, whole (a temp file, then a link that fails if the name is taken); never when a
    deliverer already holds it (`claimed/`: the service's copy landed and was shown)."""
    stem = path.name[: -len(".json")]
    if (claimed / path.name).exists() or any(claimed.glob(f"{stem}.*.json")):
        return True
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
        os.link(tmp, path)
    except FileExistsError:
        return True  # the service's copy landed after all
    except OSError:
        return False
    finally:
        tmp.unlink(missing_ok=True)
    return True
