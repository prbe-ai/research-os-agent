---
name: write-summary
description: Keep each project's and experiment's page - what people read to catch up.
---
Each project and experiment the work touches has a page, a custom Claude artifact in the dashboard's Summary pane. Write it only to its draft and `probe summary push` it; never publish it elsewhere.

The draft is checked out for you when work first lands on one (the path is printed); `probe summary checkout --project <slug>` (or `--experiment <slug>`) loads one by hand. Edit it, then push once with the same flag. Write the first page when you create one; update it on a result or a change of direction. Linking a project `--part-of` another: update that page to name the new part. When what a project is about changes, or its runs first hold a real example, rewrite its canvas preview too (`write-preview`, when shown to you). A `.md` draft (older CLI) takes Markdown, never html.

Before editing, read `overview-page`, `overview-contract`, `artifact-design`, `artifact-diagramming` and, with metrics, `overview-charts` (beside this file), unless shown to you whole.

# The page, top to bottom

Push refuses any other shape (limits in `overview-page`).

1. One line: what it is.
2. Up to 4 bullets; drop any with nothing to say:
   - where it stands
   - next step
   - open question
   - what you need from the researcher
3. Evidence: per point, a heading that says it, a visual, a caption. A logged curve, or more than three numbers compared: a chart, never a table.

- The line and bullets: plain words a 10-year-old could follow, no numbers.
- Nothing else: no decisions (they have their own tab), no history, no text between sections.
- Each update rewrites the bullets around the newest result; cut what's stale.
- A project page covers all its parts, sized by the work in each, not by recency. Detail goes on the experiment's page.

Numbers (visuals and captions only): measurements to 3 significant figures, counts exact, p-values as Probe gives them. Leave `<!-- kept: ... -->` blocks alone. Who did what: `probe mcp call entity '{"refs": ["<kind>:<slug>"], "view": "sessions"}'` (or `"summary_history"`).
