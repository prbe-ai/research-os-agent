---
name: track-research-work
description: Record what the team's ML work produces, whatever its shape — Project Summary Markdown, hidden notes, per-step metrics, spans, artifacts, a run's final status. Log at the moment it happens, not at session end — a decision made or reversed, a data processing run, data deleted or kept, a config change, a user override. Trigger unprompted whenever a run is open or something worth recording happens without one — a tool or dataset behaving differently than documented, substituted infrastructure, a number that measures nothing because the harness was broken. Also for reading back what was captured and before handoff or publication.
---

# Track research work

Opening the run is `start-research-work`, which is also where the SDK-or-CLI choice
gets made. Record with the surface the run was opened with.

1. **Capture as it happens.**

   | | in the script (SDK) | from a shell (CLI) |
   |---|---|---|
   | metrics | `run.log({"loss": l}, step=i)` | `probe log RUN loss=0.4 --step 100` |
   | per-device / per-actor | `run.log_hw({"gpu_temp": 88}, device=3)` | `probe log RUN gpu_temp=88 --dim device=3` |
   | nested structure | `with run.span("trial", ...) as s:`, `run.step(i)` | `probe span add RUN --type trial` |
   | outputs | `run.log_artifact("ckpt", path=...)` | `probe artifact add RUN PATH --name ckpt` |
   | external ids | `run.link(wandb_run_id="abc")` | `probe link RUN --set wandb_run_id=abc` |
   | sub-runs | `run.child("fold-2")` | `probe run child RUN --name fold-2` |
   | computed metrics | `run.log_derived_series("eval/auc", pts, producer="…")` | `probe log RUN eval/auc=0.9 --step 100 --derived --producer …` |
   | expression views | `run.create_view("loss_ratio", spec)` | `probe views create RUN loss_ratio --spec-file spec.json` |

   Spans are for work that NESTS -- a trial containing agent turns containing a
   verifier -- where the tree is the finding. A flat training loop is not that
   shape: step 12 is not inside step 11, and its phases belong in one metric
   series each (`perf/rollout`, `perf/train`), which plots as a curve instead of
   thousands of unnested tiles. `spans: 0` on a trainer run is usually correct.
   See `instrument-training-runs` for the full shape rule.

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
   `view="trajectory"`. So `run.log({"loss": l, "phase": "eval"})` is one call.

   **Decide the shape before you log.** A series is a key plus a dimension
   combination, so every distinct combination is a separate series — and a series
   holding one point has nothing to plot, so it renders as a scalar tile. Each
   metric is one of three shapes:

   | shape | how to log it | renders as |
   |---|---|---|
   | curve (loss, lr, reward over time) | one series, many points, differing in `step=` | a line chart |
   | headline scalar (final accuracy) | one series, one point, 0–2 low-cardinality dims | a stat tile |
   | breakdown (accuracy by category) | one series per category value, cardinality under ~20 | N tiles, or a grouped read |

   Dimensions are the LOW-CARDINALITY axes you intend to group by — split, seed,
   rank, category. Never an identifier: `example_id`, a row id, a uuid, a filename,
   a per-field name each mint their own one-point series, so 500 examples logged
   with `example_id` as a dimension is 500 series, 500 tiles and zero graphs. Only
   `step=` makes a curve; spreading values across a dimension does not. Per-sample
   ids go in `labels=` instead (POINT identity — it does not widen the series), and
   per-item detail goes in an artifact, which is what analysis code reads anyway.
   Budget it: series ≈ the product of your dimension cardinalities, and past ~50 you
   have designed a wall of tiles.

   **A LABELED POINT IS NEVER PLOTTED. This is the one that gets people, because
   moving an identifier out of dimensions and into labels looks like the fix and is
   only half of one.** Charts read the unlabeled stream only — the server filters on
   `labels_hash = <empty>` — so a point carrying ANY label is excluded from every
   graph before drawing. Labels exist to make a point addressable in the per-sample
   views, not to annotate a plottable one. Label every point and the chart says "No
   unlabeled points to plot", which reads like the run logged nothing while the data
   sits there intact.

   So a curve and per-sample identity are two different writes, and you make both:

   ```python
   for i, trial in enumerate(trials):
       run.log({"reward": trial.reward}, step=i)                    # THE CURVE: no labels
       run.log({"reward_sample": trial.reward}, step=i,             # the per-sample record
               labels={"instance_id": trial.id, "repo": trial.repo})
   ```

   Separate keys, deliberately: one key logged both ways gives you a series whose
   points are half plottable and a chart that silently shows a subset. If you only
   want one, keep the unlabeled curve — per-item detail belongs in an artifact
   anyway, and that is what analysis code reads.

   The rule generalises past this case. Anything that makes a point unique — a
   sample id, a repo name, a filename, a trial uuid — is fatal to a chart in BOTH
   fields: in `dimensions` it shatters one curve into hundreds of one-point series,
   and in `labels` it removes the point from plotting entirely. Neither field is
   where per-item identity earns its keep; an artifact is.

   Two that only bite later. The headline number must be exactly ONE series — a
   computed view resolves a key and refuses one carrying several dimension
   variants — so keep `accuracy` and `accuracy_per_example` as separate keys, and
   give the high-cardinality one its own `kind=` so the run page still shows the
   handful of numbers a human wants. And declare `agg="mean"` at the write, so
   every later reader gets the right reduction without having to name one.

   A durable claim about the work — why this approach, what you found, what a next
   session should not repeat — is prose, not a metric key. Prose has five homes, and
   picking the wrong one is why findings end up in commit messages instead:

   | what you are writing | where it goes |
   |---|---|
   | a short statement of what this project is | the PROJECT's description |
   | durable context a teammate should read on the dashboard | the editable Markdown suffix in the PROJECT Summary |
   | something the WHOLE TEAM needs, across projects | the TEAM note |
   | an operational handoff, caveat, decision or thing not to repeat | the PROJECT's notes |
   | the running record of one experiment — configs, results, conclusions | that EXPERIMENT's notes |
   | why THIS run's number should be distrusted | that RUN's notes |
   | what a sweep or a campaign of runs concluded | that GROUP's notes |
   | provenance about one file — where it came from, what is wrong with it | that ARTIFACT's notes |

   ```
   probe project get PROJECT | jq -r '.summary_markdown // ""' > PROJECT.md
   # Edit the complete file; preserve existing sections.
   probe project set PROJECT --summary @PROJECT.md
   probe project get PROJECT | jq -r '.summary_markdown // ""'  # verify

   probe notes team                       # the team's shared working memory
   probe notes append --team <<'EOF'      # every session is briefed with this
   Harbor has no generic-Kubernetes backend, so DOKS is out. Using GKE.
   EOF

   probe notes show                       # this project's operational briefing
   probe notes append <<'EOF'             # add to it, never clobber it
   The 2026-08 export supersedes the 07 one; rows 400-900 were duplicated.
   EOF

   probe notes append --experiment EXP --run RUN --group GRP --artifact ID
   probe notes edit --team --old "DOKS is out" --new "DOKS is out (retest 2027)"
   ```

   TWO WRITES, AND NEITHER IS A REWRITE. `append` adds a paragraph; `edit`
   replaces one exact span (`--new` omitted deletes it). Both are safe when other
   agents are writing the same document at the same moment, and neither asks you
   to hold the document in context. Do NOT read a note, change a line, and write
   the whole thing back: that is how the parts you did not think to repeat
   disappear. An `edit` whose `--old` does not match exactly once is refused with
   the match count — copy more surrounding text and retry, never guess.

   COMPACTING is editing. When a document gets long, fold finished appends up into
   its sections with `edit` rather than letting the bottom grow forever; the team
   note tells you how much room is left on every write.

   The dashboard Project Summary is one rendering with two owners: the server
   refreshes the AI narrative, while agents and people replace only
   `summary_markdown`, the durable suffix. The suffix is whole-document and
   last-write-wins: read it immediately before every edit, keep useful existing
   content, write the complete document, and verify the stored value afterwards.
   Never copy the AI narrative into the suffix or attempt to edit that server-owned AI
   section.

   **Embedding a repository's README.** A line containing only
   `[README](https://github.com/owner/repo)` renders that repository's README at
   that point in the document — a live view, refreshed when someone pushes a
   change to it, and readable for a PRIVATE repository when the team's GitHub App
   is installed on that account. Put it where a reader needs the repo's own words
   rather than restating them; keep your surrounding text for what the README does
   NOT say (why this project forked it, which parts matter here).

   TWO THINGS FOLLOW FROM THIS, and both matter when writing the suffix:
   the LINK TEXT is what makes it an embed, so an ordinary citation
   (`[the trainer](https://github.com/owner/repo)`, or a bare URL) stays a plain
   link and embeds nothing. And the project's `repo` field is DERIVED from this
   line — writing one connects the repository, removing it disconnects. Do not
   write `[README](...)` for a repository the project is not actually about.

   The project's hidden notes are one free-text markdown document, no schema, and an excerpt rides
   along on the project's MCP `card` — so the next agent sees it while orienting
   rather than having to know it exists. **That surfacing is why the project's notes
   are the default for operational handoff.** Run and group notes are read only by
   someone already looking at that row, so an operational claim that matters beyond
   one run belongs in the project's notes even if it was learned inside one. Use
   `--append` when others may be working the same project: a plain write is
   last-one-wins. Use the visible suffix instead when the reader is a teammate
   arriving on the dashboard rather than another agent resuming the work.

   Notes are NOT a second description. A description says what the thing IS and is
   written before it runs; notes say what a later reader should distrust, and are
   nearly always learned afterwards. With one field the two compete, and the caveat
   wins by destroying the description. All three take `@file` or `-` for stdin.

   Experiments have their OWN notes now, and they are the running experiment
   document: the configs you tried, the numbers they produced, what you concluded,
   accumulated while the work happens rather than written up afterwards. Claims
   about one experiment belong there, not in the project's notes — that redirect
   used to be necessary and mixed every experiment's record into one file.

   The launch context capture now records automatically — the exact argv, the seeds,
   the container, the determinism flags — is worth a note the moment it SURPRISES you:
   a seed you did not set, a non-deterministic op you had to allow, a flag you flipped
   to make a run finish. `probe run reproduce` will show a reader WHAT the launch was;
   only a note says why it was odd.

   **Data provenance: processing steps are runs, at script granularity.**
   When a session processes data — a transform, a filter, a dedupe, a
   re-scoring — open ONE project-direct run per script or stage VERSION, not
   per invocation: pass a deterministic `--external-id` (e.g.
   `clean-structures-v2`). A retried launch of a FAILED step resumes that run
   instead of minting a sibling; a COMPLETED step refuses the id, which is the
   system telling you to bump the version suffix — re-running a finished step
   means something changed. Attach the script as an artifact, link inputs and outputs with
   lineage edges (`consumes`/`produces`), and record the decision that shaped
   the step — the threshold chosen, the records deleted or kept, and why — in
   that run's notes at the moment it happens; conclusions that outlive the
   step go to the project's notes. Deletions are provenance, not housekeeping:
   log what was removed and why, or a later reader infers a processing chain
   that never existed. Done this way, a paper's method section is
   reconstructable from the chain of runs.

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

