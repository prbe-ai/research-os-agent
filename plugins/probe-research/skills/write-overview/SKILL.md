---
name: write-overview
description: Write the first version of a project's or experiment's Overview page through the agent door (`probe overview write`) — an artifact someone else can read to understand what is going on. Load it when a project or experiment you worked on has no page yet, or when the researcher asks for the page; the lane keeps the page current afterwards.
---

# Write the Overview page

The Overview tab of a project or experiment is one self-contained HTML page:
an artifact someone else reads to understand what is going on. The research
dashboard writes one automatically from the tracked record, but the agent
that did the work can write the FIRST version -- it knows why the work
exists and what happened -- and the dashboard's own lane refreshes that page
later as the entity changes. Same contract either way: the server lints the
page and refuses (422) anything the sandboxed frame would block, with the
fault named so you can fix it and resend.

Write it when: you have finished a body of work on a project or experiment
that has no page yet, or the researcher asks for one. A write REPLACES the
current page, so do not rewrite a page that exists unless asked. Never touch
the entity's authored Markdown or description: those are the researcher's.

## What the page is

Its first job is to EXPLAIN, the way you would tell a colleague at the
whiteboard: what this is, why it exists, what was tried, what happened, what
it means, what comes next. Open with two to four short paragraphs, plain
words, complete sentences, in that order -- a reader who stops after the
first paragraph can still say in one sentence what the work is about and
where it stands. Gloss every house term the first time ("BFCL (the
tool-calling benchmark)"). Never open with a table, a strip of settings, a
status grid, a chart or a diagram. No title, no dek, no byline: the dashboard
prints the entity's name, description, people and tags above the page.

After the explanation, only what the evidence earns, each under a heading
that says something (a claim or a question, never "Results"): the handful of
settings a reader orients by; what is running or finished and the one number
that matters; the mechanism, with an inline SVG diagram when a picture shows
a structure prose cannot; the comparison that answers the question, as a
table or a chart; how it runs; the files and checkpoints that matter; open
threads. 400 to 1,200 words of visible text.

Grounding: every fact, name and number comes from the tracked record or
your own session. Never invent data. Round to three significant figures and
say which run a number belongs to. No secrets, ever -- keys, tokens,
connection strings and private URLs become `<redacted>`. No bookkeeping
(byte counts, retry counts, raw ids as decoration).

## The fragment

Return an HTML FRAGMENT: one `<style>` element, the markup wrapped in
`<article class="ov-page">…</article>`, then any `<script>` elements. No
document wrapper, meta, forms, frames or media. Scope every CSS rule under
`.ov-page`; colors come from the host tokens (`var(--ov-fg)`,
`var(--ov-muted)`, `var(--ov-line)`, `var(--ov-accent)`, `var(--ov-good)`,
`var(--ov-warn)`, `var(--ov-bad)`, `var(--ov-surface)`), fonts from
`var(--ov-font)` / `var(--ov-mono)`. The host sizes the page: no padding,
margin, width, max-width, font-size or line-height on `.ov-page` itself;
headings one step above the text at most. Scripts and styles inline or from
the CDN allowlist only (cdnjs.cloudflare.com, cdn.jsdelivr.net/npm/,
cdn.tailwindcss.com, code.jquery.com); no fetch, no navigation, links are
`href="#section"` only; images only as data: URIs (draw diagrams as inline
`<svg>` instead).

Charts: never type data points. Declare each series in `--series` (a metric
key and the run ids exactly as the record names them, at most 8 runs per
set, at most 6 sets); the host injects the real points and draws the chart.
For each declared set write one container and one call, nothing else:

    <div id="chart-loss" class="ov-chart" style="height:280px"></div>
    <script>PROBE.lineChart('chart-loss', 'loss', {title: 'Validation loss', yName: 'val/loss'})</script>

A declared series that no call draws is a fault; so is a series whose min
equals max (state it once in prose instead).

## The write

    probe overview write page.html \
      --project <slug or id:<uuid>>            # or --experiment
      --blurb "<at most three plain sentences: what this is, what it is for, where it stands>" \
      --plan "<4-6 named colors, the type roles, one sentence of layout>" \
      --series @series.json                     # [{"id":"loss","metric":"val/loss","run_ids":["<uuid>"]}]

A bare ref is a SLUG; write an id as `id:<uuid>`. The blurb is shown on cards
and read by other summaries. On a 422 the response names the fault: fix the
fragment and resend the whole thing, never a diff. Success prints the stored
page's provenance (`content`, `source: agent`, `generated_at`).
