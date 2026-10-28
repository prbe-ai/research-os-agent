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
Creating or linking a project `--part-of` another changes that one too: update its page so it names the new part.
A `.md` draft (an older CLI) takes Markdown, never html.

Before you edit a page, read `overview-page/SKILL.md` (what the page says and in what order) and
`overview-contract/SKILL.md` (its rules; push refuses a page that breaks one); they sit beside this skill. Also
`overview-charts/SKILL.md` when there are metrics, `artifact-diagramming/SKILL.md` for a diagram and
`artifact-design/SKILL.md` for the look.

Fix whatever newer work made wrong. Numbers from Probe, 3 significant figures. Leave
`<!-- kept: ... -->` blocks alone.

Who did what, or why: `probe mcp call entity '{"refs": ["<kind>:<slug>"], "view": "sessions"}'` (or `"summary_history"`).
