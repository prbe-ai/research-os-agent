---
name: visualize-progress
description: Renders progress of the current project/work tracked in Probe - meant to be used to help the user visualize what is going on.
---
# Visualize research progress

## THE ONE RULE:

**Every line is a CLAIM that needs a READ**, never a memory of this session.
Writes QUEUE: the daemon's answer must show it, or the run closed via
`probe run end`, before a stage or count is stated as done.

## READS:

The Probe daemon reads the team's work for you. Ask it ONCE per drawing:
`probe ask --wait '<question>'` with a long shell timeout - single quotes so a
backtick does not run as a command, and no `'` inside the question. Name the
project when a read gave it to you, otherwise ask about this session's work;
never guess a name. Ask for every fact the two tables below need, each with its
id or number.
THIS MACHINE'S state comes from the CLI - `probe session status` for tracking.

A fact the answer does not give is `?`. A reply with no facts (`not asked: ...`,
`asked (...)`, `still looking ...`, `ask ... failed`) is not an empty project: do
what it says, or say you could not look and draw nothing - never draw from
memory or as if no work exists.

## THE STATUS BLOCK:

Render this first - the timeline cannot carry it. **Every line must be able to
change what happens next; a line that cannot is not rendered.** Four lines is
the ceiling and two is the usual render.

```
STATUS  ·  bird-sql-agentic-rl — RL fine-tuning for SQL agents
  missing    last run has no snapshot ⚠ · 2 experiments have no summary
  attention  <the sharpest live caveat from the notes, verbatim-short>
```

| line | render it |
| --- | --- |
| `missing` | what SHOULD exist and does not - no question, no summary, a running run with no snapshot |
| `attention` | the sharpest live caveat from the project's notes, one line. Omit rather than INVENT |
| `tracking` | ONLY when it is not the quiet default - `read only (daemon)`, or a run live. If read only say so and render the rest ANYWAY. From `probe session status` |
| `tracked` | ONLY on the first render of a project - counts orient an arrival and are noise on a redraw |
| `code` | ONLY when actionable - a suggested repo waiting for confirm, or no code source at all. A commit count is trivia, and a repo is never GUESSED from prose |

Nothing to put on any line means nothing is wrong - just omit it from the
rendering.

Emit it inside a fence. The columns are the readability, and markdown outside
a fence reflows them.


## THE TIMELINE:

Research steps (curate, train, evaluate) and tracking steps (question,
snapshot, close, publish) take turns - a snapshot belongs BEFORE the run
starts, not beside it. Draw them as ONE track, in the order they actually
happen.

| stage | done when |
| --- | --- |
| project | it resolves |
| question | `question` is non-null |
| reuse check | the name resolves at the shared level |
| snapshot | `env_ref` set **and** the execution record resolves |
| inputs captured | no read in this profile - always `?` |
| run open | status `running` |
| logging | `series` is non-empty |
| in-run phase | which `span_types` have counts, which `series` prefixes exist |
| outputs | `artifact_total` > 0 |
| eval | an `eval/*` series or an eval span exists |
| close | status is `completed` / `failed` / `crashed` / `canceled` |
| publish | the experiment has a version |

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
 NEXT  probe.log_artifact("ckpt", path="ckpt-4000.pt") in the training loop
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

- **Not a write.** What it shows as missing or unlogged is the daemon's to
    record - a NEXT that is the daemon's write is not yours to run, so say the
    daemon records it. Yours is your run's own data (`probe exec`, `probe log`,
    `probe run end`, the SDK).
- **Not a report to file.** It goes in the session; the daemon records what
    lasts.
- **Not a plan of record.** Brief-derived stages change as the work does -
    when one turns out wrong, note it and redraw.
