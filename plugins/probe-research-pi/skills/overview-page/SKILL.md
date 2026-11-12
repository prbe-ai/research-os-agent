---
name: overview-page
description: How a page is built - its frame, headings, length, parts and a person's blocks. Load before editing a page.
---

A page is a custom Claude artifact in the dashboard's Summary pane: a research memo, plain and polished, never a landing page. What it says, and in what order, is `write-summary`'s.

- At most 400 words of prose (push refuses more); a `<figure>`, `<table>`, `<svg>` or `<canvas>` doesn't count, so put a diagram or a row of number tiles in a `<figure>`.
- No title, dek or byline (the dashboard prints them). Open with the TL;DR as a `<p>` and a `<ul>`, no heading above it. Headings state claims.
- Skip a question the evidence can't answer, in one line.
- A bigger effort's page covers its parts: its card lists `parts` (a part's lists `part_of`), `probe experiment list --project P` its experiments; read each with `entity` (`record` holds a conclusion whole; the card cuts it).

Where these skills disagree: `write-summary` wins, then this one, then `overview-contract`, then `artifact-design`.

# A person's own blocks

They show as `<!-- kept: hN -->` ... `<!-- /kept -->`; build the page around them. Leave every marker where it is, in order (the server puts their words back), and never restate or quote one elsewhere.
