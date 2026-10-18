---
name: overview-contract
description: The page's HTML, CSS and chart rules; a linter refuses a page that breaks one. Load before editing a page.
---

A linter checks every rule here; `probe summary push` hands the fault back.

# Shape

- One `<style>`, then `<article class="ov-page">...</article>`, then any `<script>`. No doctype, html, head, body, meta, base, iframe, form, input, button, audio or video.
- No title or masthead: no `<h1>`, `<header>` or `<hgroup>`, and no heading or styled name first. Open with prose; `<h2>` / `<h3>` head later sections.
- No generated-at line or model name.

# A person's text: `<!-- kept: hN -->` ... `<!-- /kept -->`

- Every marker stays, in order; the server puts the person's words back in its place.
- Never write inside one, or add `data-owner`, `data-strength` or `data-origin`.

# Sandbox

- Styles inline; the only script is the chart calls below, and a page loads no library. No `on*` handlers, no `javascript:` URLs. Fonts from fonts.googleapis.com / fonts.gstatic.com. Images as `data:` URIs; diagrams as inline `<svg>`.
- No fetch, XMLHttpRequest, WebSocket, form, window.open or navigation. No `@import`; `url()` only for `data:` or fonts.gstatic.com.
- Links: `href="#section"` only. A person's marked element may keep their own https link; you never write one.

# Look: the host is light and owns the frame

- Nothing on `.ov-page`, `html` or `body` (background, padding, margin, width, max-width, font-size, line-height). Scope every rule under `.ov-page`.
- Headings at most one step above 14px. No background, border or shadow on tiles, chart or table wrappers, cards or lists. Keep table row lines, status badges and a diagram's meaningful strokes and fills.
- Colors and fonts only from the host tokens, as `var(--ov-fg)`: `--ov-bg`, `--ov-surface`, `--ov-surface-2`, `--ov-fg`, `--ov-muted`, `--ov-line`, `--ov-accent`, `--ov-good`, `--ov-warn`, `--ov-bad`, `--ov-info`, `--ov-font`, `--ov-mono`. Set `.ov-page { --ov-accent: <hex>; }` once; one other literal hex only for a diagram's key element.
- No `prefers-color-scheme` query.

# Charts: the host draws them (`overview-charts` says when)

- Declare the series in the `<script type="application/json" data-probe-series>[...]</script>` block at the top of the draft: a list of entries (`id`, `metric`, `run_ids`, `kind`, `x_axis`, `title`; optional `metrics`, `expression`, `label`, `chart`, `x_min`, `x_max`, `y_min`, `y_max`), keys and run ids copied from the `.chartable.json` file the checkout names. At most 6 sets; `[]` when the page has no chart.
- Per declared series, one container and one call, nothing else: `<div id="chart-loss" class="ov-chart" style="height:280px"></div><script>PROBE.lineChart('chart-loss', 'loss', {title: 'Validation loss', yName: 'val/loss'})</script>`
- A declared series with no call, or whose min equals max, is a fault.
- No chart of your own: no library, no `<canvas>`, no data plotted by hand in `<svg>`; the dashboard draws every chart. A number with no logged series goes in a table or a sentence. `artifact-design`'s chart guidance is for diagrams only.
