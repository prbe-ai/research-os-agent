---
name: overview-contract
description: The page's HTML, CSS and script rules; push refuses a page that breaks one. Load before editing a page.
---

`probe summary push` checks every rule here and hands the fault back.

# Shape

- Any font `<link rel="stylesheet">` and `<style>`, then `<article class="ov-page">...</article>`. No doctype, html, head, body, meta, base, iframe, form, input, button, audio or video. No HTML comments (`<!-- -->`, except the kept markers) and no `@layer`; comment CSS with `/* */`.
- No title or masthead: no `<h1>`, `<header>` or `<hgroup>`, and no heading first. Open with the TL;DR; `<h2>` / `<h3>` head later sections.
- No generated-at line or model name.

# A person's text: `<!-- kept: hN -->` ... `<!-- /kept -->`

- Every marker stays, in order; the server puts the person's words back in its place.
- Never write inside one, add `data-owner`, `data-strength` or `data-origin`, or touch one from a script. Keep their block out of a figure with code and out of a filled card.

# Script

- Code goes in a `<script>` inside the `<figure>` it draws: its container, its library and its code together (each figure loads its own library; the browser fetches it once). A `<script>` takes no attribute but `src`. Name no variable like a credential (ending `Sig`, `Signature`, `Token` or `ApiKey`, or holding `password`, `pwd`, `secret` or `credential`): push refuses what Probe's scrubber would rewrite. Attach handlers with `addEventListener` (click, hover), never `on*` attributes or `javascript:` URLs.
- Libraries only a pinned build from cdnjs, Chart.js, d3 or echarts: `https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js`, `https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js`, `https://cdnjs.cloudflare.com/ajax/libs/echarts/5.4.3/echarts.min.js`.
- The page reaches nothing: no fetch, XMLHttpRequest, WebSocket, WebRTC, `import()`, postMessage, cookies, navigation, window.open, top or parent, no field a person types into (input, textarea, contenteditable) and no key listeners.

# Look

- The frame is light only and sizes the page's box; inside it the design is yours (`artifact-design`). Style your own classes under `.ov-page`; leave `html`, `body` and the article's own box to the host. Host tokens: `--ov-bg`, `--ov-surface`, `--ov-surface-2`, `--ov-fg`, `--ov-muted`, `--ov-line`, `--ov-accent`, `--ov-good`, `--ov-warn`, `--ov-bad`, `--ov-info`, `--ov-font`, `--ov-mono`. No `prefers-color-scheme`.
- Fonts with `<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=...&display=swap">` before the `<style>` (`@import` is refused); images as base64 `data:` URIs; links `href="#section"` only.

# Charts

- Declare every logged series the page draws in the `<script type="application/json" data-probe-series>[...]</script>` block at the top of the draft: entries (`id`, `metric`, `run_ids`, `kind`, `x_axis`, `title`; optional `metrics`, `expression`, `label`, `chart`, `x_min`, `x_max`, `y_min`, `y_max`), keys and run ids copied from the `.chartable.json` file the checkout names; it lists this entity's own runs, so a run filed elsewhere (a reused control) is typed, not declared. Its `caps` are the limits: 6 entries, 8 runs and 16 lines a chart, 8 computed lines a page. `[]` when none.
- Draw each one, by its literal id: in your figure's code, `PROBE.series('loss')` returns `{metric, title, x_axis, runs: [{run_id, name, metric, points: [[x, y], ...], missing}]}`, one entry per run and key (a computed line has a `label`; x is the step, or epoch ms on `wall_clock`); skip an entry whose `points` is empty. Or with the dashboard's own chart: `<div id="chart-loss" class="ov-chart" style="height:280px"></div><script>PROBE.lineChart('chart-loss', 'loss', {title: 'Validation loss'})</script>` (options: `title`, `yName`, `xName`, `smooth`, `log`).
- A page that draws nothing (no declared series, no `<canvas>` or `<svg>`) is refused when the entity's runs logged curves. A declared series nobody draws is a fault. Numbers that no run logged as a series: type them, copied from Probe reads this run.
