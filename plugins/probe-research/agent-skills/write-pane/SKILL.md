---
name: write-pane
description: Writes a pane - one live visual the dashboard's own charts cannot draw (before/after images or documents, a per-task table, why tasks did not finish) on a project's or experiment's Probe page, redrawn from its runs' data each time it opens and while a run trains. Use for per-task eval results across runs and to show the PDFs or Word files a run wrote. A score compared across runs is a chart in an experiment's Metrics center (overview-visuals), never a pane.
---
# Write a pane

A pane is your own HTML/JS plus a recipe: the data it reads, never the data itself. Probe resolves
the recipe every time the pane opens, for the runs in view (an experiment's, a project's newest 50,
or the runs someone picked to compare), so new runs show up without a rewrite. Panes sit at the top
of the project's or experiment's page; summary rewrites never touch them.

Write one when the best visual depends on the task: before/after images for a vision task, a
table of tasks, why tasks did not finish.

Never draw a chart in a pane: its own bar or line chart looks unlike every other chart on the page.
A curve over steps is already in the metric section. A score compared across runs (a leaderboard, by
a config, or by a group such as document length) is a `table` or `bar` in the Metrics-center visuals
of the experiment whose runs recorded it (`overview-visuals`); the project page shows each
experiment's charts too. A research project and its experiments carry no charts: scored runs belong
in an evaluation, training or general project.

1. The run records the score once per group, under its own key, with a dimension of a few values:
   the group's mean, never one call per task and never a task id as the dimension
   (`probe.log({"eval/score_by_length": mean}, dimensions={"length": "1-2 pages"})`; for a run that
   already ended, `probe log RUN eval/score_by_length=0.74 --dim 'length=1-2 pages' --derived --producer <what computed it> --step <its last step>`).
2. Then chart it: a `bar` with `x: {dimension: "length"}` (`probe visuals set`; where the daemon
   records, ask its writer: `probe ask --writer "<the chart>"`).

A pane shows per-task rows, files and the server's `totals` as they are; it never averages
`samples` rows into a per-run or per-group score.

## 1. Record per-task results

One rollout span per task, its scores in `attributes`, its output files linked to it:

```python
with probe.span("rollout", name=task.id, external_key=task.id) as span:
    out = solve(task)
    span.attributes.update(score=out.score, unfinished_reason=out.reason)  # None when it finished
    probe.log_artifact(f"eval/{task.id}/after.png", path=out.image, span_id=span)
```

A run that writes Word files should also save a PDF of each, so a pane can show its exact look
(`soffice --headless --convert-to pdf --outdir out/ out/after.docx`), and log both to the span.

A sample is `failed` only when an exception escapes the `with` block; setting `unfinished_reason`
alone leaves it `completed`, so count why tasks did not finish by that attribute, not by status.
From a shell, `probe span add RUN --type rollout ...` prints the span id; upload a file to it with
`--span <span id>`.

## 2. Write the recipe and check it

```json
{"runs": "scope",
 "data": [
  {"id": "variants", "source": "runs", "config": ["lr", "model"]},
  {"id": "tasks", "source": "samples", "fields": ["attributes.score"], "counts": ["attributes.unfinished_reason"]},
  {"id": "after", "source": "files", "prefix": "eval/", "name": "*.png", "span": "rollout"}]}
```

`runs` is `"scope"` (the runs in view) or a list of run ids. `files` filters by `prefix`, `name`
glob, `span` type and `kind` (an artifact kind such as `evaluation`; there is no `image` kind, so
match images by `name`, `*.png`). Each source's rows, as the server returns them:

- `runs`: `id`, `name`, `status`, `experiment_id`, `created_at`, `locked`, `config{}` (null for a
  locked run, or one past the size cap, named in `cut`), `scalars{"<key>": {last, min, max, step}}`
  (a key the run never logged is absent).
- `series`: `run_id`, `key`, `points` as `[step, value]` pairs.
- `samples`: `id` (the span id), `run_id`, `name`, `external_key`, `status`, `step`, `started_at`,
  `ended_at`, `fields{"attributes.score": 0.8}`. A null or missing attribute is left out of
  `fields`.
- `files`: `id`, `run_id`, `name`, `kind`, `content_type`, `size_bytes`, `span_id`, `step`,
  `created_at`: a file belongs to the sample whose `id` is its `span_id`.

See what it resolves to before writing code:

```
probe pane data <project-or-experiment> --recipe recipe.json [--runs <run-id>,<run-id>]
```

It prints a preview: each entry's `state` (`ok`, `partial`, `too_large`), `rows_total` and first
rows, a line's first and last points (`points_total`), long values cut; `--full` prints it all. A
samples entry adds `totals`: per run, `total` and `counts{"status" or field: {values: [{value,
count}], other}}` over ALL its rollout spans, exact even when rows are cut. Spans without the field
count under `value: null`: drop that bucket when counting reasons.

