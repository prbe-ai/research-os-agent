---
name: write-pane
description: Writes a pane - one live visual (a leaderboard, before/after images, a task table, a chart) on a project's or experiment's Probe page, redrawn from its runs' data each time it opens. Use when results would read better as a picture than as numbers, and for per-task eval results across runs.
---
# Write a pane

A pane is your own HTML/JS plus a recipe: the data it reads, never the data itself. Probe resolves
the recipe every time the pane opens, for the runs in view (an experiment's, a project's newest 50,
or the runs someone picked to compare), so new runs show up without a rewrite. Panes sit at the top
of the project's or experiment's page; summary rewrites never touch them.

Write one when the best visual depends on the task: before/after images for a vision task, a
leaderboard of variants, a table of tasks, why tasks did not finish. Skip it for a curve the
dashboard already draws.

## 1. Record per-task results

One rollout span per task, its scores in `attributes`, its output files linked to it:

```python
with probe.span("rollout", name=task.id, external_key=task.id) as span:
    out = solve(task)
    span.attributes.update(score=out.score, unfinished_reason=out.reason)  # None when it finished
    probe.log_artifact(f"eval/{task.id}/after.png", path=out.image, span_id=span)
```

From a shell, `probe span add RUN --type rollout ...` prints the span id; upload a file to it with
`--span <span id>`.

## 2. Write the recipe and check it

```json
{"runs": "scope",
 "data": [
  {"id": "board", "source": "runs", "keys": ["eval/score"], "config": ["lr", "model"]},
  {"id": "loss", "source": "series", "keys": ["train/loss"]},
  {"id": "tasks", "source": "samples", "fields": ["attributes.score"], "counts": ["attributes.unfinished_reason"]},
  {"id": "after", "source": "files", "prefix": "eval/", "name": "*.png", "span": "rollout"}]}
```

`runs` is `"scope"` (the runs in view) or a list of run ids. `runs` rows carry each run's config and
last/min/max of `keys`; `series` the curves; `samples` the rollout spans with `fields`; `files` file
rows, filtered by `prefix`, `name` glob, `span` type and `kind` (an artifact kind such as
`evaluation`; there is no `image` kind, so match images by `name`, `*.png`). See what it resolves to
before writing code:

```
probe pane data <project-or-experiment> --data recipe.json [--runs <run-id>,<run-id>]
```

Each entry comes back with `state` (`ok`, `partial`, `too_large`) and `rows`. A samples entry adds
`totals`: per run, `total` and `counts` over ALL its rollout spans, exact even when rows are cut.

## 3. Write the code

- `PROBE.runs('board')`, `PROBE.series('loss')`, `PROBE.samples('tasks')`, `PROBE.files('after')`
  return that entry. Name each id literally and read every entry the recipe has.
- `PROBE.image(row)` resolves to a `data:` URL for an `<img>`; `PROBE.text(row)` to text (CSV too);
  `PROBE.json(row)` to parsed JSON. They read only rows of the pane's own `files` entries. Each can
  reject (`budget`, `not_in_files`, `too_large`): catch it and draw a placeholder, or the whole pane
  shows as failed.
- A `partial` entry still draws what it has; say it was cut.
- Color each run with its `color` from `PROBE_DATA.runs`, so the pane matches the explorer.
- The frame reaches no network: no `fetch`, XHR, WebSocket or `import()`, no `eval` or
  `new Function`, no `postMessage`, no cookies. Load a library as one exact-version cdnjs build,
  `<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>` (not Plotly
  or Vega: they need `eval`).
- Buttons, `<select>`, and checkboxes and sliders written as markup (`<input type="checkbox">`,
  `<input type="range">`) are fine; never `createElement('input')`. No text fields,
  `contenteditable` or key listeners. A `<script>` takes only `src`; attach handlers with
  `addEventListener`, never `on*` attributes.
- Look: start with the content. The dashboard prints the title above it, so don't repeat it. No
  outer border, card or box, and no inner scrollbar: the pane sits on the page and grows to its
  content. Use the host's colors (`--ov-fg`, `--ov-muted`, `--ov-line`, `--ov-accent`,
  `--ov-good`, `--ov-bad`); for design, `artifact-design`.

## 4. Push it

```
probe pane push <project-or-experiment> <slug> --html pane.html --data recipe.json [--title "Before / after"]
```

The same slug replaces the pane, so pushing again is safe. A project or experiment holds 8. A
refusal names the rule the page broke: fix it and push again. `probe pane list`, `probe pane get
<project-or-experiment> <slug> --out <folder>` and `probe pane rm` do the rest.

From a script or notebook, the SDK does the same:

```python
client = probe.Client()
client.save_pane("vision-evals", "before-after", html, recipe, title="Before / after")
client.read_pane_data("vision-evals", recipe)  # what `probe pane data` prints
```