2. **Computing a metric nobody logged? You do not need a new run.** A finished run
   is not a sealed archive here — the series catalog is a row store, so a metric
   you only thought of afterwards (AUROC from stored predictions, a rescored eval,
   reward-under-the-curve) can land on the run it belongs to. Two doors, and the
   choice is about where the arithmetic happens:

   - **Derived metrics** — you compute it in Python and push the points.
     Anything is expressible, and the points are stored. `--producer` is
     mandatory: a derived series carries `origin="derived"` plus provenance
     (what produced it, what it read, where the code lives), so nobody later has
     to guess whether a curve came from the training loop or from a notebook two
     weeks after. Use `run.log_derived_series(key, points, producer=…)` for a
     whole curve in one request; `run.log_derived({...}, step=…, producer=…)`
     writes a single step. `step` is required — a backfill lands on steps that
     already exist, so there is nothing to auto-increment.
   - **Expression views** — a formula over series the run already has,
     stored as an AST and evaluated at READ time. No points are stored, so it
     costs nothing until someone looks and stays correct as a live run advances.
     Build the spec with `probe.expr` (`expr.series("train/loss") /
     expr.series("train/entropy")`, `.ema(factor=0.9)`, bare numbers coerce), or
     write the JSON yourself and pass `--spec-file` (`-` reads stdin).

   Reach for a view when the thing you want is arithmetic over existing curves;
   reach for a derived metric when it is anything else. `preview` before
   `create` — a spec naming a series the run never logged comes back with
   `missing_inputs` instead of becoming a panel that renders empty for everyone.

   The dashboard renders, renames and deletes views but does not compose them.
   Authoring them is your job, which is why both surfaces above are write doors
   and the MCP tools stay read-only.

