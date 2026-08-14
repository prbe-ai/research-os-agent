---
name: show-research-timeline
description: Draw the whole research arc as one timeline in the session, with the current position marked and the next action named. Use when someone asks where they are, what comes next, how far along this is, what still has to happen, or to see the plan — and unprompted BEFORE a training run starts, when the arc ahead is invisible and the cost of taking a stage out of order is highest. Also use when arriving in an unfamiliar project, when handing work to another person or session, and after a run closes. Not a substitute for opening or recording a run; those are start-research-work and track-research-work.
---

# Show research timeline

A researcher about to launch a training run can see the command in front of them and
nothing else — not what the run has to have on it before it starts, not what happens
to the result afterwards, not which of those steps already happened in this project
last week. Probe holds every one of those facts and hands them back one entity at a
time. This skill spends those reads once and draws the answer.

**One timeline, not two.** The science (curate, train, evaluate, ablate) and the
tracking (hypothesis, snapshot, close, publish) are not parallel tracks — they
interleave in real time, and a snapshot that happens after the run started is a
missed snapshot. Rendering them as separate bands hides exactly that. Put every
stage on one track, in the order it actually has to occur.

## 1. Read the state before you draw it

Every mark on the timeline is a claim. Make the reads first, cheapest to dearest,
and stop as soon as the arc is decided:

```
browse_research(scope="project:<id>")          # experiments, run_count, active_run_count
probe notes show                               # what the last session decided
get_entity(ref="run:<id>", view="handoff")     # series, span_types, artifact_total
get_entity(ref="run:<id>", view="reproduce")   # env_ref, execution_record, missing
get_entity(ref="experiment:<id>", view="versions")   # published or not
```

`browse_research` alone answers the common case — "this project has four experiments
and nothing running" is most of a timeline. Only fetch the run views when a run
exists and its interior matters.

**Never mark a stage from memory of what you did this session.** What you wrote and
what landed are different claims, and the timeline is read as evidence. If a write
went out with `--async`, `probe outbox status` must be clean before its stage can be
drawn as done.

## 2. Where each mark comes from

The track is derivable. Do not guess at any row in this table — read it.

| stage | done when | read it from |
| --- | --- | --- |
| project | it resolves | `browse_research`, or the project card |
| hypothesis | `hypothesis` is non-null | experiment card, or a run's `reproduce` |
| reuse check | the name resolves at the shared level | `get_entity(ref="artifact:<name>", view="versions")` |
| snapshot | `env_ref` set **and** the record resolves | `reproduce` — `execution_record` in `missing` means NOT done |
| inputs captured | `0 unavailable` | `probe snapshot-restore RUN --verify-only` |
| run open | status `running` | run card |
| logging | `series` is non-empty | `handoff` |
| in-run phase | which `span_types` have counts, which `series` prefixes exist | `handoff` |
| outputs | `artifact_total` > 0 | `handoff` |
| eval | an `eval/*` series or an eval span exists | `handoff` |
| close | status is `completed` / `failed` / `crashed` / `canceled` | run card |
| publish | the experiment has a version | experiment `versions` |

The stages ahead of the current one that are NOT in that table — "ablate the KL
grid", "write it up" — come from the researcher's own brief in this session. Draw
them, because the arc ahead is the entire point of the timeline, but they earn no
completion mark from inference: only the table above can produce a `✓`.

## 3. Glyphs

Five marks, one meaning each:

```
✓  done, with its evidence under the label
▶  where the work is now
○  ahead, not started
?  Probe has no signal — unknown, NOT "not done"
!  out of order or at risk — the focus line says why
```

`?` and `○` are the pair that matters. A missing snapshot on a run that is already
training is not a `○` waiting its turn; it is a `!`, because the moment to take it
has passed and the run is now unreproducible. Absence stops being informative once
nobody records which kind of absence it was.

## 4. Draw it

Left to right, because the arc is a sequence in time and the reader's question is
"how much of this is behind me". A vertical list answers that by counting; a track
answers it at a glance.

Three lines make the track, on a **13-column grid**: the marker, then twelve
connectors. Under each marker its label, and under that ONE evidence token — each
up to 12 characters, blank where there is nothing to show. Then a focus line for
the current stage, carrying the detail no cell has room for.

Twelve characters is enough for the real word. Spend it: `hypothesis`, not
`hypoth`; `SFT baseline`, not `SFT`. Abbreviating to buy back width costs the
reader more than the width was worth, and a track nobody can parse at a glance has
lost the only argument for drawing it horizontally.