`unmatched` names the keys no readable run has, and a samples entry's `unseen_fields` the fields not
in the `rows_read` rows read: a typo, or not logged yet (`state` stays `ok`). Take real names from a
run: `probe run series <run-id>` lists its metric keys, `probe span list <run-id> --type rollout
--limit 3` shows its samples' `attributes` (not the RL samples command: those are RL samples).

## 3. Write the code

- A pane is an HTML fragment, the inside of a page: no `<!doctype>`, `<html>`, `<head>`, `<body>`
  or `<!-- comments -->`. Start with your markup, `<style>` and `<script>` tags.
- `PROBE.runs('variants')`, `PROBE.samples('tasks')`, `PROBE.files('after')` (and `PROBE.series(id)`
  for a `series` entry) return that entry. Name each id literally and read every entry the recipe has.
- `PROBE.image(row)` resolves to a `data:` URL; `PROBE.text(row)` to text (CSV too);
  `PROBE.json(row)` to parsed JSON. They read only rows of the pane's own `files` entries. Each can
  reject (`budget`, `not_in_files`, `too_large`, `unavailable`): catch it and draw a placeholder, or
  the whole pane shows as failed.
- `PROBE.bytes(row)` resolves to an `ArrayBuffer` of the file's original bytes (a PDF, a Word
  file, any binary up to 25 MiB; bigger rejects `too_large`, unread when its `size_bytes` is known)
  for a library to draw.
- Images: create the `<img>` in script and set its `src` from `PROBE.image(row)`. An `<img>` in
  the markup is refused (its `src` is not a `data:` URL yet). A `<script>` is not a module, so
  wrap `await` in a function:

  ```js
  (async function () {
    var img = document.createElement('img');
    img.alt = row.name;
    document.getElementById('grid').appendChild(img);
    try { img.src = await PROBE.image(row); } catch (e) { img.alt = row.name + ': ' + e.message; }
  })();
  ```
- A `partial` entry still draws what it has; say it was cut.
- Color each run with its `color` from `PROBE_DATA.runs` (match by `id`), so the pane matches the
  explorer. The dashboard adds it, so `probe pane data` does not show it, and it can be null: keep
  a fallback palette.
- The frame reaches no network: no `fetch`, XHR, WebSocket or `import()`, no `eval` or
  `new Function`, no `postMessage`, no cookies. Load a library as one exact-version cdnjs build,
  `<script src="https://cdnjs.cloudflare.com/ajax/libs/d3/7.9.0/d3.min.js"></script>` (not Plotly
  or Vega: they need `eval`). A library that ships only as a module loads by its src,
  `<script type="module" src=".../pdf.min.mjs"></script>`; its global exists from
  `DOMContentLoaded` on.
- Buttons, `<select>`, and checkboxes and sliders written as markup (`<input type="checkbox">`,
  `<input type="range">`) are fine; never `createElement('input')`. No text fields,
  `contenteditable` or key listeners. A `<script>` takes only `src` (plus `type="module"` beside a
  library's `src`); attach handlers with `addEventListener`, never `on*` attributes.
- Look: start with the content. The dashboard prints the title above it, so don't repeat it. No
  outer border, card or box, and no inner scrollbar: the pane sits on the page and grows to its
  content. Use the host's colors (`--ov-fg`, `--ov-muted`, `--ov-line`, `--ov-accent`,
  `--ov-good`, `--ov-bad`); for design, `artifact-design`.

## 4. Live runs, documents, click-through

- **Live.** While a run in view is still going, the dashboard reads the pane's data again every
  30 s. Redraw it by passing your draw function to `PROBE.onData`; the reads above answer the new
  data, and your own controls (a slider, a picked tab) keep their state. Without it the pane keeps
  its first picture until someone clicks refresh.

  ```js
  function draw() { renderTable(PROBE.samples('tasks').rows, currentFilter); }
  draw();
  PROBE.onData(draw);
  ```
- **PDF.** pdf.js from its module build; draw only the pages on screen (this draws page 1):

  ```html
  <canvas id="page"></canvas>
  <script type="module" src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/6.4.299/pdf.min.mjs"></script>
  <script type="module" src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/6.4.299/pdf.worker.min.mjs"></script>
  <script>
  document.addEventListener('DOMContentLoaded', async function () {
    try {
      var bytes = await PROBE.bytes(PROBE.files('docs').rows[0]);
      var doc = await window.pdfjsLib.getDocument({ data: bytes }).promise;
      var page = await doc.getPage(1), viewport = page.getViewport({ scale: 1.2 });
      var canvas = document.getElementById('page');
      canvas.width = viewport.width; canvas.height = viewport.height;
      await page.render({ canvasContext: canvas.getContext('2d'), viewport: viewport, canvas: canvas }).promise;
    } catch (e) { document.getElementById('page').replaceWith(e.message); }
  });
  </script>
  ```
- **Word.** `mammoth.convertToHtml({arrayBuffer: bytes})` (cdnjs `mammoth/1.13.0/mammoth.browser.min.js`)
  gives a plain view: text, bold, italics and tables survive; fonts, colours and alignment do not.
  For the exact look, draw the PDF the run saved beside it.
