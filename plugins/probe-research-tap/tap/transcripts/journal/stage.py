"""reserve the session's next batch (scrub, gate, pack) or return the one still pending."""

from __future__ import annotations
import hashlib
import json
from contextlib import nullcontext, suppress
from pathlib import Path

from .. import capture_gate
from ..sanitizers.claude_code import call_models
from ..transcripts import nesting_exceeds
from ..pinned import open_transcript
MEDIA_SPOOL_CAP_BYTES = 512 * 1024 * 1024


class StageMixin:
    """Part of Journal: its methods run with `self` the Journal; compose() folds them into Journal."""

    def stage(
        self,
        session_id: str,
        *,
        cwd: str,
        finalize: bool = False,
        historical_only: bool = False,
        max_body_bytes: int | None = None,
        solo_limit: int | None = None,
        packing_cap: int | None = None,
        require_no_pending: bool = False,
        expected_history: tuple[int, str] | None = None,
        pasted_images: bool = False,
    ) -> bytes | None:
        """Reserve the session's next batch, or return the one still pending.

        `pasted_images`: this machine lets images the researcher pasted be
        uploaded (the tap reads `defaults.pasted_images` in the Probe config
        each pass). They are, only where the session's state also says the
        server takes them (`media`) and, on protocol 3, the stream builds a
        fragment version that carries them.
        """
        with self.transaction():
            state = self.get(session_id)
            if state is None:
                raise ReconciliationRequired("session has no validated source reservation")
            if state.get("deleted"):
                raise SessionDeleted(session_id, newly=False)
            _validate_history(state, expected_history)
            if not state.get("cwd") and cwd and Path(cwd).is_absolute():
                state["cwd"] = cwd
            self._validate_source(state, Path(state["path"]))
            pending = self.pending(session_id)
            if pending:
                _require_safe_pending(pending)
                if require_no_pending:
                    raise ReconciliationRequired("another transcript delivery was reserved during reconciliation")
                return pending
            # Probe off: nothing new while the switch reads `off` (or an off
            # stretch is still open); the lines of its stretches go as nothing.
            gate = capture_gate.verdict(session_id)
            if gate.blocked:
                return None
            skip = gate.gaps
            historic = bool(state.get("snapshot_path") and not state.get("historical_complete"))
            if historical_only and state.get("historical_complete"):
                return None
            path = Path(state["snapshot_path"] if historic else state["path"])
            if historic and prefix_hash(path, state["historical_end"]) != state["historical_hash"]:
                raise ReconciliationRequired("historical snapshot changed")
            protocol = _stream_protocol(state)
            # Protocol 3: the fragment builder, and whether events go beside it.
            module = self._fragment_builder(protocol)
            send_events = module is None or bool(state.get("send_events"))
            version = _stream_fragment_version(state, module) if module is not None else None
            # Rows an older tap left when it delivered the batch naming them
            # (it knows no `media`): no pending batch names them any more.
            self.conn.execute("DELETE FROM media WHERE session_id NOT IN (SELECT session_id FROM pending)")
            extract = _pasted_extractor(self.source) if pasted_images else None
            images_since = getattr(module, "IMAGES_SINCE", None) if module is not None else None
            if extract is not None and state.get("media") is True and (
                module is None or (images_since is not None and version >= images_since)
            ):
                spool: dict[str, tuple[str, bytes]] = {}
                room = MEDIA_SPOOL_CAP_BYTES - self.conn.execute(
                    "SELECT COALESCE(sum(size),0) FROM media"
                ).fetchone()[0]
            else:
                extract = None
                spool, room = {}, 0  # unread without `extract`; passed to the line reader
            # A lone event may use the whole ROUTE budget (the protocol's); a
            # packed batch may only use the packing target. A caller that lowers
            # `max_body_bytes` to exercise batching still gets the real solo
            # allowance unless it says otherwise, because the two answer
            # different questions: how much to pack, versus what the server
            # will take.
            if max_body_bytes is None:
                max_body_bytes = MAX_BODY_BYTES if module is None else MAX_FRAGMENT_BODY_BYTES
                if module is not None and packing_cap is not None:
                    # A caller's byte budget (`probe import --transcripts-budget-mb`)
                    # below protocol 3's larger target would otherwise never
                    # see one batch fit. The solo ceiling is unchanged.
                    max_body_bytes = min(max_body_bytes, packing_cap)
            if solo_limit is None:
                solo_limit = max(
                    max_body_bytes,
                    MAX_SOLO_EVENT_BYTES if module is None else MAX_SOLO_FRAGMENT_BYTES,
                )
            end = state["source_byte_end"]
            lines = state["source_line_end"]
            event_no = state["event_end"]
            events: list[dict] = []
            size = 2048  # envelope + bounded source provenance
            records = 0  # source lines that retained an event
            marks: list[_Mark] = []  # after each packed line, to give lines back
            sanitize = _sanitizer(self.source)  # an unknown source refuses, never guesses
            read_title = _title_reader(self.source)
            markers = _call_model_markers(self.source)
            # A call's usage line names no model; an earlier line does. Kept
            # per session, saved with the cursor, and one copy per packed
            # line, so a line given back is read again from the models before it.
            models = _resume_call_models(state, path, sanitize, markers, end) if markers else None
            models_at_start = models.snapshot() if models is not None else None
            kept_models: list[dict | None] = []
            end, event_no, lines, records = self._read_batch_lines(
                end=end, event_no=event_no, events=events, extract=extract, historic=historic,
                kept_models=kept_models, lines=lines, marks=marks, max_body_bytes=max_body_bytes,
                models=models, module=module, path=path, read_title=read_title, records=records,
                room=room, sanitize=sanitize, send_events=send_events, size=size, skip=skip,
                solo_limit=solo_limit, spool=spool, state=state, version=version,
            )
            at_end = end == path.stat().st_size
            final = (
                not events and end == state["source_byte_end"] and at_end and (finalize or historic)
            )
            if end == state["source_byte_end"] and not final:
                return None
            if final and state["finalized"]:
                if historic:
                    state["historical_complete"] = True
                    self._save(state)
                return None
            while True:
                body = {
                    "protocol_version": protocol,
                    "session_id": session_id,
                    "stream_id": state["stream_id"],
                    "batch_seq": state["last_seq"] + 1,
                    "source_byte_start": state["source_byte_end"],
                    "source_byte_end": end,
                    "source_line_start": state["source_line_end"],
                    "source_line_end": lines,
                    "event_start": state["event_end"],
                    "event_end": event_no,
                    "prefix_sha256": prefix_hash(path, end),
                }
                if module is not None:
                    # Every protocol-3 batch, finalize included: the gateway requires it.
                    body["fragment_version"] = version
                if final:
                    body["finalize"] = True
                else:
                    body["cwd"] = state.get("cwd") or cwd
                    if state["last_seq"] == -1 and state.get("provenance"):
                        body["provenance"] = state["provenance"]
                if historic:
                    body["snapshot_byte_end"] = state["historical_end"]
                    body["snapshot_sha256"] = state["historical_hash"]
                if module is None:
                    if not final:
                        body["events"] = events
                    encoded = canonical_payload(_scrub_content(body))
                    # One event that could not be packed is allowed the route's budget;
                    # anything packed stays inside the smaller target.
                    envelope_limit = solo_limit if len(events) <= 1 else max_body_bytes
                    break
                envelope_limit = solo_limit if records <= 1 else max_body_bytes
                if final:
                    encoded = canonical_payload(_scrub_content(body))
                    break
                # The batch's events are scrubbed TOGETHER first (adjacent
                # messages are scanned as one text, as protocol 2 does), each
                # fragment is built from its scrubbed event, and the fragments
                # are then scrubbed to their own fixed point. Scrubbing a body
                # without its events gives the same fragments: the scrubber
                # never joins text across keys.
                scrubbed = _scrub_content({"events": events})["events"]
                body["fragments"] = [_fragment(module, event, version) for event in scrubbed]
                content = _scrub_content(body)
                if send_events:
                    content["events"] = scrubbed
                encoded = canonical_payload(content)
                if len(encoded) > envelope_limit and records <= 1 and send_events:
                    # One record whose event does not fit beside its fragments:
                    # this batch goes without events (optional per batch) rather
                    # than not at all.
                    del content["events"]
                    encoded = canonical_payload(content)
                if len(encoded) <= envelope_limit or records <= 1:
                    break
                # Lines were packed by an estimate (their unscrubbed records);
                # the built batch is what counts. Give back trailing lines
                # worth the overshoot and build it again.
                over = len(encoded) - envelope_limit
                while len(marks) > 1 and over > 0:
                    over -= marks.pop().size - marks[-1].size
                    kept_models.pop()
                count, end, lines, event_no, size, records = marks[-1]
                del events[count:]
            if len(encoded) > envelope_limit:
                raise DeliveryPending("transcript envelope exceeds the batch budget")
            used = self.conn.execute(
                "SELECT COALESCE(sum(length(body)),0) FROM pending"
            ).fetchone()[0]
            if used + len(encoded) > self.cap_bytes:
                raise DeliveryPending("transcript queue full; pending bytes preserved")
            self.conn.execute(
                "INSERT INTO pending(session_id,body,digest) VALUES(?,?,?)",
                (session_id, encoded, hashlib.sha256(encoded).hexdigest()),
            )
            if extract is not None and spool:
                # The bytes of exactly the images this batch names (lines
                # given back above name theirs again in the next batch).
                for digest in sorted(named_media(json.loads(encoded)).intersection(spool)):
                    media_type, data = spool[digest]
                    self.conn.execute(
                        "INSERT INTO media(session_id,sha256,media_type,size,bytes) VALUES(?,?,?,?,?)",
                        (session_id, digest, media_type, len(data), data),
                    )
            if models is not None:
                # The models at this batch's end: the next batch starts there.
                state[CALL_MODELS_STATE] = {
                    "at": end,
                    "models": kept_models[-1] if kept_models else models_at_start,
                }
            # The reservation is represented by this row. It is committed together
            # with state; no source cursor can pass it until its receipt arrives.
            self._save(state)
            return encoded


    def _read_batch_lines(
        self, *, end, event_no, events, extract, historic, kept_models, lines, marks,
        max_body_bytes, models, module, path, read_title, records, room, sanitize, send_events,
        size, skip, solo_limit, spool, state, version,
    ):
        """Read the transcript from the cursor into this batch: its events, until the batch is full.
        Returns (end, event_no, lines, records) where the read stopped."""
        with call_models(models) if models is not None else nullcontext(), open_transcript(path) as handle:
            handle.seek(end)
            while True:
                # Written while Probe was off: read past, sent as nothing.
                skipped = bool(skip) and capture_gate.in_gap(skip, end)
                raw = handle.readline(MAX_SOURCE_LINE + 1)
                length = len(raw)
                if length > MAX_SOURCE_LINE:
                    # Too long to read (D16): skipped to its newline and
                    # said, never held, so the capture goes on. It used to
                    # stop this session's capture for good.
                    if not raw.endswith(b"\n"):
                        length = _rest_of_line(handle, length)
                    raw = b""
                    if length is None:
                        if historic:
                            raise DeliveryPending(
                                "historical transcript has an incomplete trailing line"
                            )
                        break  # still being written: read it whole next pass
                    retained = [] if skipped else [oversize_line_event(length)]
                else:
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        if historic:
                            raise DeliveryPending(
                                "historical transcript has an incomplete trailing line"
                            )
                        break
                    line = None
                    try:
                        if skipped:
                            value = None
                        else:
                            line = json.loads(raw) if raw.strip() else None
                            value = sanitize(line) if raw.strip() else None
                        if value is not None and nesting_exceeds(value):
                            value = None  # the redactor below would hit the recursion limit
                    except (ValueError, UnicodeDecodeError):
                        value = None
                    except MemoryError:
                        raise  # may pass; never drop a line for it
                    except Exception:  # noqa: BLE001
                        # A sanitizer that trips on one hostile or unforeseen
                        # line costs that line, never the session: the cursor
                        # never moves past an exception, so raising here would
                        # stop this session's capture for good.
                        value = None
                    if read_title is not None and line is not None:
                        named = read_title(line)
                        if named is not None:
                            # Kept beside the batch, never in it (`send_title`).
                            # A /rename outranks the generated title lines that
                            # keep following it.
                            state["custom_title" if named[1] else "ai_title"] = named[0]
                    retained = (
                        value if isinstance(value, list) else ([] if value is None else [value])
                    )
                    if extract is not None and retained:
                        # A shape the reader did not foresee costs the
                        # line's images (placeholders), never the line.
                        with suppress(ValueError, TypeError, KeyError, AttributeError, IndexError):
                            room = _attach_pasted(extract(line), retained, spool, room)
                additions = [
                    {"line_no": event_no + i, "raw": event} for i, event in enumerate(retained)
                ]
                if module is None:
                    increment = len(json.dumps(additions, separators=(",", ":")).encode())
                    solo = increment
                else:
                    fragment_bytes, event_bytes = _record_bytes(additions)
                    increment = fragment_bytes + (event_bytes if send_events else 0)
                    # Events are optional per protocol-3 batch: a record
                    # that must go alone is held to the budget by its
                    # fragments, and goes without its events if need be.
                    solo = fragment_bytes
                if size + increment > max_body_bytes:
                    if not events:
                        # NOTHING TO PACK IT BESIDE, so the packing target
                        # does not apply -- only what the route will take.
                        # Refusing here on `max_body_bytes` rejected batches
                        # the server would have accepted, and did it
                        # permanently: one such event stranded its whole
                        # session on every retry forever.
                        if solo > solo_limit and module is not None:
                            # Rare, so exact: the estimate may be high.
                            solo = _encoded_bytes(
                                [_fragment(module, event, version) for event in additions]
                            )
                        if solo > solo_limit:
                            raise DeliveryPending(
                                "sanitized event exceeds the gateway batch budget; "
                                "source retained"
                            )
                    else:
                        break
                events.extend(additions)
                event_no += len(additions)
                size += increment
                end += length
                lines += 1
                records += bool(additions)
                marks.append(_Mark(len(events), end, lines, event_no, size, records))
                kept_models.append(models.snapshot() if models is not None else None)
                # Bound reads even when the sanitizer drops everything.
                if end - state["source_byte_end"] >= 4 * 1024 * 1024:
                    break
        return end, event_no, lines, records
from .errors import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    DeliveryPending,
    ReconciliationRequired,
    SessionDeleted,
    _validate_history,
)
from .harness import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    CALL_MODELS_STATE,
    _call_model_markers,
    _pasted_extractor,
    _resume_call_models,
    _sanitizer,
    _title_reader,
)
from .integrity import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    _require_safe_pending,
    _scrub_content,
    canonical_payload,
    prefix_hash,
)
from .media.media import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    MAX_SOURCE_LINE,
    _attach_pasted,
    _rest_of_line,
    named_media,
    oversize_line_event,
)
from .stream_protocol import _stream_fragment_version, _stream_protocol  # noqa: E402 -- pieces need each other; the front door loads this one first
from .stream_protocols.events import MAX_BODY_BYTES, MAX_SOLO_EVENT_BYTES  # noqa: E402 -- pieces need each other; the front door loads this one first
from .stream_protocols.fragments import (  # noqa: E402 -- pieces need each other; the front door loads this one first
    MAX_FRAGMENT_BODY_BYTES,
    MAX_SOLO_FRAGMENT_BYTES,
    _Mark,
    _encoded_bytes,
    _fragment,
    _record_bytes,
)
