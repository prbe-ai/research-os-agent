"""Hook-facing coding-session capture primitives.

These calls are intentionally separate from run telemetry. Future Claude Code
hooks call attach/checkpoint/detach deterministically; researchers should use the
normal run/event/artifact APIs for experimentation data.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import Client

_SECRET_PATTERNS = [
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s\"']+"),
    re.compile(r"(?i)\b(sk-[a-z0-9_-]{12,}|ros_(?:pat|ing)_[a-z0-9]+)\b"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact_transcript(text: str) -> str:
    """Best-effort local redaction before a transcript segment leaves the host."""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(r"\1[REDACTED]", text)
        else:
            text = pattern.sub("[REDACTED_PRIVATE_KEY]", text)
    return text


class SessionCaptureClient:
    """Session correlation API reserved for hook/broker adapters."""

    def __init__(self, client: "Client"):
        self.client = client

    def attach(
        self,
        run_id: str,
        session_id: str,
        *,
        agent: str = "claude-code",
        transcript_path: str | None = None,
        cwd: str | None = None,
        strict: bool | None = None,
    ) -> dict | None:
        return self._merge_session(
            run_id,
            session_id,
            {
                "agent": agent,
                "transcript_available": bool(transcript_path),
                "cwd": cwd,
                "state": "attached",
                "attached_at": _now(),
            },
            strict=strict,
        )

    def checkpoint(
        self,
        run_id: str,
        session_id: str,
        *,
        transcript_path: str | None = None,
        reason: str = "checkpoint",
        strict: bool | None = None,
    ) -> dict[str, Any]:
        artifact = None
        if transcript_path:
            path = Path(transcript_path).expanduser()
            raw = path.read_text(errors="replace")
            redacted = redact_transcript(raw)
            digest = hashlib.sha256(redacted.encode()).hexdigest()
            # API v3 cannot upload bytes yet. Record the redacted segment as a
            # local reference and report that portability remains incomplete.
            artifact = self.client.write(
                "POST",
                f"/v1/runs/{run_id}/artifacts",
                {
                    "kind": "transcript_segment",
                    "name": f"session-{session_id}-{digest[:12]}",
                    "content_hash": digest,
                    "size_bytes": len(redacted.encode()),
                    "is_reference": True,
                    "meta": {
                        "schema_version": "1.0",
                        "session_id": session_id,
                        "source_name": path.name,
                        "redacted": True,
                        "portable": False,
                        "reason": reason,
                    },
                },
                strict=strict,
            )
        self._merge_session(
            run_id,
            session_id,
            {"state": "attached", "last_checkpoint_at": _now(), "reason": reason},
            strict=strict,
        )
        return {
            "run_id": run_id,
            "session_id": session_id,
            "artifact": artifact,
            "portable": False if transcript_path else None,
        }

    def detach(
        self,
        run_id: str,
        session_id: str,
        *,
        reason: str = "session_end",
        strict: bool | None = None,
    ) -> dict | None:
        return self._merge_session(
            run_id,
            session_id,
            {"state": "detached", "detached_at": _now(), "reason": reason},
            strict=strict,
        )

    def _merge_session(
        self,
        run_id: str,
        session_id: str,
        fields: dict[str, Any],
        *,
        strict: bool | None,
    ) -> dict | None:
        run = self.client.get_run(run_id)
        metadata = dict(run.get("metadata") or {})
        agent_meta = dict(metadata.get("agent") or {})
        sessions = [dict(item) for item in agent_meta.get("sessions", [])]
        for item in sessions:
            if item.get("session_id") == session_id:
                item.update(fields)
                break
        else:
            sessions.append({"session_id": session_id, **fields})
        agent_meta.update({"schema_version": "1.0", "sessions": sessions})
        metadata["agent"] = agent_meta
        return self.client.write(
            "PATCH", f"/v1/runs/{run_id}", {"metadata": metadata}, strict=strict
        )
