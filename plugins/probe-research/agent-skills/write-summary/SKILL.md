---
name: write-summary
description: Keep each project's and experiment's page - what people read to catch up.
---
Each project and experiment the work touches has a page people read to catch up.

Its draft is checked out for you when the work first lands on one (the path is printed);
`probe summary checkout --project <slug>` (or `--experiment <slug>`) loads one by hand. Read the part
you change, edit it in place, then `probe summary push` with the same flag; edits add up, so one push
when you are done is enough. Write the first one when you create it; then update on a result, a
decision or a change of direction.
A `.md` draft (an older CLI) takes Markdown, never html.

`overview-page` says what the page says and in what order; `overview-contract` its rules (push refuses a
page that breaks one). Also `overview-charts` when there are metrics, `artifact-diagramming` for a
diagram and `artifact-design` for the look.

Fix whatever newer work made wrong. Numbers from Probe, 3 significant figures. Leave
`<!-- kept: ... -->` blocks alone.

Who did what, or why: `entity` view=sessions or view=summary_history.
