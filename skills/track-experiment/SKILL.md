---
name: track-experiment
description: Box and track scientific training, evaluation, docking, scoring, sweep, simulation, or other result-producing work. Use before launching, resuming, interpreting, checkpointing, completing, or handing off a research experiment. Do not use for ordinary editing, formatting, installation, or unit tests unless they are evidence inside an active experiment.
---

# Track an experiment

1. Orient before the first meaningful action. `browse_research` if you do not know
   what is in this project yet; `search_knowledge` if you have terms and want prior
   work on this specific thing. Check what is already RUNNING before you launch
   anything — `browse_research` reports `active_run_count`, and duplicate GPU-hours
   are the expensive mistake here.
2. Reuse the active run when its intent matches. Otherwise create one with
   `probe run start`, an explicit hypothesis, and a deterministic external id.
3. **Reuse before you create.** Before writing or materially changing a reusable
   script, scoring method, dataset, config, image, checkpoint or container
   definition, call `get_entity(ref="asset:<name>", view="versions")`. Reuse an
   exact version or pin a new version of the SAME asset; start a new identity only
   when nothing compatible exists (see manage-research-asset). Never edit a
   published version in place.
   A name that does not exist errors. A name that exists with no version satisfying
   your `filters={"requirement": ...}` returns `state="no_match"` **plus the
   versions that do exist** — that is a real version ceiling, not an absent asset,
   and the difference decides whether you pin, bump, or start fresh.
4. Run `probe snapshot RUN_ID` before launch (captures code + env, pins `env_ref`).
   Record W&B, scheduler, pod, image and storage ids with `probe link` as they
   appear (they land on the run's `foreign_keys`).
5. Upload metrics, spans and outputs through `probe log` (`--dim key=value` for
   per-actor / per-device series), `probe span add`, `probe artifact add`. Capture
   intent, decisions, observations, failures, results, deviations and next steps
   with `probe note add`.
6. **Read back what you recorded before relying on it** — `get_entity` with
   `view="trajectory"` for the spans, `view="metrics"` for the series. What you
   wrote and what landed are different claims, and only the second is evidence.
7. Before handoff or completion, read `view="handoff"` or `view="reproduce"`.
   Report missing capture honestly: `completeness.missing` is the answer, not your
   recollection of what you logged.
8. Finish the run with its real lifecycle outcome (`completed` / `failed` /
   `crashed` / `canceled`). Mint an immutable experiment version only through the
   publication workflow.

If the run reports liveness (`probe` heartbeats it), keep heartbeating for the whole
run or not at all: a run that beats once and then stops is reaped as `crashed`.

Do not invoke `probe hook ...`; those are reserved for deterministic coding-agent hooks.

## Choosing a view

`get_entity` carries the full view matrix in its own description, and `card` (the
default) returns `available_views` for whatever you just fetched — so ask the tool,
do not memorise a table that can go stale. This file deliberately does NOT repeat
the matrix: it lived in three places, and the copies drifted.

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
