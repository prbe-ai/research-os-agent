---
name: visualize-progress
description: Render where the work stands in one view - the current project (what it is, what is tracked, what is missing), this session's tracking state, and the arc as a timeline with the current position marked and the next action named. One pass of cheap reads; it writes nothing. Use when someone asks where they are, what comes next, what is tracked, how far along something is, or to see the plan. Trigger UNPROMPTED whenever a high-level view would change what happens next - arriving in an unfamiliar project or repo, BEFORE a training run or sweep starts (a stage taken out of order costs most there), when a run ends, at handoff, when the user asks a broad question about the work rather than a narrow one, and before proposing what to do next. When the picture has moved, redraw it rather than describing the change in prose. Not for a narrow code or debugging question; registering or recording is track-work.
---
# Visualize research progress

## THE ONE RULE:

**Every line is a CLAIM that needs a READ**, never a memory of this session.
Writes QUEUE: `probe outbox status` must be clean, or the run closed via
`probe run end`, before a stage or count is stated as done.

## READ THE STATE, CHEAPEST FIRST:

```
probe session status                              # tracking on/off, active project, live run
browse(ref="project:<id>")                        # experiments, run_count, active_run_count
probe notes show                                  # what the last session decided
entity(refs=["project:<id>"], view="summary")     # authored Markdown below AI Summary
entity(refs=["run:<id>"], view="handoff")         # series, span_types, artifact_total
entity(refs=["run:<id>"], view="reproduce")       # env_ref, execution_record, missing
entity(refs=["experiment:<id>"], view="versions") # published or not
probe outbox status                               # queued vs delivered
```

Stop once the picture is decided: `browse` alone answers the common case, and
run views are worth fetching only when a run EXISTS and its interior matters.

## THE STATUS BLOCK:

Render this first - a timeline cannot carry it.

```
STATUS  ·  <project name> — <one-line description>
  tracking   on · this session files under <project> · run <slug> live
  tracked    4 experiments · 31 runs (2 active) · 18 files · notes 6d ago
  code       Acme-Lab/Train@odyssey3 · 1,2k-commit timeline · 1 suggested repo
  missing    2 experiments have no summary · last run has no snapshot ⚠
  attention  <the single most important caveat from the notes, verbatim-short>
```

- **tracking** - `probe session status`: on/off, the project this session
    files under, whether a run is live. If OFF, say so and render the rest
    ANYWAY.
- **tracked** - what EXISTS, from `browse` and the project card: experiments,
    runs (active), files across anchors, note freshness.
- **code** - the repository/branch from the project card's `code` block
    (`entity(view="code")` has the timeline), plus a suggested repo waiting
    for confirm. Omit the line when there are no code sources; never GUESS a
    repo from prose.
- **missing** - what SHOULD exist and does not: experiments without a question
    or summary, a running run with no snapshot, undelivered outbox, a project
    with no description. Reads only; an empty line is fine.
- **attention** - the sharpest live caveat from the notes, one line. Omit
    rather than INVENT.

## THE ARC - ONE TIMELINE, NOT TWO:

Science (curate, train, evaluate, ablate) and tracking (question, snapshot,
close, publish) interleave: a snapshot after the run started is a MISSED
snapshot. ONE track, in the order things have to occur.

| stage | done when | read it from |
| --- | --- | --- |
| project | it resolves | `browse`, or the project card |
| question | `question` is non-null | experiment card, or a run's `reproduce` |
| reuse check | the name resolves at the shared level | `entity(refs=["artifact:<name>"], view="versions")` |
| snapshot | `env_ref` set **and** the record resolves | `reproduce` - `execution_record` in `missing` means NOT done |
| inputs captured | `0 unavailable` | `probe snapshot-restore RUN --verify-only` |
| run open | status `running` | run card |
| logging | `series` is non-empty | `handoff` |
| in-run phase | which `span_types` have counts, which `series` prefixes exist | `handoff` |
| outputs | `artifact_total` > 0 | `handoff` |
| eval | an `eval/*` series or an eval span exists | `handoff` |
| close | status is `completed` / `failed` / `crashed` / `canceled` | run card |
| publish | the experiment has a version | experiment `versions` |

Stages ahead not in the table ("ablate the KL grid", "write it up") come from
the researcher's brief. Draw them, but only the table produces a `✓`.

### Glyphs

```
✓  done, with its evidence under the label
▶  where the work is now
○  ahead, not started
?  Probe has no signal - unknown, NOT "not done"
!  out of order or at risk - the focus line says why
```

**`?` and `○` are the pair that matters.** A missing snapshot on a run already
training is not `○`; it is `!` - the moment has passed.

### Draw it

Left to right on a **13-column grid**: marker row, twelve connectors per cell;
under each marker its label, under that ONE evidence token - each up to 12
characters, blank where there is nothing. Then a focus line for the current
stage. Solid `━` behind the work, light `─` ahead. Spend the 12 characters on
the real word (`question`, not `hypoth`).

```
STATUS  ·  bird-sql-agentic-rl — RL fine-tuning for SQL agents
  tracking   on · files under bird-sql-agentic-rl · run tunneling-sambar-254 live
  tracked    3 experiments · 22 runs (1 active) · 41 files · notes 2h ago
  missing    no eval/* series yet on the live run

RESEARCH ARC  ·  bird-sql-agentic-rl / grpo-kl-sweep                     4 of 8 done
────────────────────────────────────────────────────────────────────────────────────
 ✓━━━━━━━━━━━━✓━━━━━━━━━━━━✓━━━━━━━━━━━━✓━━━━━━━━━━━━▶────────────○────────────?────────────○
 curate data  SFT baseline question   snapshot     RL — GRPO    close run    eval         publish
 12,481 rows  exec-acc 65  kl .02 v .04 verified     step 4,120                no eval/*
────────────────────────────────────────────────────────────────────────────────────
 ▶ GRPO · tunneling-sambar-254 · step 4,120 · reward + kl_div · 312 rollout spans
 NEXT  probe artifact add tunneling-sambar-254 ckpt-4000.pt --name ckpt
```

**Wrap, never shrink.** About eight stages fill 99 columns; when the next cell
would overflow, end the track in `━━━→` and continue on a fresh block,
repeating nothing. Do not narrow cells or drop middle stages. Drop the
evidence line entirely when no stage has a token worth writing.

## CLOSE WITH ONE NEXT ACTION:

One command, the one that advances the `▶` stage - not a menu. If the next
action is a DECISION rather than a command, say so in one line and stop.

**Redraw, do not narrate.** Redraw when a stage flips, before a launch, and at
handoff. Do not draw one for work with no arc: fixing a data loader bug is a
bug, not an arc.

## WHAT THIS IS NOT:

- **Not a write.** Registering what it shows as missing, or recording what it
    shows as unlogged, is `track-work`.
- **Not a status report to file.** It goes in the session; a durable claim
    belongs in the project's notes (`probe notes checkout` / `push`).
- **Not a plan of record.** The stages ahead came from the brief and change as
    the work does; when one turns out wrong, note it and redraw.
