---
name: track-research-work
description: Record an open run — per-step metrics, spans, artifacts, notes, asset versions, and its final status. Use while a run is in flight, when reading back what was captured, or before handoff, completion, or publication.
---

# Track research work

Opening the run is `start-research-work`, which is also where the SDK-or-CLI choice
gets made. Record with the surface the run was opened with.

1. **Capture as it happens.**

   | | in the script (SDK) | from a shell (CLI) |
   |---|---|---|
   | metrics | `run.log({"loss": l}, step=i)` | `probe log RUN loss=0.4 --step 100` |
   | per-device / per-actor | `run.log_hw({"gpu_temp": 88}, device=3)` | `probe log RUN gpu_temp=88 --dim device=3` |
   | structure | `with run.span("rollout", ...) as s:`, `run.step(i)` | `probe span add RUN --type rollout` |
   | outputs | `run.log_artifact("ckpt", path=...)` | `probe artifact add RUN PATH --name ckpt` |
   | notes | `client.notes.add(run.id, "decision", "…")` | `probe note add RUN --kind decision --statement "…"` |
   | external ids | `run.link(wandb_run_id="abc")` | `probe link RUN --set wandb_run_id=abc` |
   | sub-runs | `run.child("fold-2")` | `probe run child RUN --name fold-2` |

   Prefer the `with` form for spans in live code: it takes both timestamps off one
   clock, nests anything opened inside it, and closes the span as `failed` if the
   body raises. Spans have no heartbeat and no reaper, so one abandoned by an
   exception stays `running` forever otherwise.

   Only the left column can be called from inside the training loop, so `step=` is a
   real curve there and a scattering of points anywhere else. In the SDK, omitting
   `step=` auto-increments (per metric kind), so a bare `run.log({"loss": l})` in a
   loop still plots; pass `step=None` when there genuinely is no step and the points
   belong on the wall-clock axis.

   `run.log()` takes values of any type. Numbers plot; strings, dicts, lists and
   None are stored on that step's record instead and come back under
   `view="trajectory"`. So `run.log({"loss": l, "phase": "eval"})` is one call —
   but a durable claim about the work still belongs in a note, not a metric key. Record intent,
   decisions, observations, failures, results, deviations and next steps as notes
   either way — that is the same record from both surfaces.

   **Never block on delivery: async mode.** Any CLI write above takes `--async`
   (or `PROBE_ASYNC=1` for a whole session): the write is queued in a durable
   local outbox and the command returns immediately; a background drainer
   uploads with retries. Prefer it whenever you do not need the server's
   response — a stuck network then costs you nothing. Prefer `--reference` over
   uploading bytes when the file lives on storage the team can already resolve
   (shared volume, workstation path): it is a single queued record, no staging
   at all. Three rules keep async honest:
   - Queued is not delivered. `probe outbox status` (exit 0 = all delivered)
     before treating a missing recent write as absent anywhere, including MCP
     reads.
   - `probe run end` (WITHOUT --async) is the barrier: it delivers that run's
     queued items first and refuses to close the run while any cannot be. With
     `--async` it instead queues the close BEHIND the run's data — ordering is
     the barrier, and nothing blocks.
   - Failures surface on later commands (a stderr `outbox:` banner) and in
     `probe outbox status` / `probe doctor`; `probe outbox retry` requeues
     dead-lettered items after you fix the cause.

2. **Read back what you recorded before relying on it.** `get_entity` with
   `view="trajectory"` for the spans, `view="metrics"` for the series. What you wrote
   and what landed are different claims, and only the second is evidence. In
   async mode, `probe outbox status` must be clean before a read-back can prove
   anything.

3. **Version reusable outputs; do not copy them.** An asset is an artifact with a
   version chain — upload it (`run.log_artifact` / `probe artifact add`), then pin
   with `probe artifact version-add`. The reuse check that decides whether you are
   pinning a version or opening a new identity is step 4 of `start-research-work`;
   the syntax is in `reference.md`.

4. **Before handoff or completion**, read `view="handoff"` or `view="reproduce"`.
   Report missing capture honestly: `completeness.missing` is the answer, not your
   recollection of what you logged.

5. **Close with the real outcome** — `completed` / `failed` / `crashed` / `canceled`,
   via `run.finish("failed")` or `probe run end RUN --status failed`. In-script,
   `with run:` closes the run for you and records `failed` when the loop raises.
   Minting an immutable experiment version is a separate act the researcher asks for
   explicitly; see `reference.md`.

Liveness follows the surface. An SDK handle beats for the life of its process and
stops when you finish it, so there is nothing to do. A CLI-opened run is detached and
does not beat at all — do not bolt a beat onto one you cannot keep beating for the
whole run: a run that beats once and then goes quiet is reaped as `crashed`.

Outcome changed the run's meaning (flaky, superseded, promoted)? Retro-tag it:
`probe run tag RUN_ID <tag> [--remove <tag>]` (also `experiment tag` / `project tag`).

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

**Trust the envelope over your own optimism.** What `completeness` and `next_cursor`
mean is written in `get_entity`'s description and the server instructions; read it
there rather than re-deriving it. What is not written there: when you cannot resolve
a partial read, name the part you did not see in the same breath as the finding it
qualifies, not in a caveat further down.
