"""Canonical home of the tap's transcript data contract.

The probe-research-tap plugin ships sanitized per-session transcripts to the
Probe knowledgebase. The backfill CLI ships *historical* transcripts
through the same ingest door, and the two paths must produce identical wire
shapes — same sanitizer, same batch chunking, same byte-cursor discipline —
or the same session recorded twice tells two different stories.

The tap is a separate distribution living in the agent's plugin cache: the CLI
cannot import it, and the plugin cannot import probe. So the shared modules
live here canonically and are VENDORED into the plugin as one tree,
byte-identical, at `tap/transcripts/` by `make sync-tap-core`:

    sanitizers/        one sanitizer per coding agent (claude_code, codex, pi, kimi_code)
    transcripts.py     JSONL tail/split/validate + batch chunking + byte cursor
    render.py          a merged transcript as the prose that gets read
    journal/           the session journal (protocols 2 and 3), split by job
    credential_scan/   the credential scanner every batch passes through

tests/test_tap_core_sync.py fails when the copies drift. Edit the canonical
files here, never the plugin copies. Cross-module imports inside these files
must stay RELATIVE (`from .sanitizers import ...`) so the same bytes resolve in
both packages; the only module outside the tree they reach is `compose`, which
the tap carries too. Same contract as sync-plugin-policy / sync-telemetry-core /
sync-session-marker, with the canonical direction unchanged: canonical in
src/probe, copy in the plugin.
"""
