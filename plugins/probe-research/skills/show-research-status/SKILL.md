---
name: show-research-status
description: Show where the research stands, in one view — a summary of the current project (what it is, what is tracked in it, what is missing), the session's tracking state, and the whole arc as a timeline with the current position marked and the next action named. Use when someone asks where they are, what comes next, what is tracked, what this project contains, how far along this is, or to see the plan — and unprompted BEFORE a training run starts, when the cost of taking a stage out of order is highest. Also when arriving in an unfamiliar project, at handoff, and after a run closes. Not a substitute for registering or recording work; that is track-work.
---

# Show research status

A researcher about to act can see the command in front of them and nothing
else — not what this session is filed under, not what the project already
holds, not which stage of the arc they are standing in. Probe holds every one
of those facts and hands them back one entity at a time. This skill spends the
reads once and renders the answer: first the state, then the arc.

## 1. Read the state before you draw it

Every line is a claim. Make the reads first, cheapest to dearest, and stop as
soon as the picture is decided:

```
probe session status                           # tracking on/off, active project, live run
browse(scope="project:<id>")          # experiments, run_count, active_run_count
probe notes show                               # what the last session decided
entity(ref="project:<id>", view="summary") # authored Markdown below AI Summary
entity(ref="run:<id>", view="handoff")     # series, span_types, artifact_total
entity(ref="run:<id>", view="reproduce")   # env_ref, execution_record, missing
entity(ref="experiment:<id>", view="versions")   # published or not
probe outbox status                            # queued vs delivered
```

`browse` alone answers the common case; fetch the run views only when
a run exists and its interior matters. **Never mark anything from memory of
what you did this session** — what you wrote and what landed are different
claims, and writes queue: `probe outbox status` must be clean, or the run
closed through `probe run end`, before a stage or a count is stated as done.

## 2. The status block

Render this first — it is the part a timeline cannot carry:

```
STATUS  ·  <project name> — <one-line description>
  tracking   on · this session files under <project> · run <slug> live
  tracked    4 experiments · 31 runs (2 active) · 18 files · notes 6d ago
  code       Acme-Lab/Train@odyssey3 · 1,2k-commit timeline · 1 suggested repo
  missing    2 experiments have no summary · last run has no snapshot ⚠
  attention  <the single most important caveat from the notes, verbatim-short>
```

Four lines, each from a read, none from recall:

- **tracking** — `probe session status`: on/off, the project this session
  files under, whether one of its runs is live. If tracking is OFF, say so
  here and render the rest anyway — the state is still real, this session is
  just not adding to it.
- **tracked** — counts from `browse` and the project card: experiments,
  runs (active), files (artifact counts across anchors), how fresh the notes
  are. What EXISTS.
- **code** — which GitHub repository/branch the project's code lives in, from
  the project card's `code` block (`entity(view="code")` has the commit
  timeline itself). Include a suggested repo when one is waiting for a
  confirm — a run captured code from it and nobody has said yes yet. Omit the
  line when the project has no code sources; never guess a repo from prose.
- **missing** — the gaps a reader would want flagged: experiments without a
  question or summary, a running run with no snapshot, `outbox` undelivered,
  a project with no description. What SHOULD exist and does not. Derive only
  from reads; an empty line is fine.
- **attention** — the sharpest live caveat from the project's notes, one line.
  Omit the line rather than inventing one.

## 3. The arc — one timeline, not two

The science (curate, train, evaluate, ablate) and the tracking (question,
snapshot, close, publish) interleave in real time — a snapshot that happens
after the run started is a missed snapshot. One track, in the order things
actually have to occur.

Where each mark comes from — do not guess at any row, read it:

| stage | done when | read it from |
| --- | --- | --- |
| project | it resolves | `browse`, or the project card |
| question | `question` is non-null | experiment card, or a run's `reproduce` |
| reuse check | the name resolves at the shared level | `entity(ref="artifact:<name>", view="versions")` |
| snapshot | `env_ref` set **and** the record resolves | `reproduce` — `execution_record` in `missing` means NOT done |
| inputs captured | `0 unavailable` | `probe snapshot-restore RUN --verify-only` |
| run open | status `running` | run card |
| logging | `series` is non-empty | `handoff` |
| in-run phase | which `span_types` have counts, which `series` prefixes exist | `handoff` |
| outputs | `artifact_total` > 0 | `handoff` |
| eval | an `eval/*` series or an eval span exists | `handoff` |
| close | status is `completed` / `failed` / `crashed` / `canceled` | run card |
| publish | the experiment has a version | experiment `versions` |

Stages ahead that are NOT in the table — "ablate the KL grid", "write it up" —
come from the researcher's own brief. Draw them, but only the table can
produce a `✓`.

### Glyphs

```
✓  done, with its evidence under the label
▶  where the work is now
○  ahead, not started
?  Probe has no signal — unknown, NOT "not done"
!  out of order or at risk — the focus line says why
```

`?` and `○` are the pair that matters. A missing snapshot on a run already
training is not a `○` waiting its turn; it is a `!` — the moment to take it
has passed.

### Draw it

Left to right on a **13-column grid**: the marker row, twelve connectors per
cell; under each marker its label, under that ONE evidence token — each up to
12 characters, blank where there is nothing. Then a focus line for the current
stage. Solid `━` behind the work, light `─` ahead, so progress registers
before a single label is read. Spend the 12 characters on the real word
(`question`, not `hypoth`).

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

**Wrap, never shrink.** About eight stages fill the grid at 99 columns; when
the next cell would push past, end the track in `━━━→` and continue on a fresh
block, repeating nothing across the break. Do not narrow cells or drop middle
stages — the stages ahead are what the researcher came for. Drop the evidence
line entirely when no stage has a token worth writing.

## 4. Close with one next action

One command, the one that advances the `▶` stage — not a menu. If the next
action is a decision rather than a command, say that in one line and stop; do
not invent a command to have something to print.

## Redraw, do not narrate

Redraw when a stage flips, before a launch, and at handoff — a redrawn view is
cheaper to read than a paragraph about what changed. Do not draw one for work
with no arc: fixing a data loader bug is a bug, not an arc.

## What this is not

- **Not a write.** It reads and renders. Registering what it shows as missing,
  or recording what it shows as unlogged, is `track-work`.
- **Not a status report to file.** It goes in the session, where the
  researcher is about to act. A durable claim about the work belongs in the
  project's notes (`probe notes append`).
- **Not a plan of record.** The stages ahead came from the brief and change as
  the work does; when one turns out wrong, note it and redraw — do not defend
  the picture.
