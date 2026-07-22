"""Durable capture primitives shared by stack connectors.

This module deliberately does not add a second server-side telemetry model.  A
``CaptureLedger`` is a small, atomically-written JSON manifest kept beside bytes
on durable storage (for example a shared PVC).  Once capture finishes, callers
can upload the ledger or fold :meth:`CaptureLedger.report` into an ordinary
Probe artifact's ``meta``.

The ledger answers two different questions:

* collection completeness -- are the expected, producer-materialized bytes on
  the configured durable path?;
* capture completeness -- have those bytes been confirmed by Probe storage?

Keeping those states separate lets a producer lifecycle hook wait only for
durable local collection, never for the network.  A post-run collector can make
the same guarantee about host outputs, but cannot make claims about state that
the producer never materialized.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

SCHEMA_VERSION = "probe.capture/v1"
_SPAN_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_DNS, "probe.capture.span")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def stable_external_key(source: str, entity: str, *parts: object) -> str:
    """Return a deterministic, delimiter-safe external key.

    ``source`` and ``entity`` form the managed vocabulary; native identifiers
    stay case-sensitive and are percent-encoded so colons, slashes, spaces, and
    Unicode cannot make two identity chains ambiguous.

    Example::

        stable_external_key("miles", "rollout", run_id, rollout_id, sample_id)
        # probe:v1:miles:rollout:job-7:600:0
    """

    source = source.strip().lower()
    entity = entity.strip().lower()
    if not source or not entity:
        raise ValueError("source and entity must be non-empty")
    if not parts:
        raise ValueError("at least one native identity part is required")

    encoded: list[str] = []
    for part in parts:
        if part is None or str(part) == "":
            raise ValueError("identity parts must be non-empty")
        encoded.append(quote(str(part), safe="-._~"))
    return ":".join(("probe", "v1", source, entity, *encoded))


def stable_span_id(run_id: str, external_key: str) -> str:
    """Return the retry-stable UUID for a span identity within one run."""

    if not str(run_id).strip() or not str(external_key).strip():
        raise ValueError("run_id and external_key must be non-empty")
    return str(uuid.uuid5(_SPAN_NAMESPACE, f"{run_id}:{external_key}"))


class CaptureState(StrEnum):
    expected = "expected"
    discovered = "discovered"
    collected = "collected"
    hashed = "hashed"
    upload_pending = "upload_pending"
    confirmed = "confirmed"
    missing = "missing"
    unsupported = "unsupported"
    collection_failed = "collection_failed"
    upload_failed = "upload_failed"
    intentionally_skipped = "intentionally_skipped"


_COLLECTED_STATES = {
    CaptureState.hashed.value,
    CaptureState.upload_pending.value,
    CaptureState.confirmed.value,
    CaptureState.upload_failed.value,
}
_FAILURE_STATES = {
    CaptureState.missing.value,
    CaptureState.unsupported.value,
    CaptureState.collection_failed.value,
    CaptureState.upload_failed.value,
}


class CaptureLedger:
    """An expected-artifact ledger persisted atomically after every mutation.

    The object is intentionally single-writer.  Atomic replacement protects
    readers and crash recovery from partial JSON; an exporter should own a
    ledger while it is updating it.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        source: str | None = None,
        run_id: str | None = None,
        external_key: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        if self.path.exists():
            self._data = self._read()
            self._assert_identity(source=source, run_id=run_id, external_key=external_key)
            return
        if not source:
            raise ValueError("source is required when creating a capture ledger")
        now = _now()
        self._data: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "source": source,
            "run_id": run_id,
            "external_key": external_key,
            "context": dict(context or {}),
            "created_at": now,
            "updated_at": now,
            "inventory_complete": False,
            "artifacts": {},
        }
        self._persist()

    @classmethod
    def open(cls, path: str | Path) -> "CaptureLedger":
        """Open an existing ledger without restating its identity."""

        return cls(path)

    def _read(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid capture ledger {self.path}: {exc}") from exc
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported capture ledger schema {data.get('schema_version')!r}"
            )
        if not isinstance(data.get("artifacts"), dict):
            raise ValueError(f"invalid capture ledger {self.path}: artifacts must be an object")
        return data

    def _assert_identity(
        self,
        *,
        source: str | None,
        run_id: str | None,
        external_key: str | None,
    ) -> None:
        for field, supplied in (
            ("source", source),
            ("run_id", run_id),
            ("external_key", external_key),
        ):
            if supplied is not None and self._data.get(field) != supplied:
                raise ValueError(
                    f"capture ledger {field} mismatch: "
                    f"{self._data.get(field)!r} != {supplied!r}"
                )

    def _persist(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data["updated_at"] = _now()
        payload = json.dumps(self._data, indent=2, sort_keys=True) + "\n"
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            # Persist the directory entry as well as the file contents.  Some PVC
            # drivers ignore this, but local/ext4 recovery benefits and it is safe.
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @property
    def source(self) -> str:
        return str(self._data["source"])

    @property
    def external_key(self) -> str | None:
        value = self._data.get("external_key")
        return str(value) if value is not None else None

    @property
    def inventory_complete(self) -> bool:
        return bool(self._data.get("inventory_complete"))

    @property
    def context(self) -> dict[str, Any]:
        return dict(self._data.get("context") or {})

    def expect(
        self,
        key: str,
        *,
        role: str,
        relative_path: str | None = None,
        required: bool = True,
        meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Declare an artifact without resetting progress from an earlier attempt."""

        if not key:
            raise ValueError("artifact key must be non-empty")
        artifacts = self._data["artifacts"]
        entry = artifacts.get(key)
        if entry is None:
            entry = {
                "key": key,
                "role": role,
                "relative_path": relative_path,
                "required": required,
                "state": CaptureState.expected.value,
                "created_at": _now(),
                "updated_at": _now(),
                "meta": dict(meta or {}),
            }
            artifacts[key] = entry
        else:
            entry.update({"role": role, "required": required})
            if relative_path is not None:
                entry["relative_path"] = relative_path
            if meta:
                entry["meta"] = {**(entry.get("meta") or {}), **meta}
            entry["updated_at"] = _now()
        self._persist()
        return dict(entry)

    def mark(self, key: str, state: CaptureState | str, **fields: Any) -> dict[str, Any]:
        """Record progress or a terminal reason for one declared artifact."""

        try:
            normalized = CaptureState(state).value
        except ValueError as exc:
            raise ValueError(f"unknown capture state {state!r}") from exc
        entry = self._data["artifacts"].get(key)
        if entry is None:
            raise KeyError(f"artifact {key!r} was not declared")
        entry["state"] = normalized
        for field, value in fields.items():
            if value is not None:
                entry[field] = value
            elif field in entry:
                entry.pop(field)
        entry["updated_at"] = _now()
        self._persist()
        return dict(entry)

    def finish_inventory(self) -> None:
        self._data["inventory_complete"] = True
        self._persist()

    def begin_inventory(self) -> None:
        """Invalidate an older inventory before rescanning a durable directory."""

        self._data["inventory_complete"] = False
        self._persist()

    def entries(self) -> list[dict[str, Any]]:
        return [dict(self._data["artifacts"][key]) for key in sorted(self._data["artifacts"])]

    def get(self, key: str) -> dict[str, Any] | None:
        entry = self._data["artifacts"].get(key)
        return dict(entry) if entry is not None else None

    def update_context(self, **values: Any) -> None:
        """Merge connector bookkeeping that is not itself expected evidence."""

        self._data["context"] = {**(self._data.get("context") or {}), **values}
        self._persist()

    def pending_artifacts(self) -> list[dict[str, Any]]:
        """Return entries whose bytes still need collection or confirmation."""

        return [entry for entry in self.entries() if entry["state"] != CaptureState.confirmed]

    def report(self) -> dict[str, Any]:
        """Return a JSON-ready completeness report derived from the ledger."""

        entries = self.entries()
        required = [entry for entry in entries if entry.get("required", True)]
        collection_missing = [
            self._missing_summary(entry)
            for entry in required
            if entry.get("state") not in _COLLECTED_STATES
        ]
        capture_missing = [
            self._missing_summary(entry)
            for entry in required
            if entry.get("state") != CaptureState.confirmed.value
        ]
        required_failures = [
            entry for entry in required if entry.get("state") in _FAILURE_STATES
        ]

        if not self.inventory_complete:
            collection_state = "pending"
        elif collection_missing:
            collection_state = (
                "partial"
                if any(entry.get("state") in _FAILURE_STATES for entry in required)
                else "pending"
            )
        else:
            collection_state = "complete"

        if not self.inventory_complete:
            capture_state = "pending"
        elif not capture_missing:
            capture_state = "complete"
        elif required_failures:
            capture_state = "partial"
        else:
            capture_state = "pending"

        counts: dict[str, int] = {}
        for entry in entries:
            state = str(entry.get("state"))
            counts[state] = counts.get(state, 0) + 1
        return {
            "schema_version": SCHEMA_VERSION,
            "source": self.source,
            "external_key": self.external_key,
            "scope": self.context.get("scope"),
            "capture_scope": "declared_file_bytes",
            "unknown": list(self.context.get("unknown") or []),
            "manifest_publication": dict(
                self.context.get("manifest_publication") or {"state": "not_started"}
            ),
            "inventory_complete": self.inventory_complete,
            "collection": {"state": collection_state, "missing": collection_missing},
            "capture": {"state": capture_state, "missing": capture_missing},
            "counts": counts,
            "skipped": [
                self._missing_summary(entry)
                for entry in entries
                if entry.get("state") == CaptureState.intentionally_skipped.value
            ],
        }

    @staticmethod
    def _missing_summary(entry: dict[str, Any]) -> dict[str, Any]:
        return {
            key: entry.get(key)
            for key in ("key", "role", "relative_path", "state", "error")
            if entry.get(key) is not None
        }

    def as_dict(self) -> dict[str, Any]:
        """Return an isolated JSON-compatible copy for diagnostics/tests."""

        return json.loads(json.dumps(self._data))


__all__ = [
    "CaptureLedger",
    "CaptureState",
    "SCHEMA_VERSION",
    "stable_external_key",
    "stable_span_id",
]
