"""protocol 2: batches of sanitized events (the retired-for-new-streams protocol the server still takes)."""

from __future__ import annotations
MAX_BODY_BYTES = 1024 * 1024

#: What the INGEST ROUTE actually accepts, from `app/ingestion/sessions_router.py`
#: (`MAX_BODY_BYTES = 2_000_000`; the ingress in front of it allows 25m, so the
#: app is the binding limit). MAX_BODY_BYTES above is this client's PACKING
#: target and is deliberately smaller -- batching to the server's exact ceiling
#: leaves no room for the envelope and turns a good batch into a 413.
#:
#: The two are separated because one event that does not fit the packing target
#: used to be fatal rather than merely awkward: with no room to pack it beside
#: anything, `stage` refused, the session never imported, and no retry could
#: change that -- while the server would have taken it happily. A lone event is
#: now allowed the whole route budget, less the envelope around it.
SERVER_BATCH_LIMIT = 2_000_000
#: Envelope overhead: eleven small scalars, a session/stream id pair, a cwd, a
#: sha256, and (on the first batch) provenance. Measured at well under 2KB;
#: 16KB is slack, not an estimate, because being wrong here costs a 413 on a
#: batch that cannot be split.
ENVELOPE_HEADROOM = 16 * 1024
MAX_SOLO_EVENT_BYTES = SERVER_BATCH_LIMIT - ENVELOPE_HEADROOM
