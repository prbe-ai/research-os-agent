---
name: track-research-work
description: Record an open run — metrics, spans, artifacts, notes, asset versions, and its final status. Use while a run is in flight, when reading back what was captured, or before handoff, completion, or publication.
---

# Track research work

Opening the run is `start-research-work`. This is everything after.

1. **Capture as it happens.** `probe log` for metrics (`--dim key=value` for
   per-actor / per-device series), `probe span add` for structure, `probe artifact
   add` for outputs. Record intent, decisions, observations, failures, results,
   deviations and next steps with `probe note add`.

2. **Read back what you recorded before relying on it.** `get_entity` with
   `view="trajectory"` for the spans, `view="metrics"` for the series. What you wrote
   and what landed are different claims, and only the second is evidence.

3. **Version reusable outputs; do not copy them.** An asset is an artifact with a
   version chain — upload with `probe artifact add`, pin with `probe artifact
   version-add`. The reuse check that decides whether you are pinning a version or
   opening a new identity is step 3 of `start-research-work`; the syntax is in
   `reference.md`.

4. **Before handoff or completion**, read `view="handoff"` or `view="reproduce"`.
   Report missing capture honestly: `completeness.missing` is the answer, not your
   recollection of what you logged.

5. **Close with the real outcome** — `completed` / `failed` / `crashed` / `canceled`.
   Minting an immutable experiment version is a separate act the researcher asks for
   explicitly; see `reference.md`.

If the run reports liveness (`probe` heartbeats it), keep heartbeating for the whole
run or not at all: a run that beats once and then stops is reaped as `crashed`.

Do not invoke `probe hook ...`; those are reserved for deterministic coding-agent hooks.

## Choosing a view

`get_entity` carries the full view matrix in its own description, and `card` (the
default) returns `available_views` for whatever you just fetched — so ask the tool,
do not memorise a table that can go stale. This file deliberately does NOT repeat the
matrix: it lived in three places, and the copies drifted.

The judgement that is not in the tool description:

- Ask for the **narrowest** view that answers your question. `card` first; it is
  cheapest and tells you what else exists.
- Narrow with `filters` rather than reading everything and skimming — they run
  server-side.
- `handoff`'s `span_types` counts tell you whether a `trajectory` call is worth
  making at all.

**Trust the envelope over your own optimism.** `completeness.state="partial"` plus
`missing[]` names exactly what you did not see. When `next_cursor` is set there ARE
more rows — pass it back with the SAME view, or say you read only a prefix. Never
report "no spans" or "no lineage" when what you got was a partial envelope.
