"""protocol 3: batches of fragments built by the vendored builder, packed line by line under the fragment limits."""

from __future__ import annotations
import json
from typing import NamedTuple
from .events import ENVELOPE_HEADROOM, MAX_BODY_BYTES
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
#: Protocol 3's packing target. A fragment carries its event's text about
#: twice (its Line and its message or parts), and the events ride along while
#: the server asks for them, so a protocol-3 batch of the same events is about
#: twice the bytes: twice the target keeps the events per batch near protocol
#: 2's. The route's ceiling (SERVER_FRAGMENT_BATCH_LIMIT) bounds a lone record.
MAX_FRAGMENT_BODY_BYTES = 2 * MAX_BODY_BYTES
#: The route's budget for a protocol-3 batch (the gateway's
#: `MAX_FRAGMENT_BODY_BYTES`): one event's text can ride three times, as the
#: fragment's message, its index Line, and the event while the server asks for
#: events. Protocols 1 and 2 keep SERVER_BATCH_LIMIT.
SERVER_FRAGMENT_BATCH_LIMIT = 6_000_000
MAX_SOLO_FRAGMENT_BYTES = SERVER_FRAGMENT_BATCH_LIMIT - ENVELOPE_HEADROOM


def _fragment_module():
    """The vendored fragment builder, or None where it cannot run.

    Imported on use, never at module load, and any failure is contained:
    protocol 2 must keep working on an interpreter or install where the
    vendored engine code does not import (it is the engine's code, written
    for the engine's Python)."""
    try:
        from ... import atif_fragment
    except Exception:  # noqa: BLE001
        return None
    return atif_fragment


def _builder_versions(module) -> tuple[int, ...]:
    """The fragment versions a vendored builder can build (one before v2)."""
    versions = getattr(module, "FRAGMENT_VERSIONS", None)
    return tuple(versions) if isinstance(versions, tuple) else (module.FRAGMENT_VERSION,)


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


class FragmentBuilderMixin:
    """Part of Journal: its methods run with `self` the Journal; compose() folds them into Journal."""

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
from ..errors import DeliveryPending  # noqa: E402 -- pieces need each other; the front door loads this one first
from ..stream_protocol import PROTOCOL_FRAGMENTS  # noqa: E402 -- pieces need each other; the front door loads this one first
