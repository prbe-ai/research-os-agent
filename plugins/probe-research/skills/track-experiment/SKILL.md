---
name: track-experiment
description: Box and track scientific training, evaluation, docking, scoring, sweep, simulation, or other result-producing work. Use before launching, resuming, interpreting, checkpointing, completing, or handing off a research experiment. Do not use for ordinary editing, formatting, installation, or unit tests unless they are evidence inside an active experiment.
---

# Track an experiment

1. Call `research_context` with the task and current project before the first meaningful experiment action.
2. Reuse the active run when its intent matches. Otherwise create a run with `probe run start`, an explicit hypothesis, and a deterministic external id.
3. Before creating or materially changing a reusable script, method, dataset, config, image, checkpoint, or container definition, call `research_resolve`. Reuse an exact version or pin a new version of the same asset; only start a new identity when no compatible match exists (see the manage-research-asset workflow). Never edit a published version in place.
4. Run `probe snapshot RUN_ID` before launch (captures code + env, pins `env_ref`). Record W&B, scheduler, pod, image, and storage ids with `probe link` as soon as they appear (they land on the run's `foreign_keys`).
5. Upload metrics, spans, and outputs through `probe log` (use `--dim key=value` for per-actor / per-device series), `probe span add`, and `probe artifact add`. Capture meaningful intent, decisions, observations, failures, results, deviations, and next steps with `probe note add`.
6. Read back what you recorded before you rely on it — `research_get view="trajectory"` for the spans themselves, `view="metrics"` for the series. What you wrote and what landed are different claims, and only the second one is evidence.
7. Before handoff or completion, call `research_get` with `view="handoff"` or `view="reproduce"`. Report missing capture honestly: `completeness.missing` in the response is the answer, not your recollection of what you logged.
8. Finish the run with its real lifecycle outcome (`completed` / `failed` / `crashed` / `canceled`). Mint an immutable experiment version only through the publication workflow.

Do not invoke `probe hook ...`; those commands are reserved for future deterministic coding-agent hooks.

## Which `research_get` view to ask for

`research_get(ref, view=..., filters=..., token_budget=..., cursor=...)` — one entity, one
purpose-shaped payload. `ref` is `run:<id>`, `experiment:<id>`, `project:<id>`, `group:<id>`,
or a bare id. Ask for the NARROWEST view that answers the question; asking for a view a kind
does not have is an error that names the ones it does.

| What you want to know | Ask for |
|---|---|
| does it exist, what state is it in | `view="card"` (default, cheapest — start here) |
| what the run actually DID (rollouts, tool calls, turns) | `view="trajectory"` on a run |
| how the numbers moved | `view="metrics"`; add `filters={"key": "loss"}` for that series' raw points |
| what came out of it | `view="artifacts"` (a run's also takes `filters={"kind": ..., "step_from": ..., "step_to": ...}`) |
| can I re-run this exactly | `view="reproduce"` on a run — hypothesis + `env_ref` resolved to its execution record |
| I am a new session, catch me up | `view="handoff"` on a run (its artifact list is bundle-capped — `missing: ["artifacts_beyond_bundle_limit"]` means read `view="artifacts"` for all of them) |
| what produced or consumed this | `view="lineage"` (a run's ancestry; an experiment's edges) |
| what happened to this run, in order | `view="events"` on a run |
| what sweeps/ensembles exist | `view="groups"` on an experiment, then `ref="group:<id>"` for one |
| what has been published | `view="versions"` on an experiment |

Narrow with `filters` rather than reading everything and skimming — trajectory takes
`span_type` / `parent_span_id` / `step_from` / `step_to`, and they run server-side.

**Trust the envelope over your own optimism.** `completeness.state="partial"` plus
`missing[]` names exactly what you did not see; `handoff`'s `span_types` counts tell you
whether a `trajectory` call is even worth making. When `next_cursor` is set there ARE more
rows — pass it back with the SAME view to continue, or say you only read a prefix. Do not
report "no spans" or "no lineage" when what you actually got was a partial envelope.
