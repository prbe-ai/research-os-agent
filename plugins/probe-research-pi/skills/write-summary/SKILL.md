---
name: write-summary
description: Keep project and experiment summaries - short meeting notes people read to catch up.
---
You keep the summary of each project and experiment the work touches. People read it to catch up.

When the work moves to one, load its summary: `probe summary checkout --project <slug>` (or
`--experiment <slug>`). A new project or experiment gets its first summary when you create it.
Update it when the session produces a result, a decision or a change of direction - not every
turn. Edit the checked-out file, then `probe summary push` with the same flag.

Top down:
1. One or two sentences: what it is (an experiment: the question it answers).
2. **Where it stands (<date>):** the latest result with its number and run, what is running,
   what is next. Replace it each time.
3. Entries, newest first: `## <date> · <name>`, 2-5 bullets - what was tried, results,
   decisions, next. Same day and person: edit that entry.

Under 500 words (the server refuses over 12,000 characters): fold old entries into one line or
cut them. Rewrite whatever the latest work made wrong. Plain words; numbers from Probe, 3
significant figures, naming the run. Leave `<!-- kept: ... -->` blocks as they are. These
dated entries live here only; notes stay as edit-notes says.

To fill a gap - who did what, why something was decided - use `entity` view=sessions or
view=summary_history, then grep a session's transcript.
