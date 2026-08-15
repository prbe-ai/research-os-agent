---
name: research-tracking
description: Turn Probe research tracking OFF or back ON for THIS conversation. Use when the researcher says to stop tracking, that this session is not research, to turn tracking off, or to resume it; and when a conversation that began as research has turned into something else (debugging the shell, writing docs, an unrelated errand). Off stops further projects, experiments, runs, notes and artifacts and stops all tracking nudges; on resumes normal tracking. Does not delete anything already recorded, and does not stop transcript capture. This is the switch — `start-research-work` is what actually opens and records work.
---

# Research tracking: on or off for this conversation

A switch, nothing else. `start-research-work` creates projects, experiments and
runs; `track-research-work` records into them. This decides whether either should
be happening here at all.

**Read the argument.** `off` (or "stop", "disable") turns tracking off; `on` (or
"start", "resume") turns it back on; no argument means report the state and ask.

## Turning it OFF

The researcher is telling you this conversation is not research work. Believe them
and stop, in that order: flip the switch first so the state is durable even if the
session ends here, then say what changed.

```
probe session untrack
```

Then, for the rest of this conversation:

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

Say it once, in one line — what it did, what it did not do, how to undo it:

> Tracking is off for this session — nothing further will be recorded. What was
> already recorded is untouched. `/research-tracking on` turns it back on.

## Turning it ON

```
probe session track
```

Normal tracking resumes, including the standing rules: from here on
`start-research-work` applies again and fires on its own when the work warrants
it. Nothing needs replaying — the record was suspended, not discarded, so whatever
this session had already recorded is still there and still attributed to it.

Say so in one line, then carry on with the work rather than opening a project to
prove the switch worked.

## No argument given

Report, then offer — do not guess which way they meant.

```
probe session status
```

It prints whether tracking is on or off, the project this session is filed under
if any, and whether one of its runs is live. Read it back in a sentence and ask
which they want.

## What OFF does NOT do

- **It does not delete anything.** Work recorded before the switch stays exactly
  where it is, because it happened. Removing it would rewrite the research record
  to match a later mood, which is the opposite of what a lab notebook is for.
- **It does not stop transcript capture.** The tap that ships session transcripts
  has no per-session off switch — only a machine-wide one — so this cannot and
  does not disable it. If the researcher wants capture off too, say so plainly
  rather than letting them assume this covered it: that is `probe wizard`, whose
  capture toggle affects EVERY session on the machine, not just this one.
- **It does not touch other sessions**, and it does not persist to the next one.

## Two states, one signal

The status line shows exactly two things: `tracking` (with the project once one
exists) or `not tracking`. There is no third "off" state, because a reader does not
care WHY nothing is being recorded — only whether anything is.

What decides it is this signal. Explicit on or off wins in both directions; with no
decision yet, it falls back to what the session has actually recorded, so a session
that quietly created a project reads as tracking without anyone announcing it.

## Who flips it

**Both of you, through the same surface.** The agent decides — that is what keeps
tracking from depending on someone remembering to ask, and it should turn tracking
ON as soon as it judges the work to be research, rather than waiting for the first
project to land. The researcher decides too, and their decision is the one that
stands: if they turn it off, it stays off until they say otherwise.

Do not argue with an explicit off. Do not wait to be told to turn it on.
