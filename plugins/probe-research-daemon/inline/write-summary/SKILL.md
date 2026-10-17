---
name: write-summary
description: Keep each project's and experiment's summary - a short, plain note people read to catch up.
---
Each project and experiment the work touches has a summary people read to catch up.

When the work moves to one, load it: `probe summary checkout --project <slug>` (or
`--experiment <slug>`). Edit that file, then `probe summary push` with the same flag. Write the
first one when you create it; then update on a result, a decision or a change of direction.

A TL;DR a smart 10-year-old could follow: short sentences, plain words, the numbers kept.
1. One sentence: what it is (an experiment: its question).
2. **Where it stands (<date>):** one or two sentences, replaced each time: the answer, its number
   and run; what's next.
3. `## <date> · <name>`, newest first: up to 3 one-line bullets. Same day and person: edit that entry.

Say what was found and decided, with its conditions in a few words ("one seed, one split"). Leave
out setup, file names and what couldn't be checked (notes take them only as edit-notes says).
Under 150 words: shrink old entries to one line, then cut the oldest (summary_history keeps them).
Fix whatever newer work made wrong. Numbers from Probe, 3 significant figures. Leave
`<!-- kept: ... -->` blocks alone.

Who did what, or why: `entity` view=sessions or view=summary_history.
