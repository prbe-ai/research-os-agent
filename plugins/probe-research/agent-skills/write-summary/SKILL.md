---
name: write-summary
description: Keep each project's and experiment's page - what people read to catch up.
---
Each project and experiment the work touches has a page people read to catch up, in the dashboard's Summary pane. Design it like a custom Claude artifact, but write it only to its draft file and `probe summary push` it; never publish it anywhere else.

Its draft is checked out for you when the work first lands on one (the path is printed);
`probe summary checkout --project <slug>` (or `--experiment <slug>`) loads one by hand. Edit it, then
`probe summary push` with the same flag; one push when you are done is enough. Write the first one when
you create it; then update on a result, a decision or a change of direction.
Creating or linking a project `--part-of` another changes that one too: update its page so it names the new part.
A `.md` draft (an older CLI) takes Markdown, never html.

Before a page edit, read `overview-page`, `overview-contract`, `artifact-design` and
`artifact-diagramming` (`SKILL.md` beside this one), and `overview-charts` when there are metrics,
unless they are already shown to you whole.

- Top: a TL;DR a smart 10-year-old could follow - one sentence on what it is, then up to 3 one-line
  bullets on where it stands, numbers kept.
- Show, don't tell: a diagram wherever it makes a comparison or a pipeline clearer, and a chart wherever
  a run logged a curve or a comparison holds more than three numbers; a table never stands in for that
  chart. One sentence per visual says what it shows.
- Each update rewrites the top around the newest result (on a project page, the project's answer) and
  replaces the old status; never add a paragraph under it. A section that no longer fits is rewritten or cut.
- A project page sums up all its parts, each given room by the work in it (runs, weeks), never by which
  came last: its top says what the project has produced and where each main line of work stands, and
  its chart sets the experiments' answers side by side. Change your part's line there, and its TL;DR
  only when the project's answer moved. The detail goes on the experiment's page.

Fix whatever newer work made wrong. Numbers from Probe: measurements to 3 significant figures, counts
(steps, runs, rows) exact, a p-value as Probe gives it. Leave
`<!-- kept: ... -->` blocks alone.

Who did what, or why: `probe mcp call entity '{"refs": ["<kind>:<slug>"], "view": "sessions"}'` (or `"summary_history"`).
