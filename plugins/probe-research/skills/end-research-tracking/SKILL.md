---
name: end-research-tracking
description: Stop tracking THIS conversation in Probe Research — no further projects, experiments, runs, notes or artifacts, and no more tracking nudges. Use when the researcher says to stop tracking, that this session is not research, or asks to turn tracking off; and when a conversation that began as research has turned into something else (debugging the shell, writing docs, an unrelated errand). Does not delete anything already recorded, and does not stop transcript capture. Reverse with `probe session track`.
---

# End research tracking for this conversation

The researcher is telling you this conversation is not research work. Believe them
and stop, in that order: flip the switch first so the state is durable even if the
session ends here, then say what changed.

## 1. Turn it off

```
probe session untrack
```

Per SESSION, not per machine — the next conversation is unaffected. Reversible
with `probe session track`.

## 2. Stop tracking, for the rest of this conversation

From here on, in this session ONLY:

- Do **not** create projects, experiments or runs, and do not open one "just in
  case". Do not write project notes or Project Summary Markdown.
- Do **not** invoke `start-research-work` or `track-research-work`, and do not
  suggest them.
- Do **not** raise tracking at all — not as a reminder, not as a caveat, not as a
  closing line. The researcher already decided; repeating it is the nagging this
  switch exists to end.
- Keep doing the actual work you were asked to do. This turns off the bookkeeping,
  not the help.

This overrides the standing tracking instructions in CLAUDE.md/AGENTS.md **for
this conversation**. That is the whole point of the switch: those rules are
deliberately broad, and a researcher needs a way to say "not this one" without
arguing with them.

## 3. Say what happened, once

One line. What it did, what it did not do, and how to undo it:

> Tracking is off for this session — nothing further will be recorded. What was
> already recorded is untouched. `probe session track` turns it back on.

## What this does NOT do

- **It does not delete anything.** Work recorded before the switch stays exactly
  where it is, because it happened. Removing it would rewrite the research record
  to match a later mood, which is the opposite of what a lab notebook is for.
- **It does not stop transcript capture.** The tap that ships session transcripts
  has no per-session off switch — only a machine-wide one — so this cannot and
  does not disable it. If the researcher wants capture off too, say so plainly
  rather than letting them assume this covered it: that is `probe wizard`, whose
  capture toggle affects EVERY session on the machine, not just this one.
- **It does not touch other sessions**, and it does not persist to the next one.

## If they change their mind

```
probe session track
```

Everything resumes, including what was already known about the session — the
record was suspended, not discarded.
