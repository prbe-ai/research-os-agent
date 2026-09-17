---
name: visualize-progress
description: Renders progress of the current project/work tracked in Probe - meant to be used to help the user visualize what is going on. Trigger unprompted on high level discussions or unfamiliarity - broad questions about the work, on arriving in an unfamiliar project, before a run starts or after one ends, and when planning next steps, etc.
---
# Visualize research progress

## THE ONE RULE:

**Every line is a CLAIM that needs a READ**, never a memory of this session.
Writes QUEUE: `probe outbox status` must be clean, or the run closed via
`probe run end`, before a stage or count is stated as done.

## READS:

Team data comes from the MCP - `browse` for what exists, `entity(refs=[...],
view=...)` for one entity's interior. THIS MACHINE'S state has no MCP door and
comes from the CLI - `probe session status` for tracking, `probe outbox
status` for what has not landed yet.

## THE STATUS BLOCK:

Render this first - the timeline cannot carry it. **Every line must be able to
change what happens next; a line that cannot is not rendered.** Four lines is
the ceiling and two is the usual render.

```
STATUS  ·  bird-sql-agentic-rl — RL fine-tuning for SQL agents
  missing    last run has no snapshot ⚠ · 2 experiments have no summary
  attention  <the sharpest live caveat from the notes, verbatim-short>
```

| line | render it | read it from |
| --- | --- | --- |
| `missing` | what SHOULD exist and does not - no question, no summary, a running run with no snapshot, an undelivered outbox | `browse` + the cards |
| `attention` | the sharpest live caveat, one line. Omit rather than INVENT | the project card's notes excerpt - `view="notes"` for the whole document |
| `tracking` | ONLY when it is not the quiet default - tracking OFF, or a run live. If OFF say so and render the rest ANYWAY | `probe session status` |
| `tracked` | ONLY on the first render of a project - counts orient an arrival and are noise on a redraw | `browse`, and `view="summary"` for the authored Markdown |
| `code` | ONLY when actionable - a suggested repo waiting for confirm, or no code source at all. A commit count is trivia, and a repo is never GUESSED from prose | the project card's `code` block |

Nothing to put on any line means nothing is wrong - just omit it from the
rendering.

Emit it inside a fence. The columns are the readability, and markdown outside
a fence reflows them.


## THE TIMELINE:

Research steps (curate, train, evaluate) and tracking steps (question,
snapshot, close, publish) take turns - a snapshot belongs BEFORE the run
starts, not beside it. Draw them as ONE track, in the order they actually
happen.

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

Stages from the researcher's brief ("ablate the KL grid", "write it up") get
drawn too, but only a table stage earns a `✓`.

### Glyphs

```
✓  done, evidence under the label     ?  Probe has no signal - NOT "not done"
▶  where the work is now              !  out of order or at risk - focus line says why
○  ahead, not started
```

A missing snapshot on a run already training is `!`, not `○` - the moment has
passed, it is not still coming.

### Draw it

A **13-column grid**: each cell is one marker plus twelve connectors, solid
`━` behind the work and light `─` ahead. Under each marker its label, under
that ONE evidence token, both up to 12 characters - real words (`question`,
not `hypoth`), blank where there is nothing. Close with a focus line for the
current stage.

```
STATUS  ·  bird-sql-agentic-rl — RL fine-tuning for SQL agents
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

**Wrap, never shrink.** Eight stages fill about 99 columns; when the next cell
would overflow, end the track in `━━━→` and continue on a fresh block,
repeating nothing. Never narrow a cell or drop a middle stage. Drop the
evidence row when no stage has a token worth writing.

## CLOSE WITH ONE NEXT ACTION:

One command - the one that advances `▶`, not a menu. If what comes next is a
DECISION, say so in one line and stop.

**Redraw, do not narrate.** Redraw when a stage flips, before a launch, and at
handoff. Work with no arc gets no drawing - fixing a data loader bug is a bug.

## WHAT THIS IS NOT:

- **Not a write.** What it shows as missing or unlogged is registered by
    `track-work`.
- **Not a report to file.** It goes in the session; a durable claim goes to
    `probe notes checkout` / `push`.
- **Not a plan of record.** Brief-derived stages change as the work does -
    when one turns out wrong, note it and redraw.
