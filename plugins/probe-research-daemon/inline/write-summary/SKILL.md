---
name: write-summary
description: Keep each project's and experiment's page - what people read to catch up.
---
Each project and experiment the work touches has a page, a custom Claude artifact in the dashboard's Summary pane. Write it only to its draft and `probe summary push` it; never publish it elsewhere.

The draft is checked out for you when work first lands on one (the path is printed); `probe summary checkout --project <slug>` (or `--experiment <slug>`) loads one by hand. Edit it, then push once with the same flag. Write the first page when you create one; update it on a result, a decision or a change of direction. Linking a project `--part-of` another: update that page to name the new part. When what a project is about changes, or its runs first hold a real example, rewrite its canvas preview too (`write-preview`, when that skill is shown to you). A `.md` draft (older CLI) takes Markdown, never html.

Before editing, read `overview-page`, `overview-contract`, `artifact-design`, `artifact-diagramming` and, with metrics, `overview-charts` (beside this file), unless shown to you whole.

# The page, top to bottom

1. TL;DR in plain words a 10-year-old could follow, no numbers: one sentence on what it is, then up to 5 one-line bullets:
   - where it's headed now
   - each key decision: what was picked over what, and why
   - how far it has moved from what it set out to do (the card's `intent`, or its first page in `summary_history`)
2. Next: the open decision, question or ask.
3. Evidence, and how sure we are: charts, tables, diagrams, number tiles, one sentence each. A chart for any logged curve or any comparison of more than three numbers, never a table instead.

- Bullets everywhere, one point each.
- A why comes only from what was recorded: the session, an experiment's conclusion or dead end, a note. None recorded: name the decision alone. Nothing decided or no drift yet: drop the bullet that has nothing to say.
- Each update rewrites the top around the newest decision or result; never add below it. Cut what newer work made stale or wrong.
- A project page covers all its parts, sized by the work in each, not by recency. Edit your part's line, and the TL;DR whenever it stops being true. Detail goes on the experiment's page.

Numbers: measurements to 3 significant figures, counts exact, p-values as Probe gives them. Leave `<!-- kept: ... -->` blocks alone. Who did what: `probe mcp call entity '{"refs": ["<kind>:<slug>"], "view": "sessions"}'` (or `"summary_history"`).