3. **Read back what you recorded before relying on it.** `get_entity` with
   `view="trajectory"` for the spans, `view="metrics"` for the series. What you wrote
   and what landed are different claims, and only the second is evidence. In
   async mode, `probe outbox status` must be clean before a read-back can prove
   anything.

   After the FIRST run of any new logging code, assert the shape rather than
   eyeballing the dashboard — a bad shape is silent, since every call succeeds and
   the values are correct:

   ```python
   series = client.run_series(run.id)      # one row per (key, kind, dimensions)
   assert len(series) < 50, f"{len(series)} series — a wall of tiles, not graphs"

   # ...and that ANYTHING is plottable. The check above passes happily when every
   # point is labeled, which is exactly one blank chart instead of fifty.
   plottable = [s for s in series if s["point_count"] > 1 and not s["has_labeled_points"]]
   assert plottable, "every point is labeled or single — nothing will draw"
   ```

   Both assertions, not either. They fail on opposite mistakes and each one passes
   the other's: an identifier in `dimensions` trips the first, an identifier in
   `labels` trips only the second, and the half-fix that moves it from one to the
   other trips the second while making the first look repaired.

   If the series count is close to your point count, every series holds one point
   and you have logged tiles and no graphs. Fix the shape before spending compute
   on more runs. Do this on a 2-instance run, not after 300: the shape is identical
   and the mistake costs two minutes instead of an afternoon. When checking a breakdown pools the way you intended, pass an
   explicitly large `step_bucket` to `get_metrics_grouped` and confirm the rows
   come back with `n > 1`: the reduction buckets by step first, so points written
   at auto-incremented steps return one group per point and the grouping appears
   to do nothing.

