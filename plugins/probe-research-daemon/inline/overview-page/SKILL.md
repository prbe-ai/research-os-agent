---
name: overview-page
description: What a page says, in what order, and how it looks. Load before editing a page.
---

A page is a custom Claude artifact in the dashboard's Summary pane: a research memo, plain and polished, never a landing page.

People read top to bottom, so the top carries what matters most now:

1. TL;DR: what this is in one `<p>` sentence, then a `<ul>` of up to 3 one-line bullets on where it stands. No heading above it.
2. The most recent or most important result, shown: a chart, table or diagram, with one sentence on what it shows.
3. What it means, and how sure we are.
4. What's next: a decision, the next question, or an ask.

Older history goes below, or goes.

- Visuals carry the evidence; prose joins them. At most 400 words of prose (push refuses more); what sits in a `<figure>`, `<table>`, `<svg>` or `<canvas>` doesn't count, so put a diagram or a row of number tiles in a `<figure>`.
- No title, dek or byline (the dashboard prints them). Headings state claims.
- The structure follows the latest data: a section or table that fit an earlier stage gets rewritten, not kept.
- Skip a question the evidence can't answer, in one line.
- A bigger effort's page covers its parts: its card lists `parts` (a part's lists `part_of`); read each with `entity`.

Where these skills disagree, write-summary and this one (what to say) win, then `overview-contract` (what the frame allows), then `artifact-design` (the look).

# A person's own blocks

They show as `<!-- kept: hN -->` ... `<!-- /kept -->`, their own html between the markers; build the page around them.

- Leave every marker where it is, in order; the server puts their words back.
- Never restate or quote one elsewhere.
