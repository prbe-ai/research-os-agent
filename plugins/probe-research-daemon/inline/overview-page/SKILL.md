---
name: overview-page
description: What a page says, in what order, and how long. Load before editing a page.
---

People read top to bottom, so the top carries what matters most now:

1. What this is, in one or two sentences.
2. The most recent or most important thing: the latest result, decision or change.
3. What it means, and how sure we are.
4. What's next: a decision, the next question, or an ask.

Older history goes below, or goes.

- Open with prose. No title, dek or byline (the dashboard prints them), and no settings strip, status grid, chart or diagram first. A small table of the latest results may follow the opening. Headings state claims.
- The structure follows the latest data: a section or table that fit an earlier stage gets rewritten, not kept.
- Prose carries the argument; a visual only what prose can't (a curve, a mechanism, a few final scores).
- Results tables (and an experiment's results charts) go in its Metrics center through `probe visuals set` (`overview-visuals`), not the page.
- Skip a question the evidence can't answer, in one line.
- A bigger effort's page covers its parts: its card lists `parts` (a part's lists `part_of`); read each with `entity`.
- About 300 words of visible text.

# A person's own blocks

They show as `<!-- kept: hN -->` ... `<!-- /kept -->`, their own html between the markers; build the page around them.

- Leave every marker where it is, in order; the server puts their words back.
- Never restate or quote one elsewhere.

Also load `overview-charts` when there are metrics, `artifact-diagramming` for a diagram and `artifact-design` for the look. `overview-contract` wins where they disagree.