4. **Version reusable outputs; do not copy them.** An asset is an artifact with a
   version chain — upload it (`run.log_artifact` / `probe artifact add`), then pin
   with `probe artifact version-add`. The reuse check that decides whether you are
   pinning a version or opening a new identity is step 4 of `start-research-work`;
   the syntax is in `reference.md`.

5. **Claim gate: before you report a run done or handoff-ready**, run `probe run check
   RUN` and state its verdict verbatim. The gate is machine-checkable — exit code 2 is
   `incomplete` — so it is the exit code, not your recollection of what you logged. If
   `incomplete`, fix it (usually a missing snapshot on a launch OUTSIDE `probe
   exec`/`run()` — see `start-research-work` step 5) or say why not, in the handoff
   note. `advisories` (no notes, no inputs-decision, a pre-capture-core run with no
   launch context) are worth knowing but never block the claim.

   To pull the whole record — for a handoff, or a question about how a past run was
   run — `probe run reproduce RUN` (MCP `view="reproduce"`) assembles everything
   reproduction needs: execution record, launch context, restore command, inputs,
   lockfiles, lineage and per-span environments. `probe experiment reproduce EXP` is
   the per-run map across an experiment. `completeness.missing` is the answer, never
   your optimism.

   A session that opened no run still ends. Append what you would do next and what is
   still unresolved to the project's notes, or planning work ends silently and the next session
   restarts from the brief.

6. **Close with the real outcome** — `completed` / `failed` / `crashed` / `canceled`,
   via `run.finish("failed")` or `probe run end RUN --status failed`. In-script,
   `with run:` closes the run for you and records `failed` when the loop raises.
   At an experiment's completion or publication, **freeze it**: `probe experiment
   freeze EXP --label L` mints an immutable version pinning exactly which runs and
   artifacts it comprised, so `probe experiment reproduce EXP --version N` resolves
   against that manifest forever — even after runs are edited or deleted. Do it when
   you publish, not "later"; see `reference.md`.

   **Status cannot say "the harness was broken", and must not be made to.** Those
   four values are LIFECYCLE — whether the process finished — and the reaper, the
   dashboard and every "is this run alive" check branch on them. A run whose verifier
   was wrong ran fine and is honestly `completed`; what is wrong is the NUMBER, not
   the execution. So mark it where meaning lives, not where lifecycle lives:

   ```
   probe run tag RUN invalid
   probe run set RUN --notes "Scored by pytest exit code, not per-test: 40 of 113
   P2P tests were uncollectable (pre-existing circular import), so the whole run
   reported 0.0. Re-scored per-test in smoke-oracle-pertest."
   ```

   The tag is what a reader scanning a list sees; the notes are why. Neither works
   alone — a bare `invalid` says "do not believe this" without saying what to believe
   instead, and notes with no tag are invisible until someone opens the row.

   Do this the moment the harness bug is found, not at the end. A streak of 0.0s that
   nobody marked reads as a result, and it is the most expensive kind of wrong: it
   looks like a finding, so the next person reasons from it.

7. **Close by handing back the link.** End with the run's dashboard URL — and one for
   everything else you created or closed this turn, not just the last one. `probe run
   end` prints it to stderr; MCP entities carry it as `url`. Echo what you were given
   — an assembled URL 404s as confidently as a real one. No `url`, no link.

   **In a script, print it yourself:** `print(run.url)` after `run.finish()`. The SDK
   will not — a library writing to stdout corrupts the job's own output — and a run
   that ends on a cluster reaches nobody otherwise.

Liveness follows the surface. An SDK handle beats for the life of its process and
stops when you finish it, so there is nothing to do. A CLI-opened run is detached and
does not beat at all — do not bolt a beat onto one you cannot keep beating for the
whole run: a run that beats once and then goes quiet is reaped as `crashed`.

Outcome changed the run's meaning (flaky, superseded, promoted, `invalid`)? Retro-tag
it: `probe run tag RUN_ID <tag> [--remove <tag>]` (also `experiment tag` /
`project tag`), and say why in `probe run set RUN_ID --notes`. A finished run is not
sealed — tags, description and notes all stay editable, so "we only understood this
afterwards" is never a reason to leave the record wrong.

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
