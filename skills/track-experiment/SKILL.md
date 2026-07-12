---
name: track-experiment
description: Box and track scientific training, evaluation, docking, scoring, sweep, simulation, or other result-producing work. Use before launching, resuming, interpreting, checkpointing, completing, or handing off a research experiment. Do not use for ordinary editing, formatting, installation, or unit tests unless they are evidence inside an active experiment.
---

# Track an experiment

1. Call `research_context` with the task and current project before the first meaningful experiment action.
2. Reuse the active run when its intent matches. Otherwise create a run with `exp run start`, an explicit hypothesis, and a deterministic external id.
3. Before creating or materially changing a reusable script, method, dataset, config, image, checkpoint, or container definition, call `research_resolve`. Reuse an exact version or pin a new version of the same asset; only start a new identity when no compatible match exists (see the manage-research-asset workflow). Never edit a published version in place.
4. Run `exp snapshot RUN_ID` before launch (captures code + env, pins `env_ref`). Record W&B, scheduler, pod, image, and storage ids with `exp link` as soon as they appear (they land on the run's `foreign_keys`).
5. Upload metrics, spans, and outputs through `exp log` (use `--dim key=value` for per-actor / per-device series), `exp span add`, and `exp artifact add`. Capture meaningful intent, decisions, observations, failures, results, deviations, and next steps with `exp note add`.
6. Before handoff or completion, call `research_get` with the `handoff` or `reproduce` view and report missing capture honestly.
7. Finish the run with its real lifecycle outcome (`completed` / `failed` / `crashed` / `canceled`). Mint an immutable experiment version only through the publication workflow.

Do not invoke `exp hook ...`; those commands are reserved for future deterministic coding-agent hooks.