- **Before/after diff.** For a document edit, unzip both files with JSZip
  (cdnjs `jszip/3.10.1/jszip.min.js`), read `word/document.xml`, and compare with jsdiff
  (cdnjs `jsdiff/8.0.2/diff.min.js`, global `Diff`): `Diff.diffArrays` over paragraph texts finds
  the changed paragraphs, `Diff.diffWordsWithSpace` the changed words, and comparing each run's
  `<w:b/>` and `<w:i/>` and each paragraph's `<w:jc>` shows formatting changes. Show it git-style: removed lines red,
  added green, unchanged paragraphs folded. The raw XML diff (`Diff.diffArrays` over its lines) is
  what the model wrote.
- **Click-through.** `PROBE.open(ref, {newTab})` opens a run (`{run: id}`: a `runs` row's `id`, any
  other row's `run_id`), a sample's trial page (`{sample: {run: row.run_id, id: row.id}}`) or a
  file's page (`{file: row.id}`). Call it from a click handler: it opens only right after a click
  in the pane, once per click, and only what the pane's data holds; otherwise it rejects
  (`no_gesture`, `not_in_data`). Catch it, or the whole pane shows as failed:
  `PROBE.open(ref).catch(function () {})`.

## 5. Push it

Check first, then save:

```
probe pane push <project-or-experiment> <slug> --html pane.html --recipe recipe.json --check
probe pane push <project-or-experiment> <slug> --html pane.html --recipe recipe.json [--title "Before / after"] [--position 0]
```

`--check` saves nothing and reports the same problems a push would, all at once, each with its line:
fix them all, check again, then push without it. A refused push names the same problems.

The same slug replaces the pane (the answer says `replaced`, with the `previous_updated_at`), so
pushing again is safe. A project or experiment holds 8. A new pane goes last; `--position N` sets
its place, 0 first. A push first opens the pane in a browser at every width; a refusal from that
names the width, state and error (fix it and push again), and a warning means the check could not
run and the pane was saved unchecked. The answer's `url` is the page the pane is on. Add `--json`
to any pane command for a typed refusal (`code` and message) on stdout.

To change a pane later, round-trip it: `probe pane get <project-or-experiment> <slug> --out DIR`
writes `DIR/<slug>.html` and `DIR/<slug>.json` (its recipe); edit them and push both back. A
re-push without `--title` keeps the title. `pane get` alone prints the pane's details without its
HTML (`html_chars`); `--html` prints the HTML too. `probe pane data <project-or-experiment> <slug>`
reads a saved pane's data and `probe pane list <project-or-experiment>` lists the panes.

`probe pane rm <project-or-experiment> <slug> --yes` deletes a pane for good: there is no trash,
so run `pane get ... --out DIR` first to keep a copy. Run it only when the researcher asks (an
agent cannot answer its prompt, so it needs `--yes`). Its runs and their data are untouched.

Pin a view only when the researcher asks, and once: pins sit at the top of the project for the
whole team, 24 at most, so pinning and unpinning take `--directed`. `probe pane push ... --pin
--directed` pins the pane it saves; `probe pin add <project-or-experiment> --pane <slug> --directed`
pins a saved one (or `--chart <metric>`, `--key-metrics`, `--parallel`); `probe pin list <project>`
reads them and `probe pin rm <project> <pin-id> --directed` unpins. Never pin from a script or a
sweep that runs again: a pin someone removed would come back. When Pinned is full, ask the
researcher which view to unpin.

From a script or notebook, the SDK does the same:

```python
client = probe.Client()
client.save_pane("vision-evals", "before-after", html, recipe, title="Before / after")
client.read_pane_data("vision-evals", recipe)  # what `probe pane data` prints
```

## 6. Limits

Past one of these a push is refused, a data read comes back `partial` or `too_large`, or a `PROBE`
file read rejects `too_large`:

| Limit | Value |
|---|---|
| Panes in one project or experiment | 8 |
| A pane's HTML | 300,000 characters |
| A pane's title | 200 characters |
| Entries in one recipe | 16 |
| Runs a recipe reads (a scope's newest) | 50 |
| Keys in one `runs` or `series` entry | 32 |
| Fields in one `samples` entry | 32 |
| `counts` fields in one `samples` entry, beside status | 3 |
| Sample rows in one entry | 5,000 |
| File rows in one entry | 2,000 |
| Points in one series line | 300 |
| One pane's resolved data | 2 MiB |
| Time to resolve one pane's data | 5 s |
| One image `PROBE.image` reads | 10 MiB |
| One file `PROBE.text` or `PROBE.json` reads | 1 MiB |
| One file `PROBE.bytes` reads | 25 MiB |
| Slug | `^[a-z0-9][a-z0-9-]{0,62}$`: lowercase words joined by hyphens |
| Recipe entry `id` | `^[A-Za-z][A-Za-z0-9_-]{0,63}$` |