**The connector carries the progress.** Segments behind the work are solid `━`,
segments ahead are light `─`, so how far along this is registers before a single
label is read. The count on the title line says the same thing exactly.

Mid-run, in a project with history:

```
RESEARCH ARC  ·  bird-sql-agentic-rl / grpo-kl-sweep                                    4 of 8 done
───────────────────────────────────────────────────────────────────────────────────────────────────
 ✓━━━━━━━━━━━━✓━━━━━━━━━━━━✓━━━━━━━━━━━━✓━━━━━━━━━━━━▶────────────○────────────?────────────○
 curate data  SFT baseline hypothesis   snapshot     RL — GRPO    close run    eval         publish
 12,481 rows  exec-acc 65  kl .02 v .04 verified     step 4,120                no eval/*
───────────────────────────────────────────────────────────────────────────────────────────────────
 ▶ GRPO · tunneling-sambar-254 · step 4,120 · reward + kl_div · 312 rollout spans
 NEXT  probe artifact add tunneling-sambar-254 ckpt-4000.pt --name ckpt
```

Before anything is opened — the case this skill exists for. Most of the track is
light, and that is the useful part of the picture:

```
RESEARCH ARC  ·  folding  ·  nothing opened yet                            1 of 7 done
──────────────────────────────────────────────────────────────────────────────────────
 ✓━━━━━━━━━━━━▶────────────○────────────○────────────○────────────○────────────○
 project      hypothesis   reuse check  snapshot     train        eval         publish
 3 exps, idle              dockq scorer              ~6h 8×H100
──────────────────────────────────────────────────────────────────────────────────────
 ▶ hypothesis · no experiment for this question yet
 NEXT  probe experiment create dockq-sweep --project folding \
         --hypothesis "temp 0.7 beats 1.0"
```

**Wrap, never shrink.** About eight stages fill the grid at 99 columns, which is as
wide as a terminal can be relied on to be — fewer when the labels run long, since
the label row is what overhangs. When the next cell would push past it, end the
track in `━━━→` and continue on a fresh block, one blank line between. Repeat
nothing across the break — a stage drawn twice reads as two stages:

```
 ✓━━━━━━━━━━━━✓━━━━━━━━━━━━✓━━━━━━━━━━━━✓━━━━━━━━━━━━✓━━━━━━━━━━━━✓━━━━━━━━━━━━✓━━━→
 curate       dedup        tokenize     SFT          reward model hypothesis   snapshot
 1.2M rows    840k rows    llama-3      exec-acc 65  pairwise     kl .02 v .04 verified

 ▶────────────○────────────?────────────○────────────○
 RL — GRPO    close run    eval         ablate kl    publish
 step 4,120                no eval/*
```

Do not narrow the cells to fit, and do not drop stages from the middle to shorten
the line. The stages ahead are what the researcher came for; a track that elides
them to stay in one block has thrown away its own subject.

Drop the evidence line entirely when no stage has a token worth writing. An empty
third line is a stripe of whitespace that says nothing.

## 5. Close with one next action

One command, the one that advances the `▶` stage. Not a menu of three. The timeline
already showed everything downstream; repeating it as options undoes the ordering
the picture just established.

If the next action is a decision rather than a command — which of two arms to run,
whether the eval is trustworthy — say that in one line instead, and stop. Do not
invent a command to have something to print.

## Redraw, do not narrate

Draw it again when a stage flips, before a launch, and at handoff. A redrawn
timeline is cheaper to read than a paragraph explaining what changed, and it stays
correct — prose about progress goes stale the moment the next step lands.

Do not draw one for work with no arc. Fixing a data loader bug is not an arc;
it is a bug. This skill fires for the team's ML work whose stages connect —
training, evaluation, sweeps, a survey feeding a build, data processing feeding
a run — and a timeline for a single-step task is decoration.

## What this is not

- **Not a write.** It reads and renders. Creating the project or experiment the
  timeline shows as missing is `start-research-work`; recording anything it shows
  as unlogged is `track-research-work`.
- **Not a status report to file.** It goes in the session, where the researcher is
  about to act. A durable claim about the work — why this arc, what got ruled out —
  belongs in the project's `NOTES.md`.
- **Not a plan of record.** The stages ahead came from the brief and change as the
  work does. When one of them turns out to be wrong, say so in `NOTES.md` and
  redraw; do not defend the picture.
