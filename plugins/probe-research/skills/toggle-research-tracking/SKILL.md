---
name: toggle-research-tracking
description: Toggle Probe research tracking for THIS conversation — invoked bare it flips to the opposite of the current state; `on`/`off` set a state explicitly; `status` only reports. Use when the researcher says to stop tracking, that this session is not research, to turn tracking off or back on; and when a conversation that began as research has turned into something else (debugging the shell, writing docs, an unrelated errand). Off stops further projects, experiments, runs, notes and artifacts and stops all tracking nudges; on resumes normal tracking. Does not delete anything already recorded, and does not stop transcript capture. This is the switch — `start-research-work` is what actually opens and records work.
---

# Research tracking: the toggle for this conversation

A switch, nothing else. `start-research-work` creates projects, experiments and
runs; `track-research-work` records into them. This decides whether either
should be happening here at all.

**The flip has already happened.** A plugin hook watches this skill's
activation — a typed command via UserPromptSubmit, a Skill/SlashCommand tool
call via PostToolUse — and writes the session's tracking signal the moment it
fires: invoked bare, it flips to the OPPOSITE of the current state; invoked
with `off` or `on` (or "stop"/"disable", "start"/"resume") it sets exactly
that state; `status` and anything else write nothing. So do not decide the
new state yourself — read it back:

```
probe session status
```

Trust what it prints, and never write the signal yourself — the hook is the
only writer, and `probe session toggle`/`track`/`untrack` are not this
skill's tools. If what status prints contradicts what the researcher plainly
asked for, that is a broken hook: say so in one line and report the state as
it actually is, rather than repairing it — a quiet model-side fix would keep
a broken hook invisible on every machine it ships to. Per SESSION, not per
machine: the next conversation is unaffected.

## If it landed OFF

For the rest of this conversation:

- Do **not** create projects, experiments or runs, and do not open one "just in
  case". Do not write project notes or Project Summary Markdown.
- Do **not** invoke `start-research-work` or `track-research-work`, and do not
  suggest them.
- Do **not** raise tracking at all — not as a reminder, not as a caveat, not as
  a closing line. The researcher already decided; repeating it is the nagging
  this switch exists to end.
- Keep doing the actual work you were asked to do. This turns off the
  bookkeeping, not the help.

This overrides the standing tracking instructions in CLAUDE.md/AGENTS.md **for
this conversation**. That is the whole point of the switch: those rules are
deliberately broad, and a researcher needs a way to say "not this one" without
arguing with them.

Say it once, in one line — what it did, what it did not do, how to undo it:

> Tracking is off for this session — nothing further will be recorded. What was
> already recorded is untouched. `/toggle-research-tracking` flips it back on.

## If it landed ON

Normal tracking resumes, including the standing rules: from here on
`start-research-work` applies again and fires on its own when the work warrants
it. Nothing needs replaying — the record was suspended, not discarded, so
whatever this session had already recorded is still there and still attributed
to it. Say so in one line, then carry on with the work rather than opening a
project to prove the switch worked.

## `status`, or no direction the hook could read

Nothing was flipped. Read `probe session status` back in a sentence — whether
tracking is on or off, the project this session is filed under if any, and
whether one of its runs is live — and ask which way they want it.

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

## Who decides what

Starting is the AGENT's call and stays that way: tracking happens because the
standing rules make it automatic, not because someone remembered to ask.
Stopping is the RESEARCHER's call, which is why this skill exists. Do not
invert that by waiting to be told to track.
